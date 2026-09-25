"""ActivityInfo partner names linked to eTools partners (by hand, by name, by programme document)."""

import datetime

import pytest
from django.core.management import call_command
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.facts import queries
from neurodb.facts.models import ActivityReportNew
from neurodb.partnerships import linking
from neurodb.partnerships.models import PCA, PartnerLink, PartnerOrganization

pytestmark = pytest.mark.django_db


def record(database, partner, project="LEB/PCA2026001", month="2026-01", value=10, indicator="i_a"):
    return ActivityReportNew.objects.create(
        dbase=database,
        database_ai_id=str(database.ai_id),
        indicator_id=indicator,
        indicator_name=indicator,
        indicator_value=value,
        month_name=month + "-01",
        month=month,
        partner_label=partner,
        project_label=project,
        location_name="Site",
        funded_by="UNICEF",
        last_edited_time=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )


@pytest.fixture
def partners(db):
    amel = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", short_name="AMEL", partner_type="CSO", vendor_number="V1"
    )
    tdh = PartnerOrganization.objects.create(
        etl_id="2",
        name="Terre des Hommes Italia",
        short_name="TdH-It",
        partner_type="CSO",
        vendor_number="V2",
    )
    other = PartnerOrganization.objects.create(
        etl_id="3", name="Save the Children", short_name="SCI", partner_type="CSO", vendor_number="V3"
    )
    PCA.objects.create(
        etl_id="11", number="LEB/PCA2026001-1", title="PD", partner=other, partner_name=other.name
    )
    PCA.objects.create(etl_id="12", number="LEB/PCA2026002-2", title="PD", partner=tdh, partner_name=tdh.name)
    return {"amel": amel, "tdh": tdh, "other": other}


def test_names_compare_without_accents_case_or_punctuation():
    assert linking.normalise_name("Amel_Association") == "amel association"
    assert linking.normalise_name("  The AMEL  Association (Lebanon) ") == "amel association lebanon"
    assert linking.normalise_name("Société Générale") == "societe generale"
    assert linking.normalise_name(None) == ""


def test_labels_are_linked_by_name_then_by_programme_document(database, partners):
    record(database, "Amel_Association")  # name, with the v2 underscore normalisation
    record(database, "TdH-It", project="LEB/PCA2026001")  # short name wins over the PD
    record(database, "Some NGO", project="LEB/PCA2026001", value=5)
    record(database, "Some NGO", project="LEB/PCA2026002", value=5, month="2026-02")
    record(database, "Some NGO", project="LEB/PCA2026002", value=5, month="2026-03")
    record(database, "Nobody knows", project="")
    record(database, "UNICEF", project="")
    run = linking.link_activityinfo_partners(triggered_by="test")
    links = {link.label: link for link in PartnerLink.objects.all()}
    assert set(links) == {"Amel_Association", "TdH-It", "Some NGO", "Nobody knows"}
    assert (links["Amel_Association"].partner, links["Amel_Association"].method) == (partners["amel"], "name")
    assert (links["TdH-It"].partner, links["TdH-It"].method) == (partners["tdh"], "name")
    assert (links["Some NGO"].partner, links["Some NGO"].method) == (partners["tdh"], "pd")  # 2 of 3 records
    assert links["Nobody knows"].partner is None and links["Nobody knows"].method == ""
    some = links["Some NGO"]
    assert (some.records, some.first_month, some.last_month, some.database_ids) == (
        3,
        "2026-01",
        "2026-03",
        [database.id],
    )
    assert run.status == SyncRun.Status.SUCCEEDED and run.job == SyncRun.Job.PARTNER_LINKS
    assert run.details["linked_by_name"] == 2 and run.details["linked_by_pd"] == 1
    assert run.details["unlinked"] == 1 and run.details["unlinked_examples"] == ["Nobody knows"]
    assert (run.rows_in, run.rows_written) == (4, 3)
    assert linking.partner_labels(partners["tdh"]) == ["Some NGO", "TdH-It"]
    assert linking.links_for(["Amel_Association", "Nobody knows"]) == {"Amel_Association": partners["amel"]}


