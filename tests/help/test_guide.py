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
        (
            "How does NeuroDB count a completed programmatic visit?",
            ("field-monitoring", "which-visits-count"),
        ),
        ("Who can write a section comment on the HACT visit plan?", ("field-monitoring", "section-comments")),
        (
            "Why is the HACT visit plan different from the workbook?",
            ("field-monitoring", "why-a-figure-can-differ"),
        ),
    ],
)
def test_the_search_finds_the_sections_a_question_is_about(question, expected):
    found = [(s.page, s.slug) for s in guide.search(question)]
    assert expected in found, found


def test_the_field_monitoring_page_is_listed_after_monitoring_insights_and_named_for_its_page():
    from neurodb.help import assistant

    assert guide.ORDER.index("field-monitoring") == guide.ORDER.index("monitoring-insights") + 1
    assert guide.page("field-monitoring").title == "Field monitoring and the HACT visit plan"
    line = assistant.page_line(reverse("reports:monitoring"), "Field monitoring · NeuroDB")
    assert '"Field monitoring and the HACT visit plan" (/help/field-monitoring/)' in line
    text = GUIDE_TEXT["field-monitoring"]
    # the export rule the page states: the focal points are a count in the files, never names
    assert "unicef_focal_points_count" in text and "never their names" in text


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


# ------------------------------------------------------------------- Home and ActivityInfo (SPEC_HOME §10)
def test_home_has_its_page_after_the_overview_and_the_pages_map_to_their_guide():
    from neurodb.help import assistant

    assert guide.ORDER.index("home") == guide.ORDER.index("overview") + 1
    home = guide.page("home")
    assert home.title == "Home"
    assert [s.heading for s in home.sections if s.level == 2] == [
        "What Home shows",
        "Your section or every section",
        "ActivityInfo: in use or archive",
    ]
    assert guide.page("briefs-and-overview").title == "Country overview, management brief and daily review"
    for path, key in (
        ("/", "home"),
        ("/country-overview/", "briefs-and-overview"),
        ("/daily-review/", "briefs-and-overview"),
        ("/brief/", "briefs-and-overview"),
    ):
        assert f"(/help/{key}/)" in assistant.page_line(path, "A page · NeuroDB"), path


def test_the_help_assistant_knows_activityinfo_is_an_archive_of_past_years():
    from neurodb.help import assistant

    assert (
        "(eTools partners, programme documents, funds, assurance, field monitoring and action points, the "
        "country programme, Compiler figures, a knowledge base, and the ActivityInfo archive of past years)"
    ) in assistant.BASE_PROMPT
    assert "(ActivityInfo results, eTools partners" not in assistant.BASE_PROMPT


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Why is ActivityInfo an archive?", ("home", "activityinfo-in-use-or-archive")),
        ("What does Home show?", ("home", "what-home-shows")),
        ("Where is the daily review?", ("briefs-and-overview", "the-daily-review")),
    ],
)
def test_the_search_finds_home_and_the_archive(question, expected):
    found = [(s.page, s.slug) for s in guide.search(question)]
    assert expected in found, found


def test_the_guide_states_the_archive_rules_and_who_sets_them():
    text = " ".join(GUIDE_TEXT["home"].split())
    for words in (
        "switched on in Scheduled jobs and this year has ActivityInfo records from the last three months",
        "In January, last year counts instead",
        "**ActivityInfo: in use or archive** (under Reporting setup)",
        "A change shows on every page within a minute. Nothing is deleted either way.",
        "a year with ActivityInfo records keeps its ActivityInfo figures",
    ):
        assert words in text, words
    assert "Show on dashboard" not in text  # never offered as a way to drop a year's figures


