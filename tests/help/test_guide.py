"""The help guide (stage D2.1): its pages render with an anchor on every heading, its links lead to pages
that exist, its search finds the sections the starter questions are about, it states the rules that are
really switched on, it holds no command, address, secret or person; and the /help/ pages, the sidebar
link and the Help assistant's frame are there for every signed-in person but a donor."""

import re

import pytest
from django.urls import Resolver404, resolve, reverse
from django.utils.html import escape

from neurodb.help import guide
from tests.donors.test_donor_access import make_donor

pytestmark = pytest.mark.django_db

GUIDE_TEXT = {key: (guide.GUIDE_DIR / f"{key}.md").read_text(encoding="utf-8") for key in guide.ORDER}
LINK = re.compile(r"\]\((/[^)\s]*)\)")


def test_every_guide_file_is_listed_and_every_page_renders(client_viewer):
    files = sorted(p.stem for p in guide.GUIDE_DIR.glob("*.md"))
    assert files == sorted(guide.ORDER)
    for key in guide.ORDER:
        response = client_viewer.get(reverse("help:page", args=[key]))
        assert response.status_code == 200, key
        html = response.content.decode()
        found = guide.page(key)
        assert f'<h1 class="page-title">{escape(found.title)}</h1>' in html
        assert html.count("<h1") == 1  # the page header's; the Markdown title is left out
        for section in found.sections:
            if section.slug:
                assert f'id="{section.slug}"' in html, (key, section.slug)
                assert f'href="#{section.slug}"' in html  # its ¶ anchor
        assert 'aria-current="page"' in html
    overview = client_viewer.get(reverse("help:index"))
    assert overview.status_code == 200 and "Getting around NeuroDB" in overview.content.decode()
    assert client_viewer.get("/help/no-such-page/").status_code == 404


def test_the_guide_holds_no_command_address_secret_person_or_unplain_word():
    for key, text in GUIDE_TEXT.items():
        assert "manage.py" not in text, key
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), key  # no e-mail address
        assert not re.search(r"https?://", text), key  # no address of another site
        assert not re.search(r"\b(?:[a-z0-9]+\.)+(?:org|com|net|io|int)\b", text, re.I), key  # no host name
        assert not re.search(r"\b[A-Za-z0-9_-]{32,}\b", text), key  # nothing that looks like a key
        assert not re.search(r"\bsk-[A-Za-z0-9]", text), key
        assert not re.search(r"\b[A-Z][A-Z0-9]+_[A-Z0-9_]+\b", text), key  # no setting's name
        for word in ("LLM", "agent", "detector", "receipt", "item"):
            assert not re.search(rf"\b{word}s?\b", text, re.I), (key, word)
        assert not re.search(r"\brun (?:the )?command\b", text, re.I), key


def test_every_link_of_the_guide_leads_to_a_page_that_exists():
    for key, text in GUIDE_TEXT.items():
        for target in LINK.findall(text):
            path, _, anchor = target.partition("#")
            try:
                match = resolve(path)
            except Resolver404:
                pytest.fail(f"{key}: {target} is not a NeuroDB page")
            if match.view_name == "help:page":
                page = guide.page(match.kwargs["key"])
                assert page is not None, target
                if anchor:
                    assert guide.section(page.key, anchor) is not None, target


def test_the_rules_switched_on_are_the_ones_the_guide_lists():
    from neurodb.fmm.models import RuleSetting

    text = GUIDE_TEXT["monitoring-insights"]
    table = text.split("### The rules switched on for Lebanon", 1)[1].split("The other rules", 1)[0]
    listed = set(re.findall(r"^\| (R\d+) ", table, re.MULTILINE))
    assert listed == set(RuleSetting.objects.filter(enabled=True).values_list("code", flat=True))
    off = text.split("The other rules (", 1)[1].split(")", 1)[0]
    assert "R4" in off and "R9 to R18" in off and "R22" in off and "R24 to R31" in off


