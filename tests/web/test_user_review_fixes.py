"""The first fixes of the user review (29 Sep 2026): donor sign-ins are not "users without a role",
"ahead of schedule" is not "over target", one on-track definition, and the status reference line."""

import datetime

import pytest
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.donors.models import DonorAccount
from neurodb.indicators.services.tracking import LABELS, OVER_TARGET, label_for, tracking
from neurodb.review.services import on_track_share


@pytest.fixture
def client_super(client, db, roles):
    client.force_login(
        User.objects.create_superuser(username="root", email="root@example.org", password="root-pass-123456")
    )
    return client


@pytest.fixture
def donor(db):
    user = User.objects.create_user(username="donor", email="donor@example.org", password="donor-pass-123456")
    return DonorAccount.objects.create(user=user, name="EU", donors=["EU"], must_change_password=False)


# ------------------------------------------------------------------ donor accounts have no role on purpose
def test_admin_home_does_not_count_a_donor_as_a_user_without_a_role(client_super, donor):
    html = client_super.get(reverse("admin:index")).text
    assert "has no role" not in html


def test_users_list_shows_donors_as_donors(client_super, donor, viewer):
    url = reverse("admin:users_user_changelist")
    none = client_super.get(url + "?role=none")
    donors = client_super.get(url + "?role=donor")
    assert donor.user not in list(none.context["cl"].result_list)
    assert list(donors.context["cl"].result_list) == [donor.user]
    assert "Donor" in donors.text


def test_a_role_action_skips_donor_accounts(client_super, donor, viewer):
    response = client_super.post(
        reverse("admin:users_user_changelist"),
        {"action": "make_administrator", "_selected_action": [donor.user.pk, viewer.pk]},
        follow=True,
    )
    assert not donor.user.groups.exists()
    assert viewer.groups.filter(name="Administrator").exists()
    assert "donor account(s) skipped" in response.text


# ------------------------------------------------------------------ ahead of schedule vs over target
def test_ahead_of_schedule_below_the_target_over_target_past_it():
    assert LABELS[OVER_TARGET] == "Ahead of schedule"
    assert label_for(OVER_TARGET, 80.0) == "Ahead of schedule"
    assert label_for(OVER_TARGET, 139.0) == "Over target"
    assert label_for("on_track", 110.0) == "On track"
    early = datetime.date(2026, 3, 1)
    assert tracking(800, 1000, 2026, early).label == "Ahead of schedule"  # 80 % in March
    assert tracking(1390, 1000, 2026, early).label == "Over target"


# ------------------------------------------------------------------ one on-track definition
def test_on_track_share_counts_on_track_and_ahead():
    counts = {"on_track": 5, "over_target": 10, "off_track": 18, "not_reported": 3, "no_target": 0}
    assert on_track_share({"status_counts": counts, "on_track_percent": 15.2}) == 45.5  # (5 + 10) of 33
    assert on_track_share({"on_track_percent": 40.0}) == 40.0  # no counts stored: the stored share
    assert on_track_share(None) is None


# ------------------------------------------------------------------ what a status is compared with
def test_dashboard_says_what_the_status_is_compared_with(client_viewer, hierarchy):
    response = client_viewer.get(reverse("reports:database_dashboard", args=[hierarchy["database"].id]))
    assert response.status_code == 200
    assert "of the year elapsed on" in response.text
    assert "ahead of schedule" in response.text