def test_the_guide_lists_the_sidebar_as_it_is_drawn():
    """The sidebar of "Getting around NeuroDB" names the groups and rows of the menu (SPEC_HOME §4), in its
    order."""
    sidebar = guide.section("overview", "the-sidebar").text
    order = [
        "**Home**",
        "**For you**",
        "**Daily review**",
        "**What's new**",
        "**Ask NeuroDB**",
        "**Programme results**",
        "**Country overview**",
        "**Management brief**",
        "**Country programme**",
        "**Partner progress (eTools)**",
        "**Partner monitoring**",
        "**Progress reports**",
        "**Programme documents**",
        "**Partners**",
        "**Funds and assurance**",
        "**Funds**",
        "**Donors**",
        "**Assurance**",
        "**Field monitoring**",
        "**Monitoring\n  insights**",
        "**Action points**",
        "**Education and youth**",
        "**Makani (MSCC)**",
        "**Makani wellbeing**",
        "**Dirasa (Bridging)**",
        "**Youth\n  programmes**",
        "**Resources**",
        "**Knowledge base**",
        "**Periodic reports**",
        "**Document review**",
        "**Library**",
        "**Maps**",
        "**Population**",
        "**Data health**",
        "**Monthly results (ActivityInfo)**",
        "**ActivityInfo archive**",
        "**Help**",
    ]
    places = [sidebar.index(label) for label in order]
    assert places == sorted(places)
    for old in ("**Overview**", "**Partnerships**"):
        assert old not in sidebar, old


def test_the_refresh_table_says_the_activityinfo_import_follows_its_scheduled_job():
    """The import runs whenever its scheduled job is switched on, whatever the state (archived because its
    records stopped, or set Archived with the job left on); the forecasts job skips by itself (R1
    spec_shell-3). The Help assistant answers "when is X refreshed" from this table."""
    rows = {
        line.split("|")[1].strip(): line.split("|")[2].strip()
        for line in GUIDE_TEXT["overview"].splitlines()
        if line.startswith("| ") and line.count("|") == 3
    }
    assert rows["ActivityInfo data"] == (
        "18:00 on days 1 to 22 of each month, while its import is switched on in Scheduled jobs (see "
        "[ActivityInfo: in use or archive](/help/home/#activityinfo-in-use-or-archive))"
    )
    assert rows["Year-end forecasts"] == "Mondays at 07:15, while ActivityInfo is in use"


def test_the_guide_says_who_sees_the_set_up_rows():
    """Only superusers and staff Administrators (who can open the administration) see the greyed *Set up*
    rows (``roles.can_set_up``), not every administrator (R1 spec_shell-5)."""
    text = " ".join(guide.section("overview", "the-sidebar").text.split())
    assert "administrators who can open the administration see it greyed, marked *Set up*" in text
    assert "out of the sidebar; administrators see it greyed" not in text


def test_home_never_dates_the_end_of_activityinfo():
    """ActivityInfo may come back (the same section says how), and 2026 shows both sources: the guide names
    no year it stopped (R1 a11y_words-N-2)."""
    text = " ".join(GUIDE_TEXT["home"].split())
    assert "until 2026" not in text
    assert (
        "Partners reported their monthly results in ActivityInfo in past years and now report progress in "
        "eTools; the sidebar lists the years the archive holds."
    ) in text


