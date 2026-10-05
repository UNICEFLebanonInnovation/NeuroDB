"""Donor accounts: confined to their page, their own funds only, the country as aggregates only.

On ``overview_fixture``: one programme document (Amel, Child Protection) with 500 children reached
(Akkar 300, Beirut 200). Its first FR (10,000, 4,000 disbursed) is the EU's; a second FR added here
(10,000, 1,000 disbursed) is Japan's. The EU therefore holds half of the programme: 250 children,
4,000 disbursed.
"""

import datetime
import json
import re
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.datamart import models as dm
from neurodb.donors import services
from neurodb.donors.models import DonorAccount
from neurodb.partnerships.models import PCA
from neurodb.reports import overview
from tests.reports.overview_fixture import TODAY, make_overview_data

PASSWORD = "donor-pass-123456"


@pytest.fixture
def data(hierarchy):
    made = make_overview_data()
    pd = PCA.objects.get(number="LEB/PD1")
    dm.FundsReservationHeader.objects.create(
        datamart_id=2,
        intervention=pd,
        fr_number="0400002",
        total_amt=Decimal(10000),
        actual_amt=Decimal(1000),
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 12, 31),
    )
    dm.FundsReservation.objects.create(
        datamart_id=2, intervention=pd, fr_number="0400002", line_item=1, donor="Japan", overall_amount=10000
    )
    dm.FundsReservation.objects.filter(fr_number="0400001").update(grant_number="SC1")
    dm.Grant.objects.create(datamart_id=1, name="SC1", donor="EU", expiry=datetime.date(2026, 9, 30))
    return made


def make_donor(username="eu-donor", **fields):
    user = User.objects.create_user(username=username, email=f"{username}@example.org", password=PASSWORD)
    defaults = {"name": "European Union", "donors": ["EU"], "must_change_password": False}
    defaults.update(fields)
    return DonorAccount.objects.create(user=user, **defaults)


@pytest.fixture
def donor(db):
    return make_donor()


@pytest.fixture
def donor_client(client, donor):
    client.force_login(donor.user)
    return client


def page_data(response) -> dict:
    match = re.search(r'<script id="donor-data" type="application/json">(.*?)</script>', response.text, re.S)
    return json.loads(match.group(1))


# ------------------------------------------------------------------------------ attribution
def test_contribution_attributes_the_donors_share_of_funds_and_children(data, reporting_year, donor):
    result = services.contribution(donor, 2026, TODAY)
    assert len(result["pds"]) == 1
    pd = result["pds"][0]
    assert pd["committed"] == 10000
    assert pd["disbursed"] == 4000  # the EU's FR, not Japan's
    assert pd["share"] == 0.5  # 10,000 of the programme's 20,000
    assert pd["children_whole"] == 500
    assert pd["children"] == 250
    assert pd["gov"] == {"akkar": 0.6, "beirut": 0.4}
    assert pd["partner"] == "Amel Association"
    assert pd["grants"] == {"SC1": 10000}
    assert pd["closed"] is False
    assert pd["age"] == {"Age not specified": 1.0}  # "Children" names no age range
    assert result["grants"] == [
        {
            "key": "SC1",
            "label": "SC1",
            "name": "",
            "expires": "2026-09-30",
            "days_left": (datetime.date(2026, 9, 30) - TODAY).days,
        }
    ]


def test_grants_carry_their_readable_name(data, reporting_year, donor):
    dm.Grant.objects.filter(name="SC1").update(description="Education and protection")
    grant = services.contribution(donor, 2026, TODAY)["grants"][0]
    assert (grant["name"], grant["label"]) == ("Education and protection", "SC1 · Education and protection")


def test_a_programme_past_its_end_is_closed_not_behind(data, reporting_year, donor):
    after_the_end = datetime.date(2027, 1, 15)
    PCA.objects.filter(number="LEB/PD1").update(end=datetime.date(2026, 6, 30))
    pd = services.contribution(donor, 2026, after_the_end)["pds"][0]
    assert pd["closed"] is True
    page = services.build(donor, 2026, after_the_end, cache=False)
    assert page["statuses"]["closed"]["label"] == "Closed"


