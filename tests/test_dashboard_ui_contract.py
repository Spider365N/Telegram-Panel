"""Static contract of the brutalist dashboard: soft refresh + mobile CSS.

These are plain (non-async) checks so they run in milliseconds and catch the two
regressions the operator reported: a full-page auto refresh wiping an edit, and
a desktop-only layout on a phone.
"""
from __future__ import annotations


def test_dashboard_js_soft_refreshes_and_never_rerenders_forms():
    import pathlib

    source = pathlib.Path("src/webadmin.py").read_text(encoding="utf-8")
    js = source.split("BRUTAL_JS = r\"\"\"", 1)[1].split('"""', 1)[0]

    assert "function setRegion(" in js, "regions are patched in place"
    assert "el.dataset.h === fingerprint" in js, "unchanged regions must not be touched"
    assert "function liveTick(" in js
    assert "if (isEditing()) return setLiveState();" in js, \
        "auto refresh must pause while a form field is focused"
    assert "setInterval(liveTick" in js
    assert "setInterval(() => { if (S.auto) draw(); }" not in js, \
        "the old full-page auto refresh must be gone"
    # forms tabs have no live patcher at all
    for tab in ("plans", "pricing", "settings"):
        assert f"{tab}: async" not in js.split("const LIVE = {")[1].split("};")[0]
    # scroll preservation inside patched regions
    assert ".table-scroll\", el).map((box) => box.scrollTop)" in js
    # mobile card tables carry their labels
    assert 'data-label="${esc(col[1])}"' in js


def test_dashboard_css_is_mobile_first():
    import pathlib

    source = pathlib.Path("src/webadmin.py").read_text(encoding="utf-8")
    css = source.split('BRUTAL_CSS = r"""', 1)[1].split('"""', 1)[0]

    assert "@media (max-width:760px)" in css
    assert "@media (max-width:380px)" in css
    assert "table.mob td::before" in css, "stacked label/value cards on phones"
    assert "-webkit-overflow-scrolling:touch" in css
    assert "font-size:16px" in css, "16px inputs stop iOS from zooming the page"
    assert ".table-scroll{max-height" in css, "recent lists scroll instead of growing the page"
    assert "position:sticky" in css
    # dashboard lists are capped
    for marker in ("recent-orders", "recent-users"):
        assert marker in source
    assert "R.recentOrders" in source and "{scroll: true}" in source


def test_table_cells_render_html_instead_of_printing_it():
    """Regression: pre-built buttons/tags used to be escaped and shown as code.

    ``fmtCell`` escapes every plain value (so a channel title can not inject
    markup), but the cells built by the renderers themselves are wrapped in
    ``html(...)`` and must be inserted verbatim.
    """
    import pathlib
    import re

    source = pathlib.Path("src/webadmin.py").read_text(encoding="utf-8")
    js = source.split("BRUTAL_JS = r\"\"\"", 1)[1].split('"""', 1)[0]

    assert "const html = (markup) => ({[RAW]" in js
    assert "v[RAW] !== undefined) return v[RAW]" in js, "fmtCell must honour raw markup"

    # every cell that carries a widget has to be wrapped
    for field in ("actions", "plan_tag", "ban_tag", "status_tag", "reactions",
                  "views", "lock", "link", "on"):
        bare = re.findall(rf"^\s*{field}: `", js, flags=re.MULTILINE)
        assert not bare, f"{field} builds markup but is not wrapped in html()"
        assert re.search(rf"\b{field}: html\(", js), f"{field} must use html(...)"

    # user data still goes through esc() — no unescaped interpolation anywhere
    assert 'data-id="${esc(' in js
    assert "`<span class=\"tag red\">${esc(a.status)}</span>`" in js


def test_mobile_nav_sticks_below_the_real_header_height():
    import pathlib

    source = pathlib.Path("src/webadmin.py").read_text(encoding="utf-8")
    css = source.split('BRUTAL_CSS = r"""', 1)[1].split('"""', 1)[0]
    js = source.split("BRUTAL_JS = r\"\"\"", 1)[1].split('"""', 1)[0]

    assert "top:var(--hdr,52px)" in css, "the tab strip must follow the wrapping header"
    assert 'setProperty("--hdr"' in js
    assert 'window.addEventListener("resize", syncHeaderHeight)' in js
