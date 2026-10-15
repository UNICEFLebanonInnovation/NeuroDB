"""Status text in app.css is readable in both themes (WCAG 1.4.3, 4.5:1 for small text; SPEC_HOME §11.5): the status
pills, the review's tags, the databases page's card note and the Watch chips draw their words in a text token
(``--nd-*-text``, or ``--nd-muted`` for the neutral ones), which is darker than the token used for fills and dots in
the light theme and the same in the dark one (review R1 a11y_words-A11Y-3: "Warning" 3.58:1, "Not in use (archive)"
4.06:1, "Critical" 4.38:1). The ratios are computed from the stylesheet's own tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings

CSS = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
STATES = ("success", "danger", "warning", "info", "neutral")


def _block(selector: str) -> str:
    """The rule whose whole selector is ``selector`` (at the start of a line, not in a list)."""
    found = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert found is not None, selector
    return found.group(1)


def _declarations(body: str) -> dict[str, str]:
    return {name.strip(): value.strip() for name, value in re.findall(r"([\w-]+)\s*:\s*([^;]+);", body)}


THEMES = {
    "light": _declarations(_block(":root")),
    "dark": {**_declarations(_block(":root")), **_declarations(_block('[data-bs-theme="dark"]'))},
}


def _colour(value: str, tokens: dict[str, str]) -> str:
    """A colour value, its ``var(--x)`` followed through the theme's tokens, as ``#rrggbb``."""
    for _ in range(5):
        found = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
        if not found:
            break
        value = tokens[found.group(1)]
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), value
    return value


def _ratio(fg: str, bg: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _pair(selector: str, fg_prop: str, bg_prop: str | None, theme: str, bg_default: str = "--nd-surface"):
    rules = _declarations(_block(selector))
    tokens = THEMES[theme]
    fg = _colour(rules[fg_prop], tokens)
    bg = _colour(rules[bg_prop] if bg_prop else f"var({bg_default})", tokens)
    return fg, bg


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("state", STATES)
def test_status_pills_are_readable(theme, state):
    fg, bg = _pair(f".pill--{state}", "--pill-fg", "--pill-bg", theme)
    assert _ratio(fg, bg) >= 4.5, (state, theme, fg, bg, round(_ratio(fg, bg), 2))
    # its words are on the page around it too (a pill sits on a card or the page)
    for page_bg in ("--nd-surface", "--nd-bg", "--nd-surface-2"):
        assert _ratio(fg, _colour(f"var({page_bg})", THEMES[theme])) >= 4.5, (state, theme, page_bg)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    "selector, fg_prop, bg_prop",
    [
        (".review-tag--new", "color", "background"),
        (".review-tag--still_open", "color", "background"),
        (".review-tag--resolved", "color", "background"),
        (".watch-chip--likely", "color", "background"),
        (".watch-chip--reason", "color", "background"),
        (".watch-chip--sure", "color", "background"),
        (".watch-chip--check", "color", "background"),
        (".watch-chip--closed", "color", "background"),
        (".db-card__note", "color", None),
        (".decide__status--raised", "--pill-fg", "--pill-bg"),
        (".decide__status--acknowledged", "--pill-fg", "--pill-bg"),
        (".decide__status--assigned", "--pill-fg", "--pill-bg"),
        (".decide__status--closed", "--pill-fg", "--pill-bg"),
    ],
)
def test_other_status_words_are_readable(theme, selector, fg_prop, bg_prop):
    fg, bg = _pair(selector, fg_prop, bg_prop, theme)
    assert _ratio(fg, bg) >= 4.5, (selector, theme, fg, bg, round(_ratio(fg, bg), 2))


def test_text_tokens_keep_fills_and_dots_as_they_were():
    """The text tokens are for words only: fills, dots and bars keep the brighter colours, and the dark theme's
    text tokens are its own status colours (it passed already)."""
    light, dark = THEMES["light"], _declarations(_block('[data-bs-theme="dark"]'))
    for state in ("success", "danger", "warning", "info"):
        assert f"--nd-{state}-text" in light and f"--nd-{state}-text" in dark, state
        assert _colour(dark[f"--nd-{state}-text"], THEMES["dark"]) == _colour(
            dark[f"--nd-{state}"], THEMES["dark"]
        )
    assert light["--nd-warning"] == "#b76e00" and light["--nd-warning-text"] == "#9a5b00"
    assert ".review-finding__sev--warning { background: var(--nd-warning); }" in CSS