# ------------------------------------------------------------- the guide and OPERATIONS.md say what is drawn
OPERATIONS = (guide.GUIDE_DIR.parents[2] / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
OPERATIONS_WORDS = " ".join(OPERATIONS.split())


def _operations_row(first_cell: str) -> str:
    """The table row of docs/OPERATIONS.md whose first cell is ``first_cell``."""
    rows = [line for line in OPERATIONS.splitlines() if line.startswith(f"| {first_cell} |")]
    assert len(rows) == 1, first_cell
    return rows[0]


def test_the_guide_and_operations_describe_homes_also_line_as_it_is_drawn():
    """With a section chosen, Home's visits are those to partners with a programme document in the
    sections and are not a link (Monitoring insights counts a section's visits another way); the
    third-party pair is not a link either, since the Field monitoring page shows the whole country. The
    guide had the two pages the other way round, and the Help assistant answers from it (R2 fixes-3)."""
    template = (
        guide.GUIDE_DIR.parents[1] / "reports" / "templates" / "reports" / "partials" / "_home_also.html"
    ).read_text(encoding="utf-8")
    assert "to partners with a programme document in" in template  # what the two texts below describe
    home = " ".join(GUIDE_TEXT["home"].split())
    assert "the Monitoring insights page shows the whole country" not in home
    assert "the Field monitoring page shows the whole country" in home
    assert "the visits are those to partners with a programme document in that section" in home
    assert "Monitoring insights counts a section's visits another way" in home
    assert "the visits to that section's partners" not in home
    row = _operations_row("*Where <who> stands in <year>*")
    assert '"to these partners"' not in row and "which show the whole country, are then not links" not in row
    assert "to partners with a programme document in <sections>" in row
    assert "Monitoring insights counts a section's visits another way" in row
    assert "the Field monitoring page shows the whole country" in row


def test_operations_pages_in_parts_names_home_and_the_daily_review():
    """The prose around the parts table still described the old site root: the overview's For you card,
    three requests after the overview's frame, a brief without a reporting year (R2 sweep-2)."""
    listed = OPERATIONS_WORDS[OPERATIONS_WORDS.index("These pages work this way:") :]
    listed = listed[: listed.index(". ")]
    assert "**Home**" in listed and "**daily review**" in listed and "**country overview**" in listed
    assert "the overview's *For you* card" not in OPERATIONS_WORDS
    assert OPERATIONS_WORDS.count("Home's *For you* card") == 2
    assert "three small requests" not in OPERATIONS_WORDS
    assert (
        "the overview: the tiles and the charts come with the frame and What's new follows it, one small "
        "request; Home: the key figures come with the frame, and *For you*, the decisions and the daily "
        "review, *Since* and *Programmes and sources* follow, four small requests"
    ) in OPERATIONS_WORDS
    brief = _operations_row("Management brief, `/brief/`")
    assert "without a reporting year" not in brief and "the empty state" not in brief.split("|")[2]


def test_operations_says_the_database_forecast_card_follows_the_overviews():
    """The database dashboard's forecast card shows only on this year's databases while ActivityInfo is in
    use, as the overview's (R2 sweep-1)."""
    assert "and on each database dashboard" not in OPERATIONS_WORDS
    assert (
        "card on the Country overview and on this year's database dashboards (this year only, while "
        "ActivityInfo is in use; the overview's for the chosen sections)"
    ) in OPERATIONS_WORDS
    assert "the Country overview's and this year's database dashboards' forecast card (this year only)" in (
        OPERATIONS_WORDS
    )


def test_operations_md_never_names_a_design_note_stage():
    """The design notes' stage labels ("fix A") are defined nowhere in OPERATIONS.md: it says what changed."""
    assert not re.search(r"\bfix [A-H]\b", OPERATIONS)


def test_the_guide_says_the_country_programme_is_added_in_the_administration():
    """The Country programme page has no upload: an administrator adds the cycle in the administration
    (its own empty state says so), and the Help assistant answers from this table (R2 sweep-3)."""
    text = " ".join(GUIDE_TEXT["overview"].split())
    assert "uploaded on the country programme page" not in text
    assert (
        "| Country programme | The results framework and its progress, added by an administrator in the "
        "administration (Country programme cycles) and shown on the country programme page. |"
    ) in text


def test_operations_states_the_briefs_rules_as_they_now_are():
    """The brief compares eTools over the same months of both years, guards the coverage both ways and
    projects each source from its own last month, never holding a month not reported flat (R2 fixes-1,
    brief-1, fixes-4: OPERATIONS.md is the docs group's, so it follows the brief's fixes here). R3 code-1,
    code-2 (changed): eTools is compared to the last month whose partner reports are all in, and a line not
    projected ends at its last reported month; the docs say so."""
    assert "flat (the total so far) to the current month" not in OPERATIONS_WORDS
    assert "this year's last month with a partner report" not in OPERATIONS_WORDS
    for words in (
        "eTools is compared over the same months of both years, from January to the last month whose "
        "partner reports are all in",
        "at least 90 % of the programme documents expected for it have sent theirs",
        "one whose reports stopped is no longer waited for two months after the quarter's end",
        "are still coming in (<n> programme documents so far, <m> in 2026)",
        '"yet" only while the year runs',
        'its period reads "2027 (not started)"',
        "a month a source has not reported is never held at the total so far",
        "a line ends at its last reported month, never drawn flat to December",
        "a key under the chart says what the solid line, the dotted line and the band are",
    ):
        assert words in OPERATIONS_WORDS, words
    guide_text = " ".join(GUIDE_TEXT["briefs-and-overview"].split())
    assert "while not even the first quarter's partner reports are all in" in guide_text
    assert "its line ends at its last reported month" in guide_text
