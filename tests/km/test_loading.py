"""The KM Database's load (SPEC_km §2.7): grouped problems in the workbook's words with Excel row numbers, the
fewer-rows guard, warnings, one transaction, batches, the lock, stale versions, the command, the version
shown. Fake generated workbooks only."""

from __future__ import annotations

import datetime

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from neurodb.km import loading, models, workbook
from neurodb.km.models import KMDocument, KMFinding, KMVersion
from tests.km import factory

pytestmark = pytest.mark.django_db

FIND, DOCS = factory.FINDINGS_SHEET, factory.DOCUMENTS_SHEET


def _rows() -> set:
    return set(KMFinding.objects.values_list("finding_id", "key_finding"))


@pytest.fixture
def shown():
    """A version already shown: 3 documents, 4 findings."""
    version = factory.load(factory.workbook())
    assert version.status == "live"
    return version


def _assert_unchanged(shown, rows):
    shown.refresh_from_db()
    assert shown.status == "live" and _rows() == rows


# ------------------------------------------------------------------------------------------ row problems
def test_missing_repeated_and_malformed_ids_are_grouped_with_excel_rows(shown):
    rows = _rows()
    edits = {
        ("findings", 3, "Finding ID"): None,
        ("findings", 5, "Finding ID"): None,
        ("findings", 6, "Finding ID"): factory.finding_id(1, 1),  # repeats row 2's
        ("findings", 7, "Finding ID"): "TST 0001 F7",  # a space
        ("documents", 4, "Record ID"): factory.record_id(1),  # repeats row 2's
    }
    version = factory.load(factory.workbook(findings=8, edits=edits))
    assert version.status == "failed" and version.content is None
    assert version.problems == [
        f"1 values of “Record ID” are repeated in “{DOCS}”, for example TST-0001 (rows 2 and 4).",
        f"2 rows of “{FIND}” have no “Finding ID”, for example rows 3, 5.",
        f"1 values of “Finding ID” in “{FIND}” hold characters an ID may not have (only letters, digits, “.”, "
        "“_” and “-”, at most 40), for example rows 7.",
        f"1 values of “Finding ID” are repeated in “{FIND}”, for example TST-0001-F00001 (rows 2 and 6).",
        # the documents sheet's row 4 no longer gives TST-0003: its findings are in rows 4 and 7
        f"2 rows of “{FIND}” name a “Record ID” that is not in “{DOCS}”, for example rows 4, 7.",
    ]
    _assert_unchanged(shown, rows)


def test_orphan_findings_name_five_rows_at_most(shown):
    rows = _rows()
    edits = {("findings", n, "Record ID"): "TST-9999" for n in range(2, 12)}
    version = factory.load(factory.workbook(findings=12, edits=edits))
    assert version.status == "failed"
    assert version.problems == [
        f"10 rows of “{FIND}” name a “Record ID” that is not in “{DOCS}”, for example rows 2, 3, 4, 5, 6."
    ]
    _assert_unchanged(shown, rows)


def test_a_finding_with_no_record_id_is_named(shown):
    version = factory.load(factory.workbook(edits={("findings", 2, "Record ID"): None}))
    assert version.problems == [f"1 rows of “{FIND}” have no “Record ID”, for example rows 2."]


def test_too_many_rows_are_refused(shown, monkeypatch):
    rows = _rows()
    monkeypatch.setattr(workbook, "MAX_FINDINGS", 3)
    version = factory.load(factory.workbook(findings=5))
    assert version.problems == [
        "The workbook holds more rows than the KM Database takes (5,000 documents, 100,000 findings)."
    ]
    _assert_unchanged(shown, rows)


def test_a_layout_problem_found_again_by_the_load_fails_the_version():
    version = factory.load(factory.workbook(drop=("Key Finding",)))
    assert version.status == "failed"
    assert version.problems == [f"The sheet “{FIND}” has no column “Key Finding”."]
    unreadable = factory.load(b"not a workbook at all")
    assert unreadable.status == "failed" and unreadable.content is None
    assert unreadable.error == (
        "The load stopped before it finished (Unreadable). The KM Database still shows the version it showed before."
    )