def test_data_date_is_the_datamart_sync_else_the_latest_source(db):
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    assert services.data_as_of() is None
    now = timezone.now()
    ok, failed = SyncRun.Status.SUCCEEDED, SyncRun.Status.FAILED
    SyncRun.objects.create(job=SyncRun.Job.ACTIVITYINFO_DATA, status=ok, finished_at=now)
    SyncRun.objects.create(job=SyncRun.Job.POPULATION, status=ok, finished_at=now + datetime.timedelta(1))
    assert services.data_as_of() == now  # no Datamart sync yet: the latest source that feeds the page
    earlier = now - datetime.timedelta(days=1)
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, status=ok, finished_at=earlier)
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, status=failed, finished_at=now)
    assert services.data_as_of() == earlier  # the funds and indicators come from the Datamart


def test_grant_restriction_and_other_donor(data, reporting_year):
    japan = make_donor("japan", name="Japan", donors=["japan"])  # the name matches whatever its case
    pd = services.contribution(japan, 2026, TODAY)["pds"][0]
    assert (pd["committed"], pd["disbursed"], pd["children"]) == (10000, 1000, 250)
    only_other_grant = make_donor("eu-other", donors=["EU"], grants=["SC999"])
    assert services.contribution(only_other_grant, 2026, TODAY)["pds"] == []


def test_partner_names_can_be_hidden(data, reporting_year):
    account = make_donor("eu-anon", show_partner_names=False)
    pd = services.contribution(account, 2026, TODAY)["pds"][0]
    assert pd["partner"] == "Partner 1 (civil society)"


def test_country_block_names_no_programme_partner_or_donor(data, reporting_year):
    everything = overview.build(
        overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY), cache=False
    )
    country = services.country(everything)
    text = json.dumps(country)
    for secret in ("Amel", "LEB/PD1", "Child protection services", "EU", "Japan"):
        assert secret not in text
    assert not {"money", "reserved", "disbursed", "by_donor", "by_partner", "attention"} & set(country)
    assert country["programmes"] == 1
    assert country["children"] == everything["impact"]["children_reached"]


# ------------------------------------------------------------------------------ confinement
def test_sign_in_lands_on_the_donor_page_whatever_next_says(client, donor):
    response = client.post(
        reverse("account_login") + "?next=/programmes/", {"login": donor.user.username, "password": PASSWORD}
    )
    assert response.status_code == 302
    follow = client.get(response["Location"])
    assert follow.status_code == 302 and follow["Location"] == reverse("donors:page")


@pytest.mark.parametrize(
    "name",
    [
        "reports:overview",
        "reports:programmes",
        "reports:donors",
        "reports:brief",
        "landing",
        "fmm:dashboard",
        "fmm:visits",
        "fmm:lookup",
        "fmm:drill",
    ],
)
def test_every_other_page_redirects_to_the_donor_page(donor_client, name):
    response = donor_client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"] == reverse("donors:page")


def test_admin_and_unknown_addresses_redirect_too(donor_client):
    assert donor_client.get(reverse("admin:index"))["Location"] == reverse("donors:page")
    assert donor_client.get("/no-such-page/")["Location"] == reverse("donors:page")


def test_the_api_the_assistant_and_htmx_are_refused(donor_client):
    assert donor_client.get(reverse("api:donors")).status_code == 403
    assert donor_client.get(reverse("api:programmes")).status_code == 403
    assert donor_client.get(reverse("assistant:ask")).status_code == 403
    assert donor_client.get(reverse("reports:overview"), HTTP_HX_REQUEST="true").status_code == 403
    assert donor_client.get(reverse("fmm:dashboard"), HTTP_HX_REQUEST="true").status_code == 403
    assert donor_client.get(reverse("fmm:visits"), HTTP_HX_REQUEST="true").status_code == 403
    assert donor_client.get(reverse("fmm:drill"), HTTP_HX_REQUEST="true").status_code == 403


