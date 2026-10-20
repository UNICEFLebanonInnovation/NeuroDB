"""The eTools master indicators in the administration: who sees them, the Add PD indicators wizard, the actions on
the masters (accept and dismiss proposals, confirm suggestions, merge, change section, suggest now) and on their
links (confirm, reject, shown only), what the forms refuse, and the masters made from the ActivityInfo archive,
which is read and never changed. Generic names only: Partner A, PD/2026/001."""

from __future__ import annotations

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.test import RequestFactory
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import SECTION_EDITOR, VIEWER
from neurodb.datamart import curation, master_links, masters
from neurodb.datamart import models as dm
from neurodb.datamart.admin import EtoolsMasterAdmin, MasterLinkForm, MasterYearForm
from neurodb.indicators.models import Database, MasterIndicator, MasterSubIndicator, SubIndicator
from neurodb.integrations import background
from neurodb.watch.models import SectionMatch
from neurodb.web import admin_site

from . import masters_world as w

pytestmark = pytest.mark.django_db

ENROLLED = "# of children enrolled in NFE"
MASTERS = "admin:datamart_etoolsmaster_changelist"
LINKS = "admin:datamart_etoolsmasterlink_changelist"


@pytest.fixture
def admin_client(client, admin_user):
    """An Administrator (not a superuser), as the role gives it."""
    client.force_login(admin_user)
    return client


@pytest.fixture
def pds(db):
    """Running programme documents of Partners A, B and C: PD/2026/001 to PD/2026/003."""
    return {letter: w.programme(letter, n) for n, letter in enumerate("ABC", start=1)}


def staff(role: str) -> User:
    user = User.objects.create_user(username=f"staff-{role}", password="staff-pass-123456", is_staff=True)
    user.groups.add(Group.objects.get(name=role))
    return user


