"""Hardening (stage 8b): a sweep of the words staff read and of the template comments.

Staff-facing text never says "item", "items", "agent", "detector", "receipt" or "LLM" (D8). The page
tests check the dashboard; this sweep reads every other place Monitoring insights writes for people:
the brief card, the chat panel, the drill-down windows of every kind, the visit pages, the look-up, the
partner and programme document panels, the overview, assurance and action points pages that link in,
every admin page of the group (with the prompts' fixed safety text left out: it names those words to
forbid them), the CSV header, the knowledge hub's visits and NeuroDB Watch's records. Text that comes
from eTools or from the AI is marked ``data-synced`` and left out, as in the page tests.

It also reads the sources: the literal text of every Monitoring insights template, and every string the
Python code marks for translation (what reaches a page), apart from the AI's fixed instructions; and it
checks that no template sends an HTML comment to the browser or holds a ``{# #}`` comment over two lines
(``{% comment %}`` is for those)."""

from __future__ import annotations

import ast
import datetime
import re
from pathlib import Path

import pytest
from django.test import override_settings
from django.urls import reverse

from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit

from .test_pages import FORBIDDEN, visible

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
FMM = Path(__file__).resolve().parents[2] / "neurodb" / "fmm"
# The AI's own instructions name the forbidden words to forbid them; administrators read them greyed out
AI_INSTRUCTIONS = {FMM / "ai" / "prompts.py"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)
    monkeypatch.setattr("neurodb.reports.views._this_year", lambda: 2026)


def _words(html: str) -> str:
    """The words a reader sees: the main part of a page (or the whole fragment), without scripts,
    synced text, chart data and the prompts' fixed text."""
    if '<main id="main"' in html:
        html = html.split('<main id="main"', 1)[1]
    html = re.sub(r'<pre class="nd-fixed__text[^"]*">.*?</pre>', " ", html, flags=re.S)
    return " ".join(visible(html).split())


def _clean(where: str, html: str) -> None:
    text = _words(html)
    found = FORBIDDEN.search(text)
    assert not found, (
        f"{where}: {found.group(0)!r} in …{text[max(found.start() - 60, 0) : found.end() + 60]}…"
    )
    assert not re.search(r"\b(None|nan)\b", text), where


def _get(client, url: str, params: dict | None = None, hx: bool = False) -> str:
    response = client.get(url, params or {}, **({"HTTP_HX_REQUEST": "true"} if hx else {}))
    assert response.status_code == 200, (url, params, response.status_code)
    return response.content.decode()


# ------------------------------------------------------------------------------------------ the pages
def test_no_forbidden_word_on_any_page_staff_read(built, fm_world, client, admin_user, viewer):
    client.force_login(viewer)
    page = reverse("fmm:dashboard")
    base = {"year": "2026", "section": ""}
    pages = {
        "brief card (AI off)": _get(client, reverse("fmm:insights"), base, hx=True),
        "insights tab": _get(client, page, {**base, "tab": "insights"}),
        "visits tab, sorted by quality": _get(client, page, {**base, "tab": "visits", "sort": "quality"}),
        "lookup miss": _get(client, reverse("fmm:lookup"), {"id": "FM-1999"}, hx=True),
        "places, every row": _get(client, page, {**base, "tab": "analysis", "places": "all"}),
        "partner page": _get(client, reverse("reports:partner_profile", args=[fm_world.partners["amel"].pk])),
        "programme document page": _get(
            client, reverse("reports:programme_detail", args=[fm_world.pds["amended"].pk])
        ),
        "action points of a visit": _get(
            client, reverse("reports:action_points"), {"module": "fm", "visit": "1722"}
        ),
    }
    hit = client.get(reverse("fmm:lookup"), {"id": "1722"}, HTTP_HX_REQUEST="true")
    assert hit.status_code == 204 and hit["HX-Redirect"] == reverse("fmm:visit", args=["1722"])
    for key in Visit.objects.values_list("key", flat=True):
        pages[f"visit {key}"] = _get(client, reverse("fmm:visit", args=[key]))
        pages[f"visit {key} window"] = _get(client, reverse("fmm:visit", args=[key]), hx=True)
    drills = (
        {"month": "2026-05"},
        {"hact_q1": "constrained"},
        {"bucket": "80-100"},
        {"bucket": "none"},
        {"flag": "R4"},
        {"flags": "3+"},
        {"urgency": "red"},
        {"rating": "off_track"},
        {"status": "reported"},
        {"office": "none"},
        {"section": "Education"},
        {"location": "30"},
        {"issue": "R1:missing:narrative"},
        {"rule": "R2", "rule_state": "na"},
        {"review": "none"},
    )
    for drill in drills:
        pages[f"drill {drill}"] = _get(client, reverse("fmm:drill"), {**base, **drill}, hx=True)
    with override_settings(FMM_AI=True, AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="x"):
        pages["chat panel (AI on)"] = _get(client, page, {**base, "tab": "insights"})
        pages["brief card (AI on)"] = _get(client, reverse("fmm:insights"), base, hx=True)
    for where, html in pages.items():
        _clean(where, html)