def test_donor_page_shows_own_funds_only(data, reporting_year, donor_client):
    response = donor_client.get(reverse("donors:page"), {"year": "2026"})
    assert response.status_code == 200
    assert "European Union" in response.text
    assert "Japan" not in response.text  # the other donor of the same programme
    assert 'id="sidebar"' not in response.text
    payload = page_data(response)
    assert [p["id"] for p in payload["pds"]] == ["LEB/PD1"]
    assert payload["pds"][0]["committed"] == 10000
    assert "Amel" not in json.dumps(payload["overall"])
    assert "The date of the data is not available yet." in response.text  # never "not yet synced"


def test_a_year_outside_the_choices_falls_back(data, reporting_year, donor_client):
    response = donor_client.get(reverse("donors:page"), {"year": "1999"})
    assert response.status_code == 200
    assert page_data(response)["year"] == datetime.date.today().year


def test_temporary_password_must_be_changed_first(client, db):
    account = make_donor("fresh", must_change_password=True)
    client.force_login(account.user)
    response = client.get(reverse("donors:page"))
    assert response["Location"] == reverse("account_change_password")
    assert client.get(reverse("account_change_password")).status_code == 200
    response = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": "a-new-long-pass-9876", "password2": "a-new-long-pass-9876"},
    )
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    account.refresh_from_db()
    assert account.must_change_password is False


def test_expired_or_switched_off_accounts_are_signed_out(client, db):
    account = make_donor("old", expires_on=datetime.date.today() - datetime.timedelta(days=1))
    client.force_login(account.user)
    response = client.get(reverse("donors:page"))
    assert response["Location"] == reverse("account_login")
    assert "_auth_user_id" not in client.session


# ------------------------------------------------------------------------------ staff
def test_staff_do_not_have_a_donor_page(client_viewer, donor):
    assert client_viewer.get(reverse("donors:page")).status_code == 404
    assert client_viewer.get(reverse("donors:page"), {"account": donor.pk}).status_code == 404


def test_administrators_preview_an_account(client, admin_user, donor, data, reporting_year):
    client.force_login(admin_user)
    assert client.get(reverse("donors:page")).status_code == 404
    response = client.get(reverse("donors:page"), {"account": donor.pk})
    assert response.status_code == 200
    assert "Preview" in response.text and "European Union" in response.text


def test_admin_creates_the_sign_in_with_a_temporary_password(client, db, roles):
    superuser = User.objects.create_superuser("root", "root@example.org", "root-pass-123456")
    client.force_login(superuser)
    response = client.post(
        reverse("admin:donors_donoraccount_add"),
        {
            "email": "Focal@Donor.example",
            "expires_on": "",
            "name": "A donor",
            "other_donors": "EU",
            "grant_list": "SC1, SC2",
            "show_partner_names": "on",
            "contact": "",
        },
        follow=True,
    )
    assert response.status_code == 200
    account = DonorAccount.objects.get()
    assert account.donors == ["EU"] and account.grants == ["SC1", "SC2"]
    assert account.user.email == "focal@donor.example"
    assert not account.user.is_staff and not account.user.is_superuser
    assert account.active and account.owns_user and account.is_valid_now()
    assert account.must_change_password
    shown = re.search(r"<code[^>]*>([^<]+)</code>", response.text).group(1)
    assert account.user.check_password(shown)


def test_admin_refuses_an_email_that_belongs_to_staff(client, db, roles, viewer):
    superuser = User.objects.create_superuser("root", "root@example.org", "root-pass-123456")
    client.force_login(superuser)
    response = client.post(
        reverse("admin:donors_donoraccount_add"),
        {"email": viewer.email, "name": "X", "other_donors": "EU"},
    )
    assert response.status_code == 200
    assert not DonorAccount.objects.exists()


def test_admin_pages_render_and_a_new_password_can_be_issued(client, db, roles, donor, data):
    superuser = User.objects.create_superuser("root", "root@example.org", "root-pass-123456")
    client.force_login(superuser)
    assert client.get(reverse("admin:donors_donoraccount_changelist")).status_code == 200
    add = client.get(reverse("admin:donors_donoraccount_add"))
    assert add.status_code == 200 and "EU" in add.text  # the donor names on FR lines are the choices
    assert client.get(reverse("admin:donors_donoraccount_change", args=[donor.pk])).status_code == 200
    old = donor.user.password
    response = client.post(
        reverse("admin:donors_donoraccount_changelist"),
        {"action": "issue_password", "_selected_action": [donor.pk]},
        follow=True,
    )
    assert response.status_code == 200
    donor.refresh_from_db()
    donor.user.refresh_from_db()
    assert donor.user.password != old and donor.must_change_password


