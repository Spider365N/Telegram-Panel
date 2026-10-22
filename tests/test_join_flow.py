"""
The join bug that kept production broken.

Telegram answers ``messages.CheckChatInvite`` in three shapes:

==========================  ============  ==================================
result                      has ``.chat``  meaning
==========================  ============  ==================================
``ChatInviteAlready``       yes           the account is already a member
``ChatInvitePeek``          yes           expired link the account peeked at
``ChatInvite``              **no**        not a member yet → must be imported
==========================  ============  ==================================

The old code did ``chat = getattr(result, "chat", None)`` and only sent
``ImportChatInviteRequest`` when that was truthy — so for the *only* case that
matters (an account that is not inside yet) the import was never sent, ``join()``
returned ``None`` and every reaction failed with:

    Could not resolve chat -1003852827894 for this account (private channel …)

These tests model Telegram's real answer shapes (see ``tests/fakes.py``) and
prove accounts actually get inside.
"""
from __future__ import annotations

import pytest

from tests.fakes import FakeTelegramClient, FakeEvent, build_stack, make_channel

OWNER = 123456789
USER = 4242
CHANNEL_ID = 810001
INTERNAL = int(f"-100{CHANNEL_ID}")


async def private_setup(accounts: int = 3, *, member_of=None, fail_join: bool = False,
                        request_approval: bool = False, invite_link=None,
                        stale_hashes=None):
    """A private channel + a pool of accounts that are NOT inside it."""
    channel = make_channel(CHANNEL_ID)          # no username → private
    clients = {
        f"phone{i+1}": FakeTelegramClient(
            f"phone{i+1}", user_id=6000 + i, channels={CHANNEL_ID: channel},
            member_of=set() if member_of is None else member_of,
            fail_join=fail_join, request_approval=request_approval,
            stale_hashes=stale_hashes,
        )
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(INTERNAL, USER, title="Private Channel",
                                  invite_link=invite_link or "https://t.me/+prodInviteHash")
    return stack, channel


@pytest.mark.asyncio
class TestJoinByInviteUnit:
    async def test_a_plain_chat_invite_is_imported(self):
        """ChatInvite has no `.chat` — the import still has to happen."""
        stack, channel = await private_setup(1)
        client = stack.bot.active_clients["phone1"]
        resolver = stack.engagement.resolver

        entity = await resolver.join_by_invite(client, "prodInviteHash", chat_id=INTERNAL)

        assert entity is not None, "join returned nothing for a not-yet-member account"
        assert client.joined == ["prodInviteHash"], "ImportChatInviteRequest was not sent"
        assert CHANNEL_ID in client.member_of, "the account is still not a member"
        assert client.requests and type(client.requests[-1]).__name__ == "ImportChatInviteRequest"

    async def test_an_existing_member_is_not_imported_twice(self):
        stack, channel = await private_setup(1, member_of={CHANNEL_ID})
        client = stack.bot.active_clients["phone1"]
        entity = await stack.engagement.resolver.join_by_invite(
            client, "prodInviteHash", chat_id=INTERNAL)
        assert entity is not None
        assert client.joined == [], "an existing member must not import the link again"

    async def test_approval_only_channels_explain_themselves(self):
        stack, channel = await private_setup(1, request_approval=True)
        with pytest.raises(ValueError) as excinfo:
            await stack.engagement.resolver.join_by_invite(
                stack.bot.active_clients["phone1"], "prodInviteHash", chat_id=INTERNAL)
        assert "approv" in str(excinfo.value).lower() or "join request" in str(excinfo.value).lower()

    async def test_an_expired_link_explains_itself(self):
        stack, channel = await private_setup(1, fail_join=True)
        with pytest.raises(ValueError) as excinfo:
            await stack.engagement.resolver.join_by_invite(
                stack.bot.active_clients["phone1"], "prodInviteHash", chat_id=INTERNAL)
        assert "no longer valid" in str(excinfo.value)

    async def test_resolve_joins_and_returns_the_entity(self):
        """``resolve(..., join=True)`` is the path every reaction uses."""
        stack, channel = await private_setup(2)
        client = stack.bot.active_clients["phone1"]
        entity = await stack.engagement.resolver.resolve(
            client, INTERNAL, join=True, invite_hash="prodInviteHash")
        assert entity is not None
        assert client.joined == ["prodInviteHash"]


@pytest.mark.asyncio
class TestTheProductionScenario:
    """Bot added → batch added → warm-up must put the accounts inside."""

    async def test_warmup_joins_the_pool_into_a_private_channel(self):
        stack, channel = await private_setup(4)
        outcome = await stack.auto.enable_free_plan(INTERNAL)
        warmup = outcome["warmup"]
        assert warmup.get("ok") == 4, outcome          # all four accounts got in
        for name, client in stack.bot.active_clients.items():
            assert CHANNEL_ID in client.member_of, f"{name} never joined"
        # the link stays on the channel for later posts
        doc = await stack.channels.get(INTERNAL)
        assert doc["invite_link"]

    async def test_the_first_post_after_joining_gets_reactions(self):
        """This is the exact production log loop — and it must not happen."""
        stack, channel = await private_setup(6)
        await stack.auto.enable_free_plan(INTERNAL)
        outcome = await stack.auto.handle_new_post(INTERNAL, 30)
        reactions = outcome["reactions"]
        assert reactions["ok"] > 0, reactions
        assert reactions["failed"] == 0, reactions
        errors = (await stack.channels.get(INTERNAL))["stats"]
        assert errors["reactions_sent"] == reactions["ok"]

    async def test_no_account_ever_leaves_the_room_without_joining(self):
        """Before the fix every account failed with 'Could not resolve chat'."""
        stack, channel = await private_setup(3, member_of=set())
        await stack.auto.enable_free_plan(INTERNAL)
        for message_id in range(31, 34):        # three posts, like the log
            outcome = await stack.auto.handle_new_post(INTERNAL, message_id)
            assert outcome["reactions"]["ok"] > 0, (message_id, outcome)
        joined = [n for n, c in stack.bot.active_clients.items() if c.joined]
        assert len(joined) == 3

    async def test_a_channel_with_no_link_yet_gets_one_from_the_bot(self):
        channel = make_channel(CHANNEL_ID)
        clients = {"phone1": FakeTelegramClient("phone1", user_id=6001,
                                                channels={CHANNEL_ID: channel},
                                                member_of=set())}
        stack = await build_stack(clients)
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(INTERNAL, USER, title="No link yet")
        outcome = await stack.auto.enable_free_plan(INTERNAL)
        assert stack.bot.exported_invites, "the bot should export a link"
        assert outcome["warmup"].get("ok") == 1, outcome
        assert CHANNEL_ID in stack.bot.active_clients["phone1"].member_of

    async def test_a_failing_join_tells_the_owner_what_to_do(self):
        stack, channel = await private_setup(2, fail_join=True)
        await stack.auto.enable_free_plan(INTERNAL)
        await stack.auto.handle_new_post(INTERNAL, 35)
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("could not be applied" in text.lower() for text in messages), messages
        assert any("no longer valid" in text.lower() or "invite" in text.lower()
                   for text in messages)


@pytest.mark.asyncio
class TestPublicChannelsStillSkipJoining:
    async def test_public_channel_never_joins(self):
        channel = make_channel(CHANNEL_ID, username="publiclogs")
        clients = {"phone1": FakeTelegramClient("phone1", user_id=6001,
                                                channels={CHANNEL_ID: channel},
                                                member_of=set())}
        stack = await build_stack(clients)
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(INTERNAL, USER, title="Public", username="publiclogs")
        await stack.auto.enable_free_plan(INTERNAL)
        outcome = await stack.auto.handle_new_post(INTERNAL, 1)
        assert outcome["reactions"]["ok"] == 1, outcome
        assert stack.bot.active_clients["phone1"].joined == []


@pytest.mark.asyncio
class TestSelfHealingAndInputValidation:
    async def test_a_revoked_link_is_replaced_and_the_join_retried(self):
        """The stored link went stale → a fresh one is exported for the retry."""
        stack, channel = await private_setup(
            2, stale_hashes={"prodInviteHash"})     # the stored link is dead
        exported = []
        real = stack.bot._export_invite

        async def counting_export(channel_id):
            link = await real(channel_id)
            exported.append(link)
            return link

        stack.bot._export_invite = counting_export
        outcome = await stack.auto.enable_free_plan(INTERNAL)

        assert exported, "the bot must export a fresh link after a dead one"
        # the accounts joined through the *fresh* link
        assert outcome["warmup"]["ok"] == 2, outcome
        for client in stack.bot.active_clients.values():
            assert CHANNEL_ID in client.member_of
        doc = await stack.channels.get(INTERNAL)
        assert doc["invite_link"].endswith("fake810001"), doc["invite_link"]

    async def test_a_dead_link_is_also_refreshed_on_a_plain_post(self):
        stack, channel = await private_setup(2, stale_hashes={"prodInviteHash"})
        await stack.auto.enable_free_plan(INTERNAL)
        refreshed = (await stack.channels.get(INTERNAL))["invite_link"]
        assert refreshed.endswith("fake810001")
        outcome = await stack.auto.handle_new_post(INTERNAL, 40)
        assert outcome["reactions"]["ok"] == 2, outcome

    async def test_a_wrong_link_is_reprompted_not_failed(self):
        """Typing "Test Message" where a post link belongs must re-ask."""
        stack, channel = await private_setup(2)
        event = FakeEvent(sender_id=USER, chat_id=USER, data="u:free_react")
        await stack.router.handle_callback(event)
        typed = FakeEvent(sender_id=USER, chat_id=USER, text="Test Message")
        await stack.router.handle_message(typed)
        alerts = [a for a in typed.answers if a["alert"]]
        assert alerts and "not a Telegram link" in alerts[-1]["text"]
        # the panel is waiting again, so the user can simply send the real link
        assert stack.router.has_input(USER)
        good = FakeEvent(sender_id=USER, chat_id=USER, text="https://t.me/c/810001/30")
        await stack.router.handle_message(good)
        orders = await stack.orders.list_for_user(USER)
        assert orders, "a valid link still creates the order"
        assert not (orders[0].get("result") or {}).get("errors") or \
            "Unrecognised" not in str((orders[0].get("result") or {}).get("errors"))


@pytest.mark.asyncio
class TestAttributionWhenTelegramHidesTheAdder:
    """The production log: `Bot added to chat -1003852827894 by unknown`."""

    async def _add_event(self, stack, channel, owner_id_seen=None):
        from src.telbot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = stack.bot.tbot
        bot.active_clients = {}
        bot.services_ready = True
        bot.store = stack.store
        bot.panel_router = stack.router
        # the bot must be able to answer "_export_invite" like the real one
        async def export(channel_id):
            return "https://t.me/+guess810001"
        bot._export_invite = export
        bot._guess_channel_owner = TelegramBot._guess_channel_owner.__get__(bot)
        stack.bot.exported_invites.append("https://t.me/+guess810001")
        return bot

    async def test_the_creator_is_used_when_no_actor_is_reported(self):
        from tests.fakes import FakeTelegramClient

        channel = make_channel(CHANNEL_ID)
        client = FakeTelegramClient("bot", user_id=999, channels={CHANNEL_ID: channel})
        # the server reports exactly one human creator (the person who added us)
        client.creator_id = USER
        stack = await build_stack({}, bot_client=client)
        bot = await self._add_event(stack, channel)

        class AddedEvent:
            user_added = True
            user_kicked = False
            sender_id = None          # Telegram reports nothing at all
            chat_id = 0
            user_id = None

            async def get_chat(self_inner):
                return channel

            async def respond(self_inner, *args, **kwargs):
                return None

        event = AddedEvent()
        event.user_id = (await client.get_me()).id      # the bot itself
        await bot._chat_action_handler(event)

        doc = await stack.channels.get(INTERNAL)
        assert doc is not None
        assert doc["owner_id"] == USER, "the creator should own the channel"
        # and the panel of that user lists it right away
        owned = await stack.user_panel.channels_of(USER)
        assert [c["_id"] for c in owned] == [INTERNAL]

    async def test_several_admins_leave_it_unassigned(self):
        from tests.fakes import FakeTelegramClient

        channel = make_channel(CHANNEL_ID)
        client = FakeTelegramClient("bot", user_id=999, channels={CHANNEL_ID: channel})
        client.admin_ids = [USER, USER + 1]          # no creator, two admins
        stack = await build_stack({}, bot_client=client)
        bot = await self._add_event(stack, channel)

        class AddedEvent:
            user_added = True
            user_kicked = False
            sender_id = None
            chat_id = 0
            user_id = None

            async def get_chat(self_inner):
                return channel

            async def respond(self_inner, *args, **kwargs):
                return None

        event = AddedEvent()
        event.user_id = (await client.get_me()).id
        await bot._chat_action_handler(event)

        doc = await stack.channels.get(INTERNAL)
        assert not doc.get("owner_id"), "ambiguous case must stay assignable by hand"
        # the admins were told, with an assign hint
        texts = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("could not tell who added me" in t.lower() for t in texts)
