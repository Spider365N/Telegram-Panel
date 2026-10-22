"""
Regression tests for the entity resolution crash reported in production:

    Error applying reaction: Could not find the input entity for
    PeerChannel(channel_id=3852827894) … extra: https://t.me/c/3852827894/22

The fix lives in src/peers.py (access-hash cache + resolution ladder + join) and
is wired into src/utils.resolve_entity.
"""
from __future__ import annotations

import pytest

from src.peers import PeerCache, PeerResolver, parse_link
from src.utils import resolve_entity
from tests.fakes import FakeTelegramClient, make_channel


class TestLinkParsing:
    def test_private_link(self):
        info = parse_link("https://t.me/c/3852827894/22")
        assert info.kind == "private"
        assert info.chat_id == -1003852827894
        assert info.message_id == 22

    def test_private_topic_link_uses_last_segment_as_message(self):
        info = parse_link("https://t.me/c/3852827894/17/99")
        assert info.chat_id == -1003852827894
        assert info.topic_id == 17
        assert info.message_id == 99

    def test_public_link(self):
        info = parse_link("https://t.me/mychannel/123")
        assert info.kind == "public"
        assert info.username == "mychannel"
        assert info.message_id == 123

    def test_public_topic_link(self):
        info = parse_link("t.me/mychannel/5/77")
        assert info.username == "mychannel"
        assert info.topic_id == 5
        assert info.message_id == 77

    def test_invite_links(self):
        assert parse_link("https://t.me/+AbC123").invite_hash == "AbC123"
        assert parse_link("https://t.me/joinchat/AbC123").invite_hash == "AbC123"
        assert parse_link("+AbC123").kind == "invite"

    def test_bare_ids_and_usernames(self):
        assert parse_link("-1001234567890").chat_id == -1001234567890
        assert parse_link("1234567890").chat_id == -1001234567890
        assert parse_link("@somechannel").kind == "unknown"  # handled by Telethon
        assert parse_link("somechannel").username == "somechannel"

    def test_garbage(self):
        assert parse_link("").kind == "unknown"
        assert parse_link("hello world").kind == "unknown"


@pytest.mark.asyncio
class TestPeerResolver:
    async def test_private_link_resolves_after_learning_access_hash(self):
        """The exact production failure: cold session, private channel link."""
        channel = make_channel(3852827894, title="Private")
        client = FakeTelegramClient("acct", channels={3852827894: channel})
        resolver = PeerResolver(store=None)

        # get_entity has no cached entity, so resolution must go through
        # GetChannelsRequest and succeed instead of raising.
        entity, message_id = await resolver.resolve_message_link(client, "https://t.me/c/3852827894/22")
        assert message_id == 22
        assert entity.id == 3852827894
        assert any(type(r).__name__ == "GetChannelsRequest" for r in client.requests)

    async def test_unknown_private_channel_joins_via_invite(self):
        channel = make_channel(3852827894, title="Invite Only")
        client = FakeTelegramClient("acct", channels={3852827894: channel}, member_of=set())
        resolver = PeerResolver(store=None)

        entity = await resolver.resolve(
            client, None, chat_id=-1003852827894, invite_hash="AbC123", join=True,
        )
        assert entity.id == 3852827894
        assert "AbC123" in client.joined

    async def test_resolution_failure_is_explicit(self):
        client = FakeTelegramClient("acct", channels={}, member_of=set())
        resolver = PeerResolver(store=None)
        with pytest.raises(ValueError):
            await resolver.resolve(client, None, chat_id=-1009999999999)

    async def test_username_resolution(self):
        channel = make_channel(123, username="my_public")
        client = FakeTelegramClient("acct", channels={123: channel})
        resolver = PeerResolver(store=None)
        entity = await resolver.resolve(client, "my_public")
        assert entity.username == "my_public"

    async def test_dialog_refresh_populates_cache(self):
        channel = make_channel(777, title="From dialogs")
        client = FakeTelegramClient("acct", channels={777: channel})
        resolver = PeerResolver(store=None)
        count = await resolver.refresh_dialogs(client, force=True)
        assert count == 1
        # after the refresh plain get_entity works
        assert (await client.get_entity(-1000000000777)).id == 777


@pytest.mark.asyncio
class TestAccessHashCachePersistence:
    async def test_hashes_are_stored_and_reused(self):
        from tests.fakes import build_stack
        stack = await build_stack()
        channel = make_channel(555, title="Cached")
        client = FakeTelegramClient("phone1", channels={555: channel})
        cache = PeerCache(stack.store)

        await cache.put(client, channel)
        stored = await stack.store.find_one("peers", {"peer_id": 555})
        assert stored and stored["access_hash"] == channel.access_hash

        # a new cache instance (simulating a restart) reads it back
        fresh = PeerCache(stack.store)
        assert await fresh.get(client, 555) == channel.access_hash


@pytest.mark.asyncio
class TestResolveEntityIntegration:
    async def test_resolve_entity_uses_peer_resolver_fallback(self):
        """utils.resolve_entity must recover instead of surfacing Telethon's error."""
        from src.peers import get_peer_resolver, reset_peer_resolver
        reset_peer_resolver()
        channel = make_channel(3852827894, title="Fix target")
        client = FakeTelegramClient("acct", channels={3852827894: channel})
        entity = await resolve_entity(-1003852827894, client)
        assert entity.id == 3852827894

    async def test_resolve_entity_keeps_revoked_session_semantics(self):
        from telethon.errors import SessionRevokedError
        from unittest.mock import AsyncMock
        account = AsyncMock()
        account.get_entity = AsyncMock(side_effect=SessionRevokedError(None))
        with pytest.raises(SessionRevokedError):
            await resolve_entity("username", account)

    async def test_resolve_entity_passthrough_entity(self):
        sentinel = object()
        assert await resolve_entity(sentinel, None) is sentinel