@pytest.fixture
def root_client(client, db, roles):
    client.force_login(User.objects.create_superuser("root", "root@example.org", "root-pass-123456"))
    return client


def add_account(client, **fields):
    form = {
        "existing_user": "",
        "email": "",
        "name": "A donor",
        "other_donors": "EU",
        "show_partner_names": "on",
    }
    form.update(fields)
    return client.post(reverse("admin:donors_donoraccount_add"), form, follow=True)


def test_admin_links_an_existing_user(root_client, viewer):
    old_password = viewer.password
    response = add_account(root_client, existing_user=viewer.pk)
    assert response.status_code == 200
    account = DonorAccount.objects.get()
    viewer.refresh_from_db()
    assert account.user == viewer and not account.owns_user
    assert viewer.password == old_password and not account.must_change_password
    assert not viewer.groups.exists()  # a donor has no role
    assert account.is_valid_now()


def test_linking_can_issue_a_temporary_password(root_client, viewer):
    response = add_account(root_client, existing_user=viewer.pk, issue_password="on")
    account = DonorAccount.objects.get()
    shown = re.search(r"<code[^>]*>([^<]+)</code>", response.text).group(1)
    viewer.refresh_from_db()
    assert viewer.check_password(shown) and account.must_change_password


def test_staff_and_editors_cannot_be_linked(root_client, admin_user):
    response = add_account(root_client, existing_user=admin_user.pk)
    assert response.status_code == 200
    assert not DonorAccount.objects.exists()
    assert str(admin_user.pk) not in re.search(
        r'<select name="existing_user".*?</select>', response.text, re.S
    ).group(0)


def test_one_of_existing_user_or_email(root_client, viewer):
    add_account(root_client, existing_user=viewer.pk, email="new@donor.example")
    add_account(root_client)
    assert not DonorAccount.objects.exists()


def test_deleting_a_linked_account_switches_the_user_off(root_client, viewer):
    add_account(root_client, existing_user=viewer.pk)
    account = DonorAccount.objects.get()
    root_client.post(reverse("admin:donors_donoraccount_delete", args=[account.pk]), {"post": "yes"})
    viewer.refresh_from_db()
    assert not DonorAccount.objects.exists()
    assert not viewer.is_active  # kept, but it does not fall back to a viewer's access


def test_deleting_an_account_deletes_the_sign_in_it_created(root_client):
    add_account(root_client, email="new@donor.example")
    account = DonorAccount.objects.get()
    root_client.post(reverse("admin:donors_donoraccount_delete", args=[account.pk]), {"post": "yes"})
    assert not User.objects.filter(email="new@donor.example").exists()


def test_grants_are_offered_by_donor_and_must_belong_to_the_chosen_donors(root_client, data):
    add = root_client.get(reverse("admin:donors_donoraccount_add"))
    assert '<optgroup label="EU">' in add.text and ">SC1<" in add.text
    assert "On: the donor sees the partners" in add.text and "Off: partners appear as" in add.text
    assert "Empty: it does not expire." in add.text

    wrong = add_account(root_client, email="x@donor.example", other_donors="Japan", grant_choices=["SC1"])
    assert "Not a grant of the donors chosen above: SC1." in wrong.text
    assert not DonorAccount.objects.exists()

    add_account(root_client, email="x@donor.example", grant_choices=["SC1"], grant_list="SC9")
    account = DonorAccount.objects.get()
    assert account.donors == ["EU"] and account.grants == ["SC1", "SC9"]
    change = root_client.get(reverse("admin:donors_donoraccount_change", args=[account.pk]))
    assert '<option value="SC1" selected>' in change.text and 'value="SC9"' in change.text