# -------------------------------------------------------------------------------------- fewer rows guard
def test_fewer_findings_than_the_version_shown_is_refused_unless_ticked():
    factory.load(factory.workbook(documents=3, findings=20))
    rows = _rows()
    smaller = factory.workbook(documents=3, findings=17)
    version = factory.load(smaller)
    assert version.status == "failed"
    assert version.problems == [
        "This workbook has 17 findings; the version shown now has 20. Nothing was loaded. If fewer findings is "
        "expected, upload it again and tick “It has fewer findings on purpose”."
    ]
    assert _rows() == rows
    assert factory.load(smaller, fewer_ok=True).status == "live"
    assert KMFinding.objects.count() == 17
    # 90% or more loads without the tick
    assert factory.load(factory.workbook(documents=3, findings=16)).status == "live"


def test_fewer_documents_is_refused_too():
    factory.load(factory.workbook(documents=10, findings=10))
    version = factory.load(factory.workbook(documents=5, findings=10))
    assert version.problems == [
        "This workbook has 5 documents; the version shown now has 10. Nothing was loaded. If fewer documents "
        "is expected, upload it again and tick “It has fewer findings on purpose”."
    ]


def test_fewer_rows_than_the_read_me_states_is_refused_unless_ticked():
    data = factory.workbook(findings=4, stated=(6, 3))
    version = factory.load(data)
    assert version.problems == [
        f"This workbook's Read Me sheet says it has 6 findings, but “{FIND}” has 4 rows. Nothing was loaded. If "
        "that is expected, upload it again and tick “It has fewer findings on purpose”."
    ]
    ticked = factory.load(data, fewer_ok=True)
    assert ticked.status == "live"
    assert f"The workbook's Read Me sheet says it has 6 findings; “{FIND}” has 4 rows." in ticked.warnings
    more = factory.load(factory.workbook(findings=4, stated=(2, 3)))
    assert more.status == "live"
    assert f"The workbook's Read Me sheet says it has 2 findings; “{FIND}” has 4 rows." in more.warnings


# ------------------------------------------------------------------------------------------------ warnings
def test_warnings_name_twenty_ids_at_most_and_link_the_page():
    edits = {("findings", n, "Evidence Confidence Level"): "Fake level" for n in range(2, 27)}
    edits[("findings", 30, "Data Quality Status")] = "Review required: fake reason"
    edits[("documents", 2, "Number of Findings")] = 99
    version = factory.load(factory.workbook(documents=3, findings=30, edits=edits))
    assert version.status == "live"
    check = next(w for w in version.warnings if "hold a value to check" in w)
    assert check.startswith(
        "25 findings hold a value to check (shown as given and marked), in “Evidence Confidence Level” (25); "
        "for example TST-0001-F00001, TST-0002-F00001, "
    )
    assert check.count("TST-") == 20 and check.endswith(". See them all on the page.")
    assert (
        "1 findings and 0 documents are marked for review in the workbook (“Data Quality Status”). See them on "
        "the page."
    ) in version.warnings
    assert (
        f"1 documents give a “Number of Findings” different from their rows in “{FIND}”, for example TST-0001."
    ) in version.warnings
    assert (version.review_count, version.check_count) == (1, 25)
    assert KMFinding.objects.exclude(checks={}).count() == 25
    shown = KMFinding.objects.get(finding_id=factory.finding_id(1, 1))
    assert shown.checks == {"Evidence Confidence Level": "Fake level"} and shown.confidence == ""
    assert shown.confidence_text == "Fake level"


def test_documents_with_no_findings_are_named():
    version = factory.load(factory.workbook(documents=4, findings=3))
    assert f"1 documents have no rows in “{FIND}”: TST-0004." in version.warnings


# ---------------------------------------------------------------------------------- the replace and batches
def test_a_load_replaces_every_row(shown):
    edits = {("findings", 2, "Key Finding"): "Fake new key finding"}
    newer = factory.load(factory.workbook(documents=2, findings=4, edits=edits), fewer_ok=True)
    assert newer.status == "live" and newer.content is None
    shown.refresh_from_db()
    assert shown.status == "replaced" and shown.content is None
    assert KMDocument.objects.count() == 2 and KMFinding.objects.count() == 4
    assert KMFinding.objects.get(finding_id=factory.finding_id(1, 1)).key_finding == "Fake new key finding"
    assert KMVersion.objects.filter(status="live").count() == 1


