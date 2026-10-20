"""The public landing page is readable and its keyboard focus visible (WCAG 1.4.3: 4.5:1, 3:1 for large text;
1.4.11: 3:1 for the focus indicator). The design's own colours failed on its light theme (white on its cyan
#1CABE2 is 2.6:1, its pale grey notes about 3:1, its yellow focus ring on white 1.4:1); landing.css's NeuroDB
block keeps the cyan surfaces and gives them a navy ink, deepens the light-cyan labels and the grey notes, and
draws a navy ring around a yellow one. The ratios are computed from the stylesheet's and the template's own
values, against the backgrounds each sits on (the lightest and the darkest stop of a gradient)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings

WEB = Path(settings.BASE_DIR) / "neurodb" / "web"
CSS = (WEB / "static" / "css" / "landing.css").read_text()
TEMPLATE = (WEB / "templates" / "landing.html").read_text()

# the page's backgrounds (landing.css, the design's light theme)
WHITE = "#ffffff"
LIGHT = {
    "white": WHITE,
    "hero, darkest stop": "#cdeffb",
    "hero, lightest stop": "#f7fcff",
    "access section": "#f1fafe",
    "journey, darkest stop": "#e5f6fc",
    "data section, darkest stop": "#e9f8fe",
    "evidence box": "#edf9fe",
    "insight evidence card": "#f5fbfe",
    "trust boxes": "#f9fcfe",
    "mock screen": "#f8fbfc",
    "footer": "#e8f6fc",
}
CYAN = {
    "cyan (controls, ribbon, ending band)": "#1cabe2",
    "ending band, light stop": "#65ceec",
    "AI insight card, darkest stop": "#0d95c5",
    "NeuroDB node, rim": "#1499cf",
    "story picture panel, rim": "#159dc9",
}
PHOTO_SHADE = "#052d3d"  # the shade on the photographs, under their credits and captions


def _ratio(fg: str, bg: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _token(name: str) -> str:
    """The last value landing.css gives the custom property ``name``."""
    values = re.findall(re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{6})", CSS)
    assert values, name
    return values[-1].lower()


def _rule(selector: str) -> str:
    """The bodies of every rule whose selector list is exactly ``selector``, one after the other."""
    found = re.findall(r"(?:^|[}\n{])" + re.escape(selector) + r"\{([^}]*)\}", CSS)
    assert found, selector
    return ";".join(found)


@pytest.mark.parametrize("background", sorted({**LIGHT, **CYAN}.items()), ids=lambda b: b[0])
def test_the_focus_ring_shows_on_every_background(background):
    ring, halo = _token("--nd-ring"), _token("--nd-ring-halo")
    assert "button:focus-visible,a:focus-visible{outline:3px solid var(--nd-ring);" in CSS
    assert "box-shadow:0 0 0 6px var(--nd-ring-halo)}" in CSS
    assert ".stepbtn:focus-visible{outline-color:var(--nd-ring)}" in CSS
    _, colour = background
    assert _ratio(ring, colour) >= 3, background
    # on the photographs' dark shade, where the navy ring would vanish, the yellow one shows
    assert _ratio(halo, PHOTO_SHADE) >= 3 and _ratio(ring, halo) >= 3


@pytest.mark.parametrize("background", sorted(CYAN.items()), ids=lambda b: b[0])
def test_words_on_the_cyan_surfaces_take_a_navy_ink(background):
    ink = _token("--nd-ink")
    for selector in (
        ".navcta,.btn-yellow,.roundbtn.primary,.scenario-select button[aria-pressed=true],.insight-tab[aria-pressed=true]",
        ".ribbon strong,.ribbon span,.ending .eyebrow,.ending p,.stage-number,.stage-overline",
        ".corenode b,.corenode small,.corenode i,.corenode em",
    ):
        assert "color:var(--nd-ink)" in _rule(selector), selector
    assert "color:var(--nd-ink)" in CSS.split(".discovery-top b,.insight-eyeline,", 1)[1].split("}", 1)[0]
    assert _ratio(ink, background[1]) >= 4.5, background
    # the controls stay the design's cyan; hovered, a lighter one
    assert "background:#1CABE2" in _rule(".navcta") and "background:#1CABE2" in _rule(".btn-yellow")
    assert _ratio(ink, "#4cc2ec") >= 4.5 and "background:#4cc2ec" in CSS


@pytest.mark.parametrize("background", sorted(LIGHT.items()), ids=lambda b: b[0])
def test_labels_and_notes_on_the_light_sections_are_readable(background):
    _, colour = background
    accent, large, note = _token("--nd-accent"), _token("--nd-accent-large"), _token("--nd-note")
    assert _ratio(accent, colour) >= 4.5, ("labels", background)
    assert _ratio(note, colour) >= 4.5, ("notes", background)
    assert _ratio(large, colour) >= 3, ("large words", background)


def test_the_honesty_labels_and_the_design_labels_use_the_readable_colours():
    note_rule = CSS.split(".herofoot,.field-photo-disclaimer,", 1)[1].split("}", 1)[0]
    assert "color:var(--nd-note)" in note_rule
    for label in (".mini-label span", ".disclaimer", ".mutednote", ".mockitem small", ".trustbox p"):
        assert label in note_rule, label  # "FICTIONAL EXAMPLE", the story's disclaimer, the mock-ups' notes
    assert ".realcaveat{color:var(--nd-note)!important}" in CSS  # the design sets it with !important
    accent_rule = CSS.split(".hero-kicker,.narrative-chip small,", 1)[1].split("}", 1)[0]
    for label in (".field-photo-meta>a", ".data-universe .eyebrow", ".storyeyebrow", ".rolecard .roleno"):
        assert label in accent_rule, label
    assert "color:var(--nd-accent)" in accent_rule
    # the "02" of the premise on its amber tile (44px: large text)
    assert _ratio("#a86f00", "#fff4d7") >= 3 and ".pbox:nth-child(2) em{color:#a86f00}" in CSS


def test_the_eyebrows_coloured_in_the_template_are_readable():
    colours = re.findall(r'class="eyebrow[^"]*"[^>]*style="color:(#[0-9a-fA-F]{6})"', TEMPLATE)
    colours += re.findall(r'style="color:(#[0-9a-fA-F]{6})"[^>]*class="eyebrow', TEMPLATE)
    assert len(colours) == 7, colours
    for colour in colours:
        for name, background in LIGHT.items():
            assert _ratio(colour.lower(), background) >= 4.5, (colour, name)


def test_words_on_the_photographs_sit_on_a_deep_shade():
    """The credits ("Photo provided for NeuroDB design preview"), the story caption and its honesty label sit
    on the shade at the foot of each photograph: dark enough for white words over a white photograph."""
    assert ".field-photo-image:after{background:linear-gradient(transparent,#052d3dc4)}" in CSS
    assert "rgba(4,40,56,.78) 50%,rgba(4,40,56,.9))}" in CSS

    def over_white(colour: str, alpha: float) -> str:
        channels = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(c * alpha + 255 * (1 - alpha)):02x}" for c in channels)

    assert _ratio(WHITE, over_white("#052d3d", 0xC4 / 255)) >= 4.5
    assert ".story-photo-caption small,.story-portrait-copy .story-quote-label{color:#f4fcff}" in CSS
    assert _ratio("#f4fcff", over_white("#042838", 0.78)) >= 4.5