def test_no_forbidden_word_in_the_admin_pages(built, client, admin_user):
    from neurodb.fmm.ai import profiles
    from neurodb.fmm.models import FieldMapping, RuleSetVersion

    client.force_login(admin_user)
    version = profiles.published()
    visit = Visit.objects.get(key="1722")
    urls = [
        reverse("admin:index"),
        reverse("admin:fmm_fieldmapping_changelist"),
        reverse("admin:fmm_fieldmapping_change", args=[FieldMapping.objects.first().pk]),
        reverse("admin:fmm_questions"),
        reverse("admin:fmm_rulesetting_changelist"),
        reverse("admin:fmm_rulesetting_change", args=["R6"]),
        reverse("admin:fmm_scoresetting_change", args=[1]),
        reverse("admin:fmm_rulesetversion_changelist"),
        reverse("admin:fmm_rulesetversion_change", args=[RuleSetVersion.objects.first().pk]),
        reverse("admin:fmm_promptversion_changelist"),
        reverse("admin:fmm_promptversion_change", args=[version.pk]),
        reverse("admin:fmm_promptversion_preview", args=[version.pk]),
        reverse("admin:fmm_modelcapability_changelist"),
        reverse("admin:fmm_insight_changelist"),
        reverse("admin:fmm_chatquestion_changelist"),
        reverse("admin:fmm_visit_changelist"),
        reverse("admin:fmm_visit_change", args=[visit.pk]),
        reverse("admin:fmm_visitreview_changelist"),
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        _clean(url, response.content.decode())


def test_no_forbidden_word_in_the_csv_the_hub_or_watch(built, client, viewer):
    from neurodb.graph.models import Entity
    from neurodb.watch.models import WatchItem
    from tests.fmm.test_links import _pass
    from tests.graph.conftest import build_hub

    client.force_login(viewer)
    csv = client.get(reverse("fmm:visits"), {"year": "2026", "section": "", "export": "csv"}).content.decode()
    header = csv.splitlines()[0]
    assert not FORBIDDEN.search(header), header
    build_hub()
    for entity in Entity.objects.filter(kind=Entity.Kind.FM_VISIT):
        assert not FORBIDDEN.search(" ".join([entity.name, entity.description])), entity.name
    _pass(TODAY)
    items = WatchItem.objects.filter(detector="fm_follow_up")
    assert items.exists()
    for item in items:
        assert not FORBIDDEN.search(f"{item.title} {item.detail}"), item.title


# ------------------------------------------------------------------------------------------ the sources
def _template_text(source: str) -> str:
    """The literal text of a template: no comments, tags, variables or attributes' template code."""
    source = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", " ", source, flags=re.S)
    source = re.sub(r"\{#.*?#\}", " ", source)
    source = re.sub(r"\{\{.*?\}\}", " ", source, flags=re.S)
    # keep what {% trans "…" %} and {% blocktrans %} write, drop every other tag
    source = re.sub(r"\{%\s*trans\s+\"([^\"]*)\"[^%]*%\}", r" \1 ", source)
    source = re.sub(r"\{%\s*trans\s+'([^']*)'[^%]*%\}", r" \1 ", source)
    source = re.sub(r"\{%.*?%\}", " ", source, flags=re.S)
    source = re.sub(r"<(script|style)\b.*?</\1>", " ", source, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", source)


def _templates() -> list[Path]:
    return sorted(FMM.glob("templates/**/*.html"))


def test_no_forbidden_word_in_the_template_sources():
    found = [
        f"{path.relative_to(FMM)}: {m.group(0)}"
        for path in _templates()
        for m in FORBIDDEN.finditer(_template_text(path.read_text("utf-8")))
    ]
    assert found == []


def _translated_strings(path: Path) -> list[tuple[int, str]]:
    """The strings passed to gettext (``_``, ``gettext``, ``gettext_lazy``, ``ngettext``) in ``path``."""
    names = {"_", "gettext", "gettext_lazy", "ngettext", "ngettext_lazy", "pgettext"}
    out = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in names:
            out += [
                (node.lineno, arg.value)
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
    return out


def test_no_forbidden_word_in_the_text_the_code_writes_for_pages():
    files = [p for p in sorted(FMM.rglob("*.py")) if "migrations" not in p.parts and p not in AI_INSTRUCTIONS]
    found = [
        f"{path.relative_to(FMM)}:{line}: {text!r}"
        for path in files
        for line, text in _translated_strings(path)
        if FORBIDDEN.search(text)
    ]
    assert found == []
    assert sum(len(_translated_strings(p)) for p in files) > 200  # the sweep did read the code's texts


def test_the_templates_send_no_html_comment_and_keep_comments_on_one_line():
    html_comments = [str(p.relative_to(FMM)) for p in _templates() if "<!--" in p.read_text("utf-8")]
    assert html_comments == [], "use {# #} or {% comment %}: an HTML comment reaches the browser"
    open_comment = re.compile(r"\{#(?:(?!#\}).)*\n", re.S)
    broken = [str(p.relative_to(FMM)) for p in _templates() if open_comment.search(p.read_text("utf-8"))]
    assert broken == []