def test_a_load_that_stops_after_the_deletes_leaves_the_old_rows(shown, monkeypatch):
    rows = _rows()

    def broken():
        raise RuntimeError("fake failure")

    monkeypatch.setattr(loading, "_fill_vectors", broken)
    version = factory.load(factory.workbook(findings=6))
    assert version.status == "failed" and version.content is None
    assert version.error == (
        "The load stopped before it finished (RuntimeError). The KM Database still shows the version it showed "
        "before."
    )
    _assert_unchanged(shown, rows)
    with pytest.raises(CommandError):
        call_command(
            "load_km_database", "--version-id", str(factory.version(factory.workbook(findings=6)).pk)
        )


def test_findings_are_written_one_batch_at_a_time(monkeypatch):
    monkeypatch.setattr(loading, "FINDING_BATCH", 2)
    sizes = []
    original = KMFinding.objects.bulk_create

    def counted(objs, *args, **kwargs):
        sizes.append(len(objs))
        return original(objs, *args, **kwargs)

    monkeypatch.setattr(KMFinding.objects, "bulk_create", counted)
    assert factory.load(factory.workbook(findings=5)).status == "live"
    assert sizes == [2, 2, 1]


def test_both_vectors_are_filled_and_the_load_is_measured(shown):
    assert not KMFinding.objects.filter(search_vector__isnull=True).exists()
    assert not KMFinding.objects.filter(own_vector__isnull=True).exists()
    assert not KMDocument.objects.filter(search_vector__isnull=True).exists()
    assert shown.peak_memory_mb > 0 and shown.load_seconds is not None and shown.finished_at is not None
    assert (shown.documents_count, shown.findings_count) == (3, 4)


def test_only_one_version_is_shown(shown):
    with pytest.raises(IntegrityError), transaction.atomic():
        factory.version(b"fake", status="live")


# --------------------------------------------------------------------------------------- the lock and stale
@pytest.fixture
def other_session():
    """A second database session: a lock it holds is held by another process for the load."""
    other = connections.create_connection("default")
    yield other
    other.close()


def test_a_load_while_another_holds_the_lock_fails_with_its_sentence(other_session):
    queued = factory.version(factory.workbook())
    with other_session.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [loading.LOCK_ID])
    try:
        ended = loading.run(queued.pk)
        assert loading.close_stale() == 0  # the lock is held: nothing is stale
    finally:
        with other_session.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [loading.LOCK_ID])
    assert ended.status == "failed" and ended.error == "Another load was running. Upload again in a minute."
    assert KMVersion.objects.get(pk=queued.pk).content is None


def test_stale_versions_are_closed_when_no_load_runs():
    stopped = factory.version(b"fake", status="loading")
    young = factory.version(b"fake")
    old = factory.version(b"fake")
    KMVersion.objects.filter(pk=old.pk).update(uploaded_at=timezone.now() - datetime.timedelta(minutes=3))
    assert loading.close_stale() == 2
    stopped.refresh_from_db()
    old.refresh_from_db()
    young.refresh_from_db()
    assert stopped.status == "failed" and stopped.content is None
    assert (
        stopped.error
        == "The load stopped before it finished (the server restarted). Upload the workbook again."
    )
    assert old.status == "failed" and old.error == "The load did not start. Upload the workbook again."
    assert young.status == "queued"  # it may still start


def test_a_version_no_longer_waiting_is_left_alone():
    failed = factory.version(b"fake", status="failed", error="fake earlier failure")
    assert loading.run(failed.pk) is None
    failed.refresh_from_db()
    assert failed.status == "failed" and failed.error == "fake earlier failure"
    call_command("load_km_database", "--version-id", str(failed.pk))  # exits quietly (status 0)


def test_the_command_loads_and_says_so(capsys):
    queued = factory.version(factory.workbook(), fewer_ok=False)
    call_command(
        "load_km_database", "--version-id", str(queued.pk), "--triggered-by", "fake-admin", "--fewer-ok"
    )
    assert "is shown now: 3 documents, 4 findings (fake-admin)" in capsys.readouterr().out
    assert KMVersion.objects.get(pk=queued.pk).status == "live"


def test_the_version_shown_is_seen_within_the_memo_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(models.time, "monotonic", lambda: clock[0])
    assert models.live() is None
    version = factory.version(b"fake", status="live")  # made live by another process
    assert models.live() is None  # kept for 10 seconds
    clock[0] += models.LIVE_MEMO_SECONDS + 0.1
    assert models.live().pk == version.pk