def test_the_guide_states_the_score_settings_and_urgency_as_seeded():
    from neurodb.fmm.models import ScoreSetting, default_categories

    s = ScoreSetting.load()
    text = GUIDE_TEXT["monitoring-insights"]
    for category in default_categories():
        assert f"| {category['label']} | {category['weight']} |" in text
    assert f"**High** from {s.band_high}, **Medium** from {s.band_medium}" in text
    weights = s.urgency_weights
    assert (
        f"{weights['quality_gap']:.2f} × (100 − quality score) + {weights['recency']:.2f} × recency + "
        f"{weights['red_flags']:.2f} × flags" in text
    )
    assert f"**Red** from {s.urgency_red}, **amber** from {s.urgency_amber}" in text
    assert f"0 at {s.recency_days} days" in text


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How is the quality score calculated?", ("monitoring-insights", "the-quality-score")),
        ("What does rule R7 check?", ("monitoring-insights", "the-rules-switched-on-for-lebanon")),
        ("Where does the urgency score come from?", ("monitoring-insights", "urgency")),
        ("Which job refreshes Monitoring insights, and when?", ("overview", "when-the-data-is-refreshed")),
        ("What does Not monitored mean?", ("monitoring-insights", "visits-statuses-and-ratings")),
        ("How do I connect Power BI?", ("exports", "power-bi-live-connection")),
    ],
)
def test_the_search_finds_the_sections_a_question_is_about(question, expected):
    found = [(s.page, s.slug) for s in guide.search(question)]
    assert expected in found, found


def test_the_search_page_lists_sections_with_their_links(client_viewer):
    html = client_viewer.get(reverse("help:index"), {"q": "urgency"}).content.decode()
    assert 'href="/help/monitoring-insights/#urgency"' in html and "Sections about" in html
    html = client_viewer.get(reverse("help:index"), {"q": "zzqx"}).content.decode()
    assert "No section of the guide matches these words" in html


def test_a_section_is_cut_for_the_search_and_links_lose_their_addresses():
    section = guide.section("monitoring-insights", "the-rules-switched-on-for-lebanon")
    text = section.plain(guide.SECTION_CHARS)
    assert len(text) <= guide.SECTION_CHARS + 2 and text.endswith("…")
    assert "](" not in guide.section("overview", "").plain()


def test_the_rendered_guide_keeps_only_links_to_neurodb():
    for key in guide.ORDER:
        html = guide.render(key)
        for href in re.findall(r'href="([^"]*)"', html):
            assert href.startswith(("/", "#")) and not href.startswith("//"), (key, href)
        assert "<script" not in html and "<img" not in html


# ------------------------------------------------------------------------------------------ the shell
def test_the_help_link_and_the_panel_frame_are_on_every_page_for_staff(client_viewer):
    for name in ("reports:overview", "help:index", "reports:action_points"):
        html = client_viewer.get(reverse(name)).content.decode()
        assert 'id="help-panel"' in html and 'data-src="/help/panel/"' in html, name
        assert 'id="help-trigger"' in html and 'aria-controls="help-panel"' in html, name
        assert 'href="/help/"' in html and "nav-item-link--help" in html, name
        assert 'role="dialog"' in html and 'aria-modal="true"' in html
        assert (
            "<script>" not in html.split('id="help-panel"', 1)[1].split("</section>", 1)[0]
        )  # CSP: none inline


def test_donors_get_no_help(client, db, roles):
    donor = make_donor()
    client.force_login(donor.user)
    html = client.get(reverse("donors:page")).content.decode()
    assert 'id="help-panel"' not in html and 'id="help-trigger"' not in html and "/help/" not in html
    assert client.get(reverse("help:index"))["Location"] == reverse("donors:page")
    assert client.get(reverse("help:page", args=["overview"]))["Location"] == reverse("donors:page")
    refused = client.post(reverse("help:stream"), {"question": "How is the quality score calculated?"})
    assert refused.status_code == 403 and refused.json() == {"detail": "Not available for donor accounts."}
    assert client.get(reverse("help:panel"), HTTP_HX_REQUEST="true").status_code == 403


def test_signed_out_people_are_sent_to_sign_in(client):
    response = client.get(reverse("help:index"))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]
    html = client.get(reverse("landing")).content.decode()
    assert 'id="help-panel"' not in html