def test_a_link_set_by_hand_survives_the_refresh(database, partners):
    record(database, "Nobody knows", project="")
    linking.link_activityinfo_partners()
    link = PartnerLink.objects.get(label="Nobody knows")
    link.partner, link.method = partners["amel"], PartnerLink.Method.MANUAL
    link.save()
    record(database, "Nobody knows", project="LEB/PCA2026001", month="2026-02")
    run = linking.link_activityinfo_partners()
    link.refresh_from_db()
    assert (link.partner, link.method, link.records) == (partners["amel"], "manual", 2)
    assert run.details["set_by_hand"] == 1 and run.details["linked_by_pd"] == 0


def test_a_pd_without_its_partner_key_still_links_through_the_partner_name(database, partners):
    PCA.objects.create(etl_id="13", number="LEB/PCA2026003-1", title="PD", partner=None, partner_name="AMEL")
    record(database, "Some NGO", project="LEB/PCA2026003")
    linking.link_activityinfo_partners()
    link = PartnerLink.objects.get(label="Some NGO")
    assert (link.partner, link.method) == (partners["amel"], "pd")


def test_a_partner_cleared_by_hand_stays_unlinked(database, partners, client, admin_user):
    record(database, "Amel_Association")
    linking.link_activityinfo_partners()
    link = PartnerLink.objects.get(label="Amel_Association")
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    response = client.post(reverse("admin:etools_partnerlink_change", args=[link.id]), {"partner": ""})
    assert response.status_code == 302
    run = linking.link_activityinfo_partners()
    link.refresh_from_db()
    assert (link.partner, link.method, link.records) == (None, "manual", 1)
    assert run.details["set_by_hand"] == 1 and run.details["linked_by_name"] == 0


def test_labels_keep_their_spelling_so_the_records_are_found_again(database, partners):
    record(database, "Amel Association ")  # a trailing space from the export
    record(database, "Amel Association ", month="2026-02")
    linking.link_activityinfo_partners()
    assert PartnerLink.objects.get(label="Amel Association ").records == 2
    assert linking.partner_labels(partners["amel"]) == ["Amel Association "]
    assert queries.partner_activity(["Amel Association "])[0]["records"] == 2


def test_names_gone_from_the_records_are_forgotten_or_emptied(database, partners):
    record(database, "Amel_Association")
    record(database, "Nobody knows", project="")
    linking.link_activityinfo_partners()
    nobody = PartnerLink.objects.get(label="Nobody knows")
    nobody.partner, nobody.method = partners["tdh"], PartnerLink.Method.MANUAL
    nobody.save()
    ActivityReportNew.objects.all().delete()
    record(database, "AMEL Association")  # renamed in ActivityInfo
    run = linking.link_activityinfo_partners()
    assert set(PartnerLink.objects.values_list("label", flat=True)) == {"AMEL Association", "Nobody knows"}
    nobody.refresh_from_db()
    assert (nobody.partner, nobody.records, nobody.database_ids) == (partners["tdh"], 0, [])
    assert linking.partner_labels(partners["tdh"]) == [] and run.details["removed"] == 2
    assert linking.partner_labels(partners["amel"]) == ["AMEL Association"]


def test_a_tie_between_two_partners_programme_documents_is_left_to_the_admin(database, partners):
    record(database, "Youth Centre", project="LEB/PCA2026001")  # Save the Children
    record(database, "Youth Centre", project="LEB/PCA2026002", month="2026-02")  # TdH
    run = linking.link_activityinfo_partners()
    link = PartnerLink.objects.get(label="Youth Centre")
    assert link.partner is None and link.method == ""
    assert run.details["ambiguous"] == 1
    assert sorted(run.details["ambiguous_examples"]["Youth Centre"]) == sorted(
        [partners["other"].id, partners["tdh"].id]
    )


def test_a_partner_deleted_in_etools_is_never_linked_to(database, partners):
    partners["other"].deleted_flag = True
    partners["other"].save()
    PCA.objects.filter(number="LEB/PCA2026001-1").update(partner_name="Terre des Hommes Italia")
    record(database, "Some NGO", project="LEB/PCA2026001")
    record(database, "Save the Children", project="")
    linking.link_activityinfo_partners()
    assert PartnerLink.objects.get(label="Some NGO").partner == partners["tdh"]  # through the PD's name
    assert PartnerLink.objects.get(label="Save the Children").partner is None
    stale = PartnerLink.objects.create(label="Old", partner=partners["other"], method="manual", records=3)
    assert linking.links_for(["Old"]) == {} and linking.partner_labels(partners["other"]) == ["Old"]
    stale.delete()


