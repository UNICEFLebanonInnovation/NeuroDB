"""Shared pieces of the education dashboards: KPI tile icon, sprite icons, chart builders, the map module."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template.loader import render_to_string

from neurodb.web.templatetags.ui import ASSETS, asset_urls

WEB = Path(settings.BASE_DIR) / "neurodb" / "web"
STATIC = WEB / "static"
NEW_ICONS = [
    "child",
    "school",
    "teacher",
    "accessible",
    "briefcase",
    "ring",
    "chat",
    "tent",
    "syringe",
    "ruler",
    "bowl",
    "shield-plus",
    "id-card",
    "route",
    "pin",
]


def test_kpi_tile_without_icon_is_unchanged():
    html = render_to_string("components/kpi_tile.html", {"label": "Children", "value": "1,234", "hint": "x"})
    assert '<div class="kpi">' in html
    assert "kpi__icon" not in html and "kpi__body" not in html
    assert '<div class="kpi__label">Children</div>' in html and "1,234" in html


def test_kpi_tile_with_icon():
    html = render_to_string(
        "components/kpi_tile.html",
        # a "class" in the page's context must not reach the icon
        {
            "label": "Children enrolled",
            "value": "39,445",
            "icon": "child",
            "status": "on_track",
            "class": "x",
        },
    )
    assert '<div class="kpi kpi--on_track kpi--icon">' in html
    icon, body = html.split('<div class="kpi__body">')
    assert '<span class="kpi__icon" aria-hidden="true">' in icon
    assert '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-child"></use></svg>' in icon
    assert "Children enrolled" in body and "39,445" in body
    assert html.count("<div") == html.count("</div>")


def test_sprite_has_the_new_icons_once_each():
    sprite = render_to_string("components/icons.html")
    ids = re.findall(r'<symbol id="i-([\w-]+)" viewBox="0 0 24 24">', sprite)
    assert len(ids) == len(set(ids)) == sprite.count("<symbol ")
    assert set(NEW_ICONS) <= set(ids)


def test_edumap_module_is_registered():
    assert ASSETS["eduMapModule"] == "js/edumap.js"
    assert (STATIC / ASSETS["eduMapModule"]).is_file()
    assert '"eduMapModule": "/static/js/edumap.js"' in asset_urls()
    app = (STATIC / "js" / "app.js").read_text()
    assert re.search(r"const MODULES = \{[^}]*\bedumap: \"eduMapModule\"", app)


def test_edumap_uses_only_hosts_the_csp_allows():
    """The map loads tiles from OpenStreetMap only: the one outside host the policy allows."""
    from django.http import HttpResponse
    from django.test import RequestFactory

    from neurodb.web.middleware import ContentSecurityPolicyMiddleware

    policy = ContentSecurityPolicyMiddleware(lambda request: HttpResponse())(RequestFactory().get("/"))[
        "Content-Security-Policy"
    ]
    source = (STATIC / "js" / "edumap.js").read_text()
    hosts = set(re.findall(r"https?://([^/\"'{]+)", source))
    assert hosts == {"tile.openstreetmap.org"}
    assert all(f"https://{host}" in policy for host in hosts)
    assert "eval(" not in source and "new Function" not in source


@pytest.mark.parametrize("builder", ["dist", '"share-bar"', "donut"])
def test_chart_builders_exist(builder):
    charts = (STATIC / "js" / "charts.js").read_text()
    assert re.search(rf"^  {re.escape(builder)}\(el, data\) \{{", charts, re.M)


def test_categorical_palette_in_both_themes():
    css = (STATIC / "css" / "app.css").read_text()
    blocks = re.findall(r"(:root|\[data-bs-theme=\"dark\"\]) \{([^}]*--nd-cat-1:[^}]*)\}", css)
    assert [selector for selector, _ in blocks] == [":root", '[data-bs-theme="dark"]']
    for _, body in blocks:
        tokens = dict(re.findall(r"--nd-cat-(\d+): (#[0-9a-f]{6})", body))
        assert sorted(tokens, key=int) == [str(i) for i in range(1, 11)]
        assert len(set(tokens.values())) == 10
    assert ".kpi__icon" in css and ".map-shell--compact" in css