def said(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


def act(client, action: str, objects, url: str = MASTERS, **extra):
    """An action chosen on a list, for the objects ticked (``extra``: the page after it, applied)."""
    data = {"action": action, "_selected_action": [o.pk for o in objects], **extra}
    if "apply" not in extra:
        data["index"] = 0
    return client.post(reverse(url), data)


def links_of(master) -> dict[tuple[str, str], tuple[str, str]]:
    """A master's links: (PD number, ident) -> (role, status)."""
    return {
        (x.pd_number, x.ident): (x.role, x.status) for x in dm.EtoolsMasterLink.objects.filter(master=master)
    }


def wizard(client, master, **data):
    url = reverse("admin:datamart_etoolsmaster_add_indicators", args=[master.pk])
    return client.post(url, data) if data else client.get(url)


def offered(response) -> dict[str, str]:
    return dict(response.context["form"].fields["indicators"].choices)


# ------------------------------------------------------------------------------------------- who sees them
def test_administrators_see_the_masters_in_reporting_setup_and_other_staff_are_refused(
    admin_client, client, pds, monkeypatch
):
    setup = next(keys for name, _, keys in admin_site.GROUPS if str(name) == "Reporting setup")
    assert {"datamart.EtoolsMaster", "datamart.EtoolsMasterLink"} <= set(setup)
    trio = {"datamart.EtoolsMaster", "datamart.EtoolsMasterYear", "datamart.EtoolsMasterLink"}
    assert trio <= admin_site.DEVELOPER_ONLY_KEEP
    assert not any(admin_site._developer_only(key) for key in trio)
    assert admin_site._developer_only("datamart.PDIndicator")  # the replicated tables stay for developers

    html = admin_client.get(reverse("admin:index")).content.decode()
    assert reverse(MASTERS) in html and reverse(LINKS) in html
    assert reverse("admin:datamart_pdindicator_changelist") not in html
    m = w.master("Children enrolled in NFE", target=1000)
    for url in (reverse(MASTERS), reverse(LINKS), reverse("admin:datamart_etoolsmaster_change", args=[m.pk])):
        assert admin_client.get(url).status_code == 200, url
    page = admin_client.get(reverse("admin:datamart_etoolsmaster_change", args=[m.pk])).content.decode()
    assert "Add PD indicators" in page
    assert reverse("admin:datamart_etoolsmaster_add_indicators", args=[m.pk]) in page

    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    for role in (VIEWER, SECTION_EDITOR):
        client.force_login(staff(role))
        for url in (
            reverse(MASTERS),
            reverse(LINKS),
            reverse("admin:datamart_etoolsmaster_add_indicators", args=[m.pk]),
        ):
            assert client.get(url).status_code == 403, (role, url)
        assert act(client, "accept_proposals", [m]).status_code == 403
        client.post(reverse("admin:datamart_etoolsmaster_suggest_now"), {"_form_submitted": "on"})
    assert started == [] and dm.EtoolsMaster.objects.get(pk=m.pk).status == "active"


def test_the_list_shows_this_years_target_and_the_counts_and_filters_on_the_target(admin_client, pds):
    a, b = pds["A"], pds["B"]
    with_target = w.master("Children enrolled in NFE", target=1000, cp_output="1.1 Children access learning")
    w.link(with_target, a, 101, title=ENROLLED)
    w.link(with_target, b, 201, title=ENROLLED, status="suggested", unit_class="")
    without = w.master("Caregivers trained")
    dm.EtoolsMasterYear.objects.create(master=without, year=w.YEAR - 1, target=50)  # last year's only
    response = admin_client.get(reverse(MASTERS))
    rows = {m.pk: m for m in response.context["cl"].result_list}
    assert (rows[with_target.pk].confirmed_links, rows[with_target.pk].suggested_links) == (1, 1)
    assert rows[with_target.pk].year_target == 1000 and rows[without.pk].year_target is None
    html = response.content.decode()
    assert "1,000" in html and "No target" in html
    assert [m.pk for m in admin_client.get(reverse(MASTERS) + "?target=yes").context["cl"].result_list] == [
        with_target.pk
    ]
    assert [m.pk for m in admin_client.get(reverse(MASTERS) + "?target=no").context["cl"].result_list] == [
        without.pk
    ]
    assert "updated_by" not in str(response.context["cl"].list_display)


# --------------------------------------------------------------------------------------- Add PD indicators
@pytest.fixture
def education(pds):
    """Education's PD indicators of this year, and a closed programme document of 2025."""
    a, b, c = pds["A"], pds["B"], pds["C"]
    w.indicator(a, 101, ENROLLED, cp_output="1.1 Children access learning")
    w.indicator(a, 102, "% of children passing the exam", display_type="percentage")
    w.indicator(a, 103, "# of children reached with PSS", section=w.CP)  # another section
    w.indicator(a, 104, "# of youth enrolled in NFE")
    w.indicator(a, 105, "Days of school attended", display_type="text")  # a type nothing combines
    w.indicator(a, 106, "Ratio of girls to boys enrolled", display_type="ratio")
    w.indicator(b, 201, ENROLLED)
    w.indicator(b, 202, "# of boys enrolled in NFE")
    old = w.programme("D", 4, start=w.day(1, 1, 2024), end=w.day(12, 31, 2025), status="closed")
    w.indicator(old, 401, ENROLLED)  # not running this year
    other = w.master("Children in non-formal education")
    w.link(other, b, 201, title=ENROLLED)  # counts in another master's total
    return {"a": a, "b": b, "c": c, "other": other}


def test_the_wizard_offers_the_sections_indicators_that_can_join_and_none_with_a_home(
    admin_client, education
):
    a, b = education["a"], education["b"]
    m = w.master("Children enrolled in NFE")
    w.link(m, b, 202, title="# of boys enrolled in NFE", status="suggested", unit_class="")
    w.link(m, a, 104, title="# of youth enrolled in NFE", status="rejected")
    response = wizard(admin_client, m)
    assert response.status_code == 200
    found = offered(response)
    # numbers of Education, running this year, homeless; the one rejected here before too (a person may
    # change their mind), and said so
    assert set(found) == {f"{a.id}|s101", f"{b.id}|s202", f"{a.id}|s104"}
    assert found[f"{a.id}|s101"] == (
        f"{ENROLLED} (PD/2026/001 · Partner A · Number · CP output: 1.1 Children access learning)"
    )
    assert found[f"{b.id}|s202"].endswith("Suggested for this master")
    assert found[f"{a.id}|s104"].endswith(curation.REJECTED_BEFORE)
    roles = dict(response.context["form"].fields["role"].choices)
    assert roles == {"total": "In the total", "shown": "Shown only"}  # a sum has no numerators

    response = wizard(admin_client, m, role="total", indicators=[f"{a.id}|s101", f"{b.id}|s202"])
    assert response.status_code == 302
    assert "2 PD indicator(s) added, confirmed: they count from now on." in said(response)
    assert links_of(m) == {
        ("PD/2026/001", "s101"): ("total", "confirmed"),
        ("PD/2026/002", "s202"): ("total", "confirmed"),  # the suggestion became the confirmed link
        ("PD/2026/001", "s104"): ("total", "rejected"),
    }
    added = dm.EtoolsMasterLink.objects.get(master=m, ident="s101")
    assert (added.title, added.unit_class, added.confirmed_by, added.score) == (
        ENROLLED,
        "number",
        "admin",
        None,
    )
    assert set(offered(wizard(admin_client, m))) == {f"{a.id}|s104"}  # only the one rejected before
    fig = w.figure(m)
    assert {sub.link.ident for sub in fig.subs if sub.eligible} == {"s101", "s202"}


def test_a_percentage_master_offers_percentages_and_numbers_as_parts_only_where_they_count_elsewhere(
    admin_client, education
):
    a, b = education["a"], education["b"]
    pass_rate = w.master("Children passing the exam", "percent")
    response = wizard(admin_client, pass_rate)
    found = offered(response)
    # the percentage in the total, the numbers as numerators or denominators; never the ratio nor the text
    assert set(found) == {f"{a.id}|s101", f"{a.id}|s102", f"{a.id}|s104", f"{b.id}|s201", f"{b.id}|s202"}
    assert found[f"{b.id}|s201"].endswith(
        "In the total of Children in non-formal education: a numerator or a denominator only"
    )
    assert set(dict(response.context["form"].fields["role"].choices)) == {
        "total",
        "numerator",
        "denominator",
        "shown",
    }
    response = wizard(admin_client, pass_rate, role="total", indicators=[f"{a.id}|s102", f"{b.id}|s201"])
    assert "1 PD indicator(s) added, confirmed: they count from now on." in said(response)
    assert (
        "1 left out: PD/2026/002: # of children enrolled in NFE: A percentage master's total takes percentages "
        "(numbers count as numerators or denominators)." in said(response)
    )
    wizard(admin_client, pass_rate, role="denominator", indicators=[f"{b.id}|s201"])
    assert links_of(pass_rate) == {
        ("PD/2026/001", "s102"): ("total", "confirmed"),
        ("PD/2026/002", "s201"): ("denominator", "confirmed"),  # a number, the denominator here
    }


def test_a_pd_indicator_without_an_etools_id_is_added_and_counts(admin_client, education):
    """The wizard sends the ident a link stores (t and its title's sha1), which is taken as it is: never
    hashed again, which would find no indicator ("No longer in eTools")."""
    a = education["a"]
    title = "# of children enrolled in catch-up classes"
    w.indicator(a, None, title)
    w.report(a, None, title, "QPR1", w.day(3, 31), {"Akkar": 40})
    m = w.master("Children in catch-up classes")
    (key,) = [k for k in offered(wizard(admin_client, m)) if "|t" in k]
    pd_id, ident = key.split("|")
    assert curation.parse_key(key) == (a.id, ident) and len(ident) == 41
    response = wizard(admin_client, m, role="total", indicators=[key])
    assert "1 PD indicator(s) added, confirmed: they count from now on." in said(response)
    link = dm.EtoolsMasterLink.objects.get(master=m)
    assert (link.pd_id, link.ident, link.status, link.title) == (a.id, ident, "confirmed", title)
    sub = w.subs(w.figure(m))[ident]
    assert sub.counted and sub.value == 40.0


def test_parse_key_takes_a_links_ident_as_it_is_and_refuses_anything_else():
    code = masters.ident_code((7, "t# of children enrolled"))
    assert curation.parse_key(f"7|{code}") == (7, code)
    assert curation.parse_key("7|s101") == (7, "s101")
    for wrong in ("7|t# of children enrolled", "7|r55", "x|s101", "7|", "|s101", "7|s", f"7|{code}0"):
        assert curation.parse_key(wrong) is None, wrong


def test_add_leaves_out_what_cannot_join_and_says_why(education):
    a, b = education["a"], education["b"]
    m = w.master("Children enrolled in NFE")
    done = curation.add(m, [(a.id, "s102"), (a.id, "s103"), (a.id, "s999"), (b.id, "s201")], "total", "staff")
    assert done.added == 0
    assert [why for _what, why in done.left] == [
        "A sum adds numbers only: a percentage or a ratio can only be shown in it.",
        "It is in section Child Protection, not in Education.",
        masters.GONE,
        "It already counts in the total of Children in non-formal education.",
    ]
    averaged = w.master("Average enrolment", "average")
    assert curation.add(averaged, [(a.id, "s101")], "total", "staff").added == 1
    done = curation.add(averaged, [(a.id, "s102")], "total", "staff")  # every total of an average: one type
    assert done.left == [
        (
            "PD/2026/001: % of children passing the exam",
            "Every total of an average has one type: this master's are number.",
        )
    ]
    assert curation.add(averaged, [(a.id, "s101")], "shown", "staff").left[0][1] == curation.ALREADY_HERE


def test_a_viewer_cannot_open_the_wizard_and_its_page_says_what_it_does(admin_client, client, education):
    m = w.master("Children enrolled in NFE")
    html = wizard(admin_client, m).content.decode()
    assert "Add PD indicators to Children enrolled in NFE" in html
    assert "PD indicators of Education in the programme documents running in 2026" in html
    assert "They count at once: a person chose them." in html
    client.force_login(staff(VIEWER))
    assert wizard(client, m, role="total", indicators=[f"{education['a'].id}|s101"]).status_code == 403
    assert not dm.EtoolsMasterLink.objects.filter(master=m).exists()


# ---------------------------------------------------------------------------------------- the masters' actions
def test_accept_makes_proposals_active_with_their_links_confirmed_and_dismiss_keeps_them_dismissed(
    admin_client, education
):
    a, b = education["a"], education["b"]
    one = w.master(ENROLLED, status="proposed", origin="proposed", proposal_key="k1")
    w.link(one, a, 101, title=ENROLLED, status="suggested", unit_class="")
    two = w.master("# of youth enrolled in NFE", status="proposed", origin="proposed", proposal_key="k2")
    w.link(two, a, 104, title="# of youth enrolled in NFE", status="suggested", unit_class="")
    w.link(two, b, 202, title="# of boys enrolled in NFE", status="suggested", unit_class="", role="shown")
    response = act(admin_client, "accept_proposals", [one])
    assert "1 master indicator(s) accepted, 1 PD indicator(s) confirmed." in said(response)
    one.refresh_from_db()
    assert (one.status, one.updated_by) == ("active", "admin")
    assert links_of(one) == {("PD/2026/001", "s101"): ("total", "confirmed")}
    assert dm.EtoolsMasterLink.objects.get(master=one).unit_class == "number"  # its type when confirmed

    response = act(admin_client, "dismiss_proposals", [one, two])
    assert "1 proposal(s) dismissed: NeuroDB will not propose them again." in said(response)
    assert "1 were not proposals and were left as they were." in said(response)
    two.refresh_from_db()
    assert (two.status, two.proposal_key, links_of(two)) == ("dismissed", "k2", {})
    assert dm.EtoolsMaster.objects.get(pk=one.pk).status == "active"


def test_confirm_all_or_only_the_strong_suggestions_of_active_masters(admin_client, education):
    a, b = education["a"], education["b"]
    m = w.master("Children enrolled in NFE")
    strong = w.link(m, a, 101, title=ENROLLED, status="suggested", unit_class="")
    similar = w.link(m, b, 202, title="# of boys enrolled in NFE", status="suggested", unit_class="")
    dm.EtoolsMasterLink.objects.filter(pk=strong.pk).update(score=0.95)
    dm.EtoolsMasterLink.objects.filter(pk=similar.pk).update(score=0.7)
    proposal = w.master("# of youth enrolled in NFE", status="proposed", origin="proposed", proposal_key="k")
    w.link(proposal, a, 104, title="# of youth enrolled in NFE", status="suggested", unit_class="")
    response = act(admin_client, "confirm_strong", [m, proposal])
    assert "1 suggestion(s) confirmed: they count from now on." in said(response)
    assert any("1 proposed master indicator(s) skipped: accept a proposal" in text for text in said(response))
    assert links_of(m) == {
        ("PD/2026/001", "s101"): ("total", "confirmed"),
        ("PD/2026/002", "s202"): ("total", "suggested"),  # 0.7: a similar title waits for a person
    }
    assert links_of(proposal) == {("PD/2026/001", "s104"): ("total", "suggested")}
    act(admin_client, "confirm_all", [m])
    assert links_of(m)[("PD/2026/002", "s202")] == ("total", "confirmed")
    assert dm.EtoolsMasterLink.objects.get(pk=similar.pk).confirmed_by == "admin"


def test_merge_moves_the_links_and_the_missing_years_and_archives_the_others(admin_client, education):
    a, b = education["a"], education["b"]
    keep = w.master("Children enrolled in NFE", target=1000, code="1.1")
    w.link(keep, a, 101, title=ENROLLED)
    other = w.master("Children enrolled (NFE)", target=900, code="9.9")
    w.link(other, a, 101, title=ENROLLED, role="shown")  # held by the master kept already: dropped
    w.link(other, b, 202, title="# of boys enrolled in NFE")
    dm.EtoolsMasterYear.objects.create(master=other, year=2025, code="0.9", target=700)
    proposal = w.master(ENROLLED, status="proposed", origin="proposed", proposal_key="k")
    w.link(proposal, a, 104, title="# of youth enrolled in NFE", status="suggested", unit_class="")

    page = act(admin_client, "merge_selected", [keep, other, proposal])
    assert page.status_code == 200  # the choice of the one kept comes first
    html = page.content.decode()
    assert "Merge master indicators" in html and "These master indicators of Education become one." in html
    assert set(page.context["form"].fields["keep"].queryset) == {keep, other}  # active ones only
    assert dm.EtoolsMaster.objects.get(pk=other.pk).status == "active"  # nothing done yet

    response = act(admin_client, "merge_selected", [keep, other, proposal], apply="1", keep=keep.pk)
    assert response.status_code == 302
    assert (
        "2 master indicator(s) merged into Children enrolled in NFE: 2 PD indicator(s) moved, 1 it held already "
        "dropped, 1 year(s) of settings moved." in said(response)
    )
    assert links_of(keep) == {
        ("PD/2026/001", "s101"): ("total", "confirmed"),
        ("PD/2026/002", "s202"): ("total", "confirmed"),
        ("PD/2026/001", "s104"): ("total", "suggested"),
    }
    assert {(y.year, y.code, y.target) for y in keep.years.all()} == {(2026, "1.1", 1000), (2025, "0.9", 700)}
    other.refresh_from_db()
    proposal.refresh_from_db()
    assert (other.status, proposal.status) == ("archived", "dismissed")  # a proposal is never proposed again
    assert {(y.year, y.code) for y in other.years.all()} == {(2026, "9.9")}  # the year the one kept had stays


def test_merge_keeps_counting_a_total_the_kept_master_had_rejected(admin_client, education):
    """The kept master once rejected PD/2026/001's indicator, the merged one counts it in its total: after the
    merge it still counts (in the kept one), never nowhere."""
    a = education["a"]
    keep = w.master("Children enrolled in NFE")
    w.link(keep, a, 101, title=ENROLLED, status="rejected")
    other = w.master("Children enrolled (NFE)")
    w.link(other, a, 101, title=ENROLLED)
    done = curation.merge(keep, [other], "admin")
    assert (done.moved, done.dropped) == (1, 1)
    assert links_of(keep) == {("PD/2026/001", "s101"): ("total", "confirmed")}
    assert w.subs(w.figure(keep))["s101"].eligible
    # a link the kept master confirmed stays; the other's rejection of it is dropped
    again = w.master("Children enrolled again")
    w.link(again, a, 101, title=ENROLLED, status="rejected")
    curation.merge(keep, [again], "admin")
    assert links_of(keep) == {("PD/2026/001", "s101"): ("total", "confirmed")}


def test_merge_is_refused_across_sections_or_methods_and_changes_nothing(admin_client, education):
    m = w.master("Children enrolled in NFE")
    w.link(m, education["a"], 101, title=ENROLLED)
    elsewhere = w.master("Children reached with PSS", section=w.CP)
    rate = w.master("Children passing the exam", "percent")
    response = act(admin_client, "merge_selected", [m, elsewhere])
    assert (
        "Only master indicators of one section can be merged: these are in Child Protection and Education."
        in said(response)
    )
    response = act(admin_client, "merge_selected", [m, rate])
    assert any(
        "Only master indicators combined the same way can be merged" in text for text in said(response)
    )
    assert act(admin_client, "merge_selected", [m]).status_code == 302
    assert {x.status for x in dm.EtoolsMaster.objects.all()} == {"active"}
    assert links_of(m) == {("PD/2026/001", "s101"): ("total", "confirmed")}


def test_change_section_moves_masters_to_the_renamed_section_unless_it_has_their_name(
    admin_client, education
):
    renamed = "Education and Learning"
    w.indicator(education["c"], 301, ENROLLED, section=renamed)  # eTools renamed the section
    m = w.master("Children enrolled in NFE", target=1000)
    w.link(m, education["a"], 101, title=ENROLLED)
    clash = w.master("Out-of-school children")
    w.master("Out-of-school children", section=renamed)
    page = act(admin_client, "change_section", [m, clash])
    assert page.status_code == 200
    choices = dict(page.context["form"].fields["section"].choices)
    assert {renamed, w.EDUCATION, w.CP} <= set(choices)
    response = act(admin_client, "change_section", [m, clash], apply="1", section=renamed)
    assert f"1 master indicator(s) moved to {renamed}." in said(response)
    assert any(
        f"{renamed} already has a master indicator named Out-of-school children." in t for t in said(response)
    )
    m.refresh_from_db()
    assert (m.section, m.updated_by) == (renamed, "admin")
    assert links_of(m) == {("PD/2026/001", "s101"): ("total", "confirmed")}  # its links and year go with it
    assert m.years.get().target == 1000
    assert dm.EtoolsMaster.objects.get(pk=clash.pk).section == w.EDUCATION


def test_suggest_now_starts_the_job_in_the_background_once(admin_client, monkeypatch):
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    url = reverse("admin:datamart_etoolsmaster_suggest_now")
    assert admin_client.get(url).status_code == 200 and started == []  # a GET only shows the confirmation
    response = admin_client.post(url, {"_form_submitted": "on"})
    assert response.status_code == 302 and response.url == reverse(MASTERS)
    assert started == [("suggest_master_indicators", "--triggered-by", "admin")]
    assert any("being worked out in the background" in text for text in said(response))
    monkeypatch.setattr(background, "is_running", lambda job: True)
    response = admin_client.post(url, {"_form_submitted": "on"})
    assert len(
        started
    ) == 1 and "The eTools master indicator suggestions are already being worked out." in said(response)


# ------------------------------------------------------------------------------------------- the links' actions
def test_the_links_list_confirms_rejects_and_shows_only_and_filters_by_score(admin_client, education):
    a, b = education["a"], education["b"]
    m = w.master("Children enrolled in NFE")
    first = w.link(m, a, 101, title=ENROLLED, status="suggested", unit_class="")
    second = w.link(m, b, 202, title="# of boys enrolled in NFE", status="suggested", unit_class="")
    gone = w.link(m, a, 107, title="# of children enrolled in ALP")  # no longer in eTools
    dm.EtoolsMasterLink.objects.filter(pk=first.pk).update(score=0.95)
    dm.EtoolsMasterLink.objects.filter(pk=second.pk).update(score=0.7)
    strong = admin_client.get(reverse(LINKS) + "?score=strong").context["cl"].result_list
    assert [x.pk for x in strong] == [first.pk]
    elsewhere = w.link(w.master("Children reached with PSS", section=w.CP), a, 103, title="PSS")
    by_section = (
        admin_client.get(reverse(LINKS) + f"?master__section={w.EDUCATION}").context["cl"].result_list
    )
    assert {first.pk, second.pk, gone.pk} <= {x.pk for x in by_section}
    assert elsewhere.pk not in {x.pk for x in by_section}

    response = act(admin_client, "confirm_links", [first, gone], url=LINKS)
    assert "1 link(s) confirmed: they count from now on." in said(response)
    assert any(
        "left as they were: PD/2026/001 # of children enrolled in ALP: No longer in eTools" in t
        for t in said(response)
    )
    first.refresh_from_db()
    assert (first.status, first.unit_class, first.confirmed_by) == ("confirmed", "number", "admin")

    act(admin_client, "reject_links", [second], url=LINKS)
    second.refresh_from_db()
    assert (second.status, second.confirmed_by) == ("rejected", "admin")
    act(admin_client, "make_shown", [first], url=LINKS)
    assert dm.EtoolsMasterLink.objects.get(pk=first.pk).role == "shown"
    html = admin_client.get(reverse(LINKS)).content.decode()
    assert "confirmed_by" not in html


def test_a_link_on_its_masters_page_is_checked_only_when_changed_and_keeps_one_total(education, monkeypatch):
    a, b = education["a"], education["b"]
    m = w.master("Children enrolled in NFE")
    shown = w.link(m, b, 201, title=ENROLLED, role="shown")
    kept = w.link(m, a, 101, title=ENROLLED)
    reads = []
    real = masters.pd_indicator
    monkeypatch.setattr(masters, "pd_indicator", lambda *args: reads.append(args) or real(*args))
    assert MasterLinkForm({"role": "total", "sequence": 0}, instance=kept).is_valid()
    assert reads == []  # unchanged: its PD is not read again
    form = MasterLinkForm({"role": "total", "sequence": 0}, instance=shown)
    assert not form.is_valid()  # it counts in another master's total
    assert form.errors["role"] == [
        "This PD indicator is in the total of Children in non-formal education already (confirmed): it can be in "
        "one total only."
    ]
    assert MasterLinkForm({"role": "shown", "sequence": 3}, instance=kept).is_valid() and len(reads) == 2


# --------------------------------------------------------------------------------------- what the forms refuse
def _add_link(client, master, pd, ident, role="total", status="confirmed"):
    data = {"master": master.pk, "pd": pd.pk, "ident": ident, "role": role, "status": status, "sequence": 0}
    return client.post(reverse("admin:datamart_etoolsmasterlink_add"), data)


@pytest.mark.parametrize(
    ("method", "ident", "role", "error"),
    [
        ("sum", "s102", "total", "A sum adds numbers only: a percentage or a ratio can only be shown in it."),
        (
            "percent",
            "s101",
            "total",
            "A percentage master's total takes percentages (numbers count as numerators or denominators).",
        ),
        (
            "percent",
            "s106",
            "total",
            "A percentage master's total takes percentages (numbers count as numerators or denominators).",
        ),
        (
            "percent",
            "s102",
            "numerator",
            "A numerator or a denominator must be a number (a count), not a percentage or a ratio.",
        ),
        ("sum", "s999", "total", "This is not one of the programme document's indicators."),
    ],
)
def test_the_link_form_refuses_what_the_master_cannot_count(
    admin_client, education, method, ident, role, error
):
    m = w.master("Some master", method)
    response = _add_link(admin_client, m, education["a"], ident, role=role)
    assert response.status_code == 200, "saved"
    errors = [text for found in response.context["adminform"].form.errors.values() for text in found]
    assert any(text.startswith(error) for text in errors), errors
    assert not dm.EtoolsMasterLink.objects.filter(master=m).exists()


def test_a_link_added_by_hand_saves_what_it_was_and_who_confirmed_it(admin_client, education):
    m = w.master("Children enrolled in NFE")
    response = _add_link(admin_client, m, education["a"], "s101")
    assert response.status_code == 302
    link = dm.EtoolsMasterLink.objects.get(master=m)
    assert (link.title, link.pd_number, link.unit_class, link.confirmed_by) == (
        ENROLLED,
        "PD/2026/001",
        "number",
        "admin",
    )


@pytest.mark.parametrize("home", ["confirmed", "suggested"])
def test_the_link_page_refuses_a_second_total_with_a_form_error(admin_client, education, home):
    """On a saved link only its role and place are a form's fields: the one-total rule is the form's, so
    making it count in a second total is said on the form, never a server error."""
    dm.EtoolsMasterLink.objects.filter(master=education["other"], ident="s201").update(status=home)
    m = w.master("Children enrolled in NFE")
    shown = w.link(m, education["b"], 201, title=ENROLLED, role="shown")
    url = reverse("admin:datamart_etoolsmasterlink_change", args=[shown.pk])
    response = admin_client.post(url, {"role": "total", "sequence": 0, "_save": "Save"})
    assert response.status_code == 200
    assert response.context["adminform"].form.errors["role"] == [
        f"This PD indicator is in the total of Children in non-formal education already ({home}): it can "
        "be in one total only."
    ]
    shown.refresh_from_db()
    assert shown.role == "shown"
    assert admin_client.post(url, {"role": "numerator", "sequence": 0, "_save": "Save"}).status_code == 200


def test_a_link_added_by_hand_starts_confirmed_and_the_night_keeps_it(admin_client, education):
    m = w.master("Children enrolled in NFE")
    form = admin_client.get(reverse("admin:datamart_etoolsmasterlink_add")).context["adminform"].form
    assert form["status"].initial == "confirmed"
    assert [value for value, _label in form.fields["status"].choices] == ["confirmed", "rejected"]
    refused = _add_link(admin_client, m, education["a"], "s101", status="suggested")
    assert refused.status_code == 200 and not dm.EtoolsMasterLink.objects.filter(master=m).exists()
    assert (
        _add_link(admin_client, m, education["a"], "s101", status=form["status"].initial).status_code == 302
    )
    link = dm.EtoolsMasterLink.objects.get(master=m)
    assert (link.status, link.confirmed_by) == ("confirmed", "admin")
    assert not isinstance(master_links.run(w.YEAR, "test"), str)
    link.refresh_from_db()  # the night rewrites suggestions only
    assert (link.status, link.role) == ("confirmed", "total")


def test_a_suggestion_made_shown_only_in_the_admin_stays_so_after_the_night(admin_client, education):
    a, other = education["a"], education["other"]
    master_links.run(w.YEAR, "test")
    suggestion = other.links.get(ident="s101", status="suggested")  # the same structure as PD B's total
    assert suggestion.role == "total"
    act(admin_client, "make_shown", [suggestion], url=LINKS)
    master_links.run(w.YEAR, "test")
    again = other.links.get(ident="s101", pd=a)
    assert (again.pk, again.status, again.role) == (suggestion.pk, "suggested", "shown")


def test_the_link_list_never_confirms_a_proposals_links_and_an_archived_master_keeps_none(
    admin_client, admin_user, education
):
    a = education["a"]
    proposal = w.master(ENROLLED, status="proposed", origin="proposed", proposal_key="k")
    link = w.link(proposal, a, 101, title=ENROLLED, status="suggested", unit_class="")
    response = act(admin_client, "confirm_links", [link], url=LINKS)
    assert any("Accept this proposed master indicator first" in text for text in said(response))
    link.refresh_from_db()
    assert link.status == "suggested"
    # an active master that counts PD indicators in its total is not archived: they would count nowhere
    m = w.master("Children enrolled in NFE")
    w.link(m, a, 104, title="# of youth enrolled in NFE")
    request = RequestFactory().get("/")
    request.user = admin_user
    form_class = EtoolsMasterAdmin(dm.EtoolsMaster, admin.site).get_form(request, m)
    data = {"section": m.section, "name": m.name, "method": "sum", "status": "archived", "sequence": 0}
    form = form_class(data, instance=m)
    assert not form.is_valid()
    assert form.errors["status"][0].startswith("It counts 1 PD indicator(s) in its total")
    m.links.update(role="shown")
    assert form_class(data, instance=dm.EtoolsMaster.objects.get(pk=m.pk)).is_valid()


def test_a_years_target_of_0_or_less_is_refused_and_the_filter_reads_it_as_none(admin_client, education):
    for target, valid in (("-5", False), ("0", False), ("", True), ("120", True)):
        form = MasterYearForm({"year": 2026, "code": "", "target": target, "is_active": "on"})
        assert form.is_valid() is valid, target
    negative = w.master("Typed below 0")
    dm.EtoolsMasterYear.objects.create(master=negative, year=w.YEAR, target=-5)  # saved before the check
    assert not masters.has_target(
        dm.EtoolsMaster.objects.prefetch_related("years").get(pk=negative.pk), w.YEAR
    )
    shown = {
        value: {
            m.pk for m in admin_client.get(reverse(MASTERS) + f"?target={value}").context["cl"].result_list
        }
        for value in ("yes", "no")
    }
    assert negative.pk in shown["no"] and negative.pk not in shown["yes"]


def test_the_master_form_refuses_a_method_change_that_breaks_its_links(admin_user, education):
    m = w.master("Children enrolled in NFE")
    w.link(m, education["a"], 101, title=ENROLLED)
    request = RequestFactory().get("/")
    request.user = admin_user
    model_admin = EtoolsMasterAdmin(dm.EtoolsMaster, admin.site)
    form_class = model_admin.get_form(request, m)
    data = {"section": m.section, "name": m.name, "method": "percent", "status": "active", "sequence": 0}
    form = form_class(data, instance=m)
    assert not form.is_valid()
    assert form.errors["method"][0].startswith("1 linked indicator(s) would not fit this method")
    assert form_class({**data, "method": "maximum"}, instance=m).is_valid()


# ------------------------------------------------------------------------------------------- from ActivityInfo
def _pivoting() -> list:
    return [
        list(model.objects.order_by("pk").values())
        for model in (MasterIndicator, MasterSubIndicator, SubIndicator, Database)
    ]


def test_masters_are_made_from_the_activityinfo_archive_which_is_never_changed(admin_client, hierarchy, pds):
    database = hierarchy["database"]
    cp = database.section
    SectionMatch.objects.create(etools_name="Child Protection", section=cp, how="exact", confirmed=True)
    SectionMatch.objects.create(
        etools_name="Child Protection and GBV", section=cp, how="manual", confirmed=True
    )
    a = pds["A"]
    w.indicator(a, 101, "# of children reached with case management", section="Child Protection")
    for n in (102, 103):  # the name with the most PD indicators
        w.indicator(
            a, n, f"# of children reached {n}", section="Child Protection and GBV", places=("Akkar", "Bekaa")
        )
    made = {
        method: MasterIndicator.objects.create(
            database=database, name=f"{method.title()} master", awp_code=f"9.{n}", aggregation_method=method,
            unit="days" if method == "AVERAGE" else None, sequence=10 + n,
        )
        for n, method in enumerate(("AVERAGE", "MAXIMUM", "MINIMUM", "COUNT"), start=1)
    }  # fmt: skip
    w.master("Share of girls", "percent", section="Child Protection and GBV")  # its name is taken
    education = Section.objects.create(name="Education", code="ED")
    SectionMatch.objects.create(etools_name="Education", section=education, how="contains", confirmed=False)
    other_db = Database.objects.create(
        ai_id=202619, db_id="ed", name="Education 2026", label="Education", username="", password="",
        section=education, reporting_year=database.reporting_year, is_funded_by_unicef=False,
    )  # fmt: skip
    unmatched = MasterIndicator.objects.create(database=other_db, name="Children enrolled", awp_code="1")
    before = _pivoting()

    chosen = [hierarchy["master"], hierarchy["ratio"], *made.values(), unmatched]
    response = act(
        admin_client, "make_etools_masters", chosen, url="admin:pivoting_masterindicator_changelist"
    )
    assert response.status_code == 302
    messages = said(response)
    assert any(
        text.startswith(
            "4 eTools master indicator(s) created in Child Protection and GBV. Type the 2026 target"
        )
        for text in messages
    )
    assert (
        "1 left out: COUNT counted ActivityInfo records and has no meaning for eTools PD indicators (Count master)."
        in messages
    )
    assert (
        "1 left out: their eTools section has a master indicator of that name already (Share of girls)."
        in messages
    )
    assert any(
        "1 not created: the NeuroDB section of their database has no confirmed eTools section name (Children "
        "enrolled)" in text
        for text in messages
    )
    assert _pivoting() == before  # the archive is read, never changed

    found = {m.activityinfo_master_id: m for m in dm.EtoolsMaster.objects.filter(origin="activityinfo")}
    gbv = "Child Protection and GBV"
    assert {pk: (m.section, m.name, m.method, m.unit, m.status) for pk, m in found.items()} == {
        hierarchy["master"].pk: (gbv, "Children reached (total)", "sum", "", "active"),
        made["AVERAGE"].pk: (gbv, "Average master", "average", "days", "active"),
        made["MAXIMUM"].pk: (gbv, "Maximum master", "maximum", "", "active"),
        made["MINIMUM"].pk: (gbv, "Minimum master", "minimum", "", "active"),
    }  # SUM_OVER_SUM (Share of girls) would be a percentage master: its name is taken in the section
    assert found[made["AVERAGE"].pk].note == curation.AVERAGED
    years = {
        (y.master.activityinfo_master_id, y.year, y.code, y.target) for y in dm.EtoolsMasterYear.objects.all()
    }
    assert (hierarchy["master"].pk, 2026, "1", None) in years  # the AWP code, no target: staff type it
    assert (made["MINIMUM"].pk, 2026, "9.3", None) in years
    again = act(
        admin_client,
        "make_etools_masters",
        [hierarchy["master"]],
        url="admin:pivoting_masterindicator_changelist",
    )
    assert any(
        "1 left out: their eTools section has a master indicator of that name already" in t
        for t in said(again)
    )
    assert dm.EtoolsMaster.objects.filter(origin="activityinfo").count() == len(found)


def test_a_percentage_master_is_made_from_sum_over_sum(admin_client, hierarchy):
    SectionMatch.objects.create(
        etools_name="Child Protection", section=hierarchy["database"].section, how="exact", confirmed=True
    )
    act(
        admin_client,
        "make_etools_masters",
        [hierarchy["ratio"]],
        url="admin:pivoting_masterindicator_changelist",
    )
    made = dm.EtoolsMaster.objects.get(activityinfo_master_id=hierarchy["ratio"].pk)
    assert (made.section, made.name, made.method, made.origin) == (
        "Child Protection",
        "Share of girls",
        "percent",
        "activityinfo",
    )
    assert (made.years.get().code, made.years.get().target) == ("2", None)


def test_only_people_who_may_add_etools_masters_can_seed_them(client, hierarchy, roles):
    user = staff(VIEWER)
    user.user_permissions.add(Permission.objects.get(codename="view_masterindicator"))
    client.force_login(user)
    SectionMatch.objects.create(
        etools_name="Child Protection", section=hierarchy["database"].section, how="exact", confirmed=True
    )
    act(client, "make_etools_masters", [hierarchy["master"]], url="admin:pivoting_masterindicator_changelist")
    assert not dm.EtoolsMaster.objects.exists()
