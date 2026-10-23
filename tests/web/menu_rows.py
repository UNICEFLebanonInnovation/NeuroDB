"""The sidebar of a page as the menu tests read it: its rows with an icon, in order, and its titled groups."""

from __future__ import annotations

import re

ROW = re.compile(
    r'<a class="nav-item-link([^"]*)" href="([^"]*)"([^>]*)>'
    r'<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-([\w-]+)"></use></svg>\s*'
    r"<span>([^<]+)</span>(.*?)</a>",
    re.S,
)


def sidebar(html: str) -> str:
    return html.split('id="sidebar"', 1)[1].split("</nav>", 1)[0]


def rows(html: str) -> list[dict]:
    """The menu's rows with an icon (the archive's own rows have none), in order."""
    return [
        {
            "css": css.split(),
            "href": href.replace("&amp;", "&"),
            "attrs": attrs,
            "icon": icon,
            "label": label.replace("&#x27;", "'"),
            "rest": rest,
        }
        for css, href, attrs, icon, label, rest in ROW.findall(sidebar(html))
    ]


def groups(html: str) -> list[tuple[str, str]]:
    """(key, title) of every titled group, in order."""
    return re.findall(
        r'<div class="nav-group" role="group" aria-labelledby="nav-title-([\w-]+)">\s*'
        r'<div class="sidebar__section-title" id="nav-title-\1">([^<]+)</div>',
        sidebar(html),
    )