def test_a_failure_is_recorded_on_the_run_and_does_not_raise(database, partners, monkeypatch):
    record(database, "Amel_Association")
    monkeypatch.setattr(linking, "_labels", lambda: 1 / 0)
    run = linking.link_activityinfo_partners()
    assert run.status == SyncRun.Status.FAILED and "ZeroDivisionError" in run.error
    assert not PartnerLink.objects.exists()


def test_only_users_who_may_change_links_can_rematch(
    database, partners, client, admin_user, django_user_model
):
    from django.contrib.auth.models import Permission

    record(database, "Amel_Association")
    viewer = django_user_model.objects.create_user(
        username="staff-viewer", password="x-pass-123456", is_staff=True
    )
    viewer.user_permissions.add(Permission.objects.get(codename="view_partnerlink"))
    client.force_login(viewer)
    assert client.get(reverse("admin:etools_partnerlink_relink")).status_code == 403
    assert not SyncRun.objects.filter(job=SyncRun.Job.PARTNER_LINKS).exists()
    viewer.user_permissions.add(Permission.objects.get(codename="change_partnerlink"))
    assert client.get(reverse("admin:etools_partnerlink_relink")).status_code == 302
    assert SyncRun.objects.filter(job=SyncRun.Job.PARTNER_LINKS).count() == 1


def test_a_name_two_partners_share_is_not_used(database, partners):
    PartnerOrganization.objects.create(
        etl_id="4", name="Amel Association", short_name="AMEL2", partner_type="CSO", vendor_number="V4"
    )
    record(database, "Amel Association", project="")
    linking.link_activityinfo_partners()
    assert PartnerLink.objects.get(label="Amel Association").partner is None


def test_the_command_and_the_admin_action(database, partners, client, admin_user):
    record(database, "Amel_Association")
    call_command("link_partners", "--triggered-by", "shell")
    assert SyncRun.objects.get(job=SyncRun.Job.PARTNER_LINKS).triggered_by == "shell"
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    changelist = client.get(reverse("admin:etools_partnerlink_changelist"))
    assert changelist.status_code == 200 and "Amel_Association" in changelist.text
    response = client.get(reverse("admin:etools_partnerlink_relink"))
    assert response.status_code == 302 and SyncRun.objects.filter(job=SyncRun.Job.PARTNER_LINKS).count() == 2
    link = PartnerLink.objects.get(label="Amel_Association")
    change = client.post(
        reverse("admin:etools_partnerlink_change", args=[link.id]),
        {"label": link.label, "partner": partners["other"].id},
    )
    assert change.status_code == 302
    link.refresh_from_db()
    assert (link.partner, link.method) == (partners["other"], "manual")


def test_the_imports_end_with_a_link_run(database, partners, monkeypatch):
    from neurodb.integrations.management.commands import import_activityinfo_data, sync_etools_datamart

    record(database, "Amel_Association")
    monkeypatch.setattr(
        import_activityinfo_data, "import_data", lambda db, run, keep_copy: run.finish("succeeded")
    )
    call_command("import_activityinfo_data", "--database", str(database.ai_id))
    assert PartnerLink.objects.get(label="Amel_Association").partner == partners["amel"]
    assert [r.job for r in SyncRun.objects.order_by("started_at")] == ["ai_data", "partner_links"]

    def fake_sync(only=None, triggered_by=""):
        return [
            SyncRun.objects.create(
                job="etools_datamart", target=name, status="succeeded", triggered_by=triggered_by
            )
            for name in (only or ["partners"])
        ]

    monkeypatch.setattr(sync_etools_datamart, "sync_all", fake_sync)
    call_command("sync_etools_datamart", "--only", "grants")
    assert SyncRun.objects.filter(job="partner_links").count() == 1  # no partner or PD synced: no link run
    call_command("sync_etools_datamart", "--only", "partners,grants")
    assert SyncRun.objects.filter(job="partner_links").count() == 2


def test_migrate_locked_seeds_the_links(database, partners, monkeypatch):
    record(database, "Amel_Association")
    call_command("migrate_locked", verbosity=0)
    assert SyncRun.objects.filter(job="partner_links", triggered_by="migrate").count() == 1
    assert PartnerLink.objects.get(label="Amel_Association").partner == partners["amel"]
