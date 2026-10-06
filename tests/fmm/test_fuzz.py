"""Hardening (stage 8b): the readers of eTools field monitoring records never raise, whatever shape a
record takes. The keys and shapes of the real records are not documented, so every reader is fed random
nested values: objects, lists, numbers, booleans, blanks, very long and odd texts, under the candidate
keys and under random ones. Pure readers are called directly; then the same shapes are stored as
finding, checklist answer, option and programme activity records, and a full and a scores-only refresh
must read them all without a failed row, and the visit pages and the chat's look-ups must still open.

Every draw comes from a seeded ``random.Random``, so a failure is reproducible from its seed."""

from __future__ import annotations

import datetime
import math
import random
from typing import Any

import pytest

from neurodb.datamart import fm
from neurodb.fmm import build, fields, parse, privacy
from neurodb.fmm.models import Visit

SEEDS = range(40)
ODD_TEXTS = (
    "",
    " ",
    "\t\n",
    "n/a",
    "N/A.",
    "On Track",
    "off-track",
    "Constrained",
    "yes",
    "لا",
    "نعم",
    "0",
    "-1",
    "1722",
    "007",
    "1e309",
    "NaN",
    "None",
    "null",
    "{'name': 'Karim Canary'}",
    "LEBA/PCA2023597/PD2025123-2",
    "PCA / PD",
    "école à Zahlé",
    "x" * 5000,
    "🙂" * 50,
    "a.b.c",
    "[]",
    "{}",
    "Mrs ",
    "karim.canary@example.org",
    "+961 3 123 456",
)
ALL_KEYS = sorted(
    {key for dataset in fields.CANDIDATES.values() for keys in dataset.values() for key in keys}
)
ODD_KEYS = (
    "",
    ".",
    "..",
    "a..b",
    "team.",
    ".name",
    "x" * 300,
    "0",
    "visit_lead",
    "monitoring_activity.id.id",
)


def _scalar(rng: random.Random, *, json_safe: bool) -> Any:
    pick = rng.randrange(12)
    if pick == 0:
        return None
    if pick == 1:
        return rng.choice((True, False))
    if pick == 2:
        return rng.choice((0, 1, -1, 1722, 2**31, -(2**63), 10**30))
    if pick == 3:
        floats = [0.0, -0.0, 1.5, 1722.0, 1e308, -1e-308]
        if not json_safe:
            floats += [math.nan, math.inf, -math.inf]
        return rng.choice(floats)
    if pick in (4, 5, 6):
        text = rng.choice(ODD_TEXTS)
        return text if json_safe else text + rng.choice(("", "\x00"))
    return "".join(rng.choice("abc XYZ0123-/_.é\n") for _ in range(rng.randrange(0, 40)))


def _key(rng: random.Random) -> str:
    pick = rng.randrange(4)
    if pick == 0:
        return rng.choice(ALL_KEYS).split(".")[0]
    if pick == 1:
        return rng.choice(ODD_KEYS)
    return rng.choice(
        ("id", "name", "text", "label", "value", "title", "reference_number", "email", "x", "1")
    )


def _value(rng: random.Random, depth: int = 0, *, json_safe: bool = True) -> Any:
    if depth >= 5 or rng.random() < 0.45:
        return _scalar(rng, json_safe=json_safe)
    if rng.random() < 0.5:
        return [_value(rng, depth + 1, json_safe=json_safe) for _ in range(rng.randrange(0, 5))]
    return {_key(rng): _value(rng, depth + 1, json_safe=json_safe) for _ in range(rng.randrange(0, 6))}


def _record(rng: random.Random, dataset: str, *, json_safe: bool = True) -> Any:
    """A record of ``dataset``: mostly an object holding some of its candidate keys (top level or
    dotted, as nested objects) with random values, sometimes something else altogether."""
    if rng.random() < 0.05:
        return _value(rng, json_safe=json_safe)  # not even an object
    record: dict[str, Any] = {"id": rng.randrange(1, 10**6)}
    candidates = [key for keys in fields.CANDIDATES[dataset].values() for key in keys]
    for key in rng.sample(candidates, k=min(len(candidates), rng.randrange(1, 12))):
        node = record
        *parents, last = key.split(".")
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[last] = _value(rng, json_safe=json_safe)
    for _ in range(rng.randrange(0, 4)):
        record[_key(rng)] = _value(rng, json_safe=json_safe)
    return record


# ------------------------------------------------------------------------------------------ pure readers
@pytest.mark.parametrize("seed", SEEDS)
def test_the_value_readers_never_raise(seed):
    rng = random.Random(seed)
    for _ in range(60):
        raw = _value(rng, json_safe=False)
        extra = _value(rng)
        record = {_key(rng): raw, **(extra if isinstance(extra, dict) else {})}
        for key in (*rng.sample(ALL_KEYS, 8), *ODD_KEYS):
            for kind in parse.KINDS:
                found = parse.value(record, key, kind)
                assert found is None or isinstance(found, str | int | bool | float)
                if kind == "number" and found is not None:
                    assert isinstance(found, float) and math.isfinite(found)
                if kind == "id" and found is not None:
                    assert isinstance(found, int) and found > 0
            parse.walk(record, key)
        assert all(isinstance(text, str) and text for text in parse.text_list(raw))
        assert all(isinstance(text, str) and text and ";" not in text for text in parse.split_list(raw))
        assert isinstance(parse.fold(raw), str)
        assert isinstance(parse.question_key(raw, _value(rng, json_safe=False)), str)
        assert parse.text_state(raw) in ("", "placeholder", "text")
        assert parse.word_count(raw) >= 0
        options = {(str(rng.randrange(20)), str(rng.randrange(4))): rng.choice(ODD_TEXTS)}
        answer = parse.read_answer(
            raw,
            _value(rng, json_safe=False),
            _value(rng, json_safe=False),
            options=options,
            question_key="12",
        )
        assert answer.answer_code in ("", "on_track", "constrained", "off_track", "yes", "no")
        assert not (answer.answered and answer.placeholder)
        assert parse.read_answer(raw).answer_words >= 0


@pytest.mark.parametrize("seed", SEEDS)
def test_the_vocabularies_and_the_programme_document_reader_never_raise(seed):
    rng = random.Random(1000 + seed)
    resolver = fm.PDResolver(
        [
            (1, "LEB/PCA2023597/PD2025123", "Learning support", 1, datetime.date(2025, 1, 1), None),
            (2, "LEB/PCA2023597/PD2025123-2", "Learning support", 1, None, datetime.date(2026, 12, 31)),
            (3, "LEB/SSFA2024001", "", None, None, None),
        ]
    )
    for _ in range(80):
        raw = _value(rng, json_safe=False)
        other = _value(rng, json_safe=False)
        assert fm.normalize_rating(raw) in fm.RATINGS
        assert fm.normalize_status(raw) in ("", *fm.STATUSES)
        assert fm.normalize_answer(raw) in ("", "on_track", "constrained", "off_track", "yes", "no", "other")
        assert fm.entity_kind(raw, other) in fm.KINDS
        token = fm.pd_token(raw)
        assert token is None or (isinstance(token, tuple) and len(token) == 2)
        key = fm.visit_key(raw, other, rng.randrange(1, 10**9))
        assert key and len(key) <= fm.VISIT_KEY_MAX and key.replace("-", "").isalnum()
        assert isinstance(fm.norm_reference(raw), str)
        entity = parse.as_kind(raw, "text") or ""
        pk, how = resolver.resolve(entity, fm.entity_kind(other, entity), rng.choice((None, 1, 2)), None)
        assert pk in (None, 1, 2, 3) and how in ("", "exact", "token", "base", "title")
        pk, how = resolver.resolve_reference(entity, rng.choice((None, datetime.date(2026, 5, 1))))
        assert pk in (None, 1, 2, 3)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_privacy_readers_never_raise_and_never_give_an_email(seed):
    rng = random.Random(2000 + seed)
    names = frozenset({"karim canary", "rania canary"})
    for _ in range(60):
        raw = _value(rng, json_safe=False)
        key = _key(rng)
        assert isinstance(privacy.person_like(key), bool)
        assert isinstance(privacy.names_person(key), bool)
        assert isinstance(privacy.example_text(raw), str)
        example = privacy.example(rng.choice(list(fields.CANDIDATES)), key, raw, names)
        assert isinstance(example, str) and len(example) <= 60 + 1
        assert "@example.org" not in example
        shown, unnamed = privacy.person_display(raw)
        assert unnamed >= 0 and all(isinstance(n, str) and "@" not in n for n in shown)
        text, inserted = privacy.clean(raw, rng.randrange(0, 700), names)
        assert isinstance(text, str) and inserted >= 0 and "karim.canary@example.org" not in text


@pytest.mark.parametrize("seed", SEEDS)
def test_the_key_probe_reads_any_record(seed):
    rng = random.Random(3000 + seed)
    for dataset in fields.CANDIDATES:
        probe = fields.Probe(dataset)
        for _ in range(25):
            probe.add(_record(rng, dataset, json_safe=False))  # a reader that raised would fail here
        mapping = probe.as_mapping()
        assert mapping["total"] == 25
        for field in fields.CANDIDATES[dataset]:
            chosen = fields.resolve(dataset, field, mapping, None)
            assert 0 <= chosen.coverage <= 1
            assert chosen.chosen_key == "" or chosen.chosen_key in fields.CANDIDATES[dataset][field]
        counts = probe.answer_counts({"answer": "answer", "answer_label": None, "summary": "summary"})
        assert counts["records"] == 25


@pytest.mark.parametrize("seed", range(10))
def test_fit_cuts_any_value_to_its_column(seed):
    rng = random.Random(4000 + seed)
    for _ in range(40):
        raw = _value(rng, json_safe=False)
        text = build.fit(Visit, "place_name", raw)
        assert isinstance(text, str) and len(text) <= 254
        listed = build.fit_list(Visit, "offices", raw if isinstance(raw, list) else [raw])
        assert len(listed) <= 50 and all(isinstance(v, str) and len(v) <= 200 for v in listed)


# ------------------------------------------------------------------------------------------ the refresh
def _stored(record: Any) -> Any:
    """A record as the store keeps it: never SQL NULL (a JSON ``null`` record is kept as an empty list)."""
    return [] if record is None else record


def _plant(rng: random.Random, fm_world) -> int:
    """Random records beside the world's own: finding rows whose record has a random shape (their typed
    columns as the sync would fill them), checklist answers, options and programme activities, some
    joined to the world's activities and some to nothing. Returns the number of finding rows added."""
    from neurodb.datamart import models as dm

    activities = (1722, 1723, 1726, 1727, 1724, 1999)
    added = 0
    for n in range(40):
        record = _record(rng, "field_monitoring")
        dm.MonitoringFinding.objects.create(
            datamart_id=900_000 + n,
            monitoring_activity_id=rng.choice((*activities, None)),
            monitoring_activity=rng.choice(("FM-2026-022", "FM/2026/9", "", "fm-2026-777")),
            entity=rng.choice(("", "LEBA/PCA2023597/PD2025123", "Amel Association", "2.2 X", "x" * 255)),
            entity_type=rng.choice(("", "PD/SSFA", "CP Output", "Partner", "weird")),
            overall_finding_rating=rng.choice(("", "On Track", "Off Track", "Not Monitored", "Partially")),
            status=rng.choice(("", "completed", "Data Collection", "canceled", "in review")),
            narrative_finding=rng.choice(("", "n/a", "Classes delayed and suspended. " * 20, "x" * 3000)),
            end_date=rng.choice((None, datetime.date(2026, 5, 12), datetime.date(2026, 9, 1))),
            partner=rng.choice((None, fm_world.partners["amel"], fm_world.partners["mercy"])),
            data=_stored(record),
        )
        added += 1
    for n in range(120):
        record = _record(rng, "fm_questions")
        if isinstance(record, dict) and rng.random() < 0.6:  # often joined to a real activity
            record["monitoring_activity_id"] = rng.choice(activities)
        dm.DatamartDocument.objects.create(
            dataset="fm_questions", record_key=f"fuzz-q-{n}", data=_stored(record), title="fuzz"
        )
    for dataset in ("fm_options", "fm_programme_activities", "offices", "sections"):
        for n in range(20):
            dm.DatamartDocument.objects.create(
                dataset=dataset,
                record_key=f"fuzz-{dataset}-{n}",
                data=_stored(_record(rng, dataset)),
                title="fuzz",
            )
    return added


@pytest.mark.parametrize("seed", range(4))
def test_a_refresh_over_random_records_never_fails_a_row(fm_world, seed, client, admin_user):
    from django.urls import reverse

    from neurodb.core.models import SyncRun
    from neurodb.fmm import refresh
    from neurodb.fmm.ai import tools
    from neurodb.fmm.scope import Scope

    added = _plant(random.Random(5000 + seed), fm_world)
    today = datetime.date(2026, 10, 5)
    full = refresh.run(triggered_by="test", today=today)
    assert full.status == SyncRun.Status.SUCCEEDED, (full.error, full.details)
    assert full.rows_failed == 0 and full.rows_written == Visit.objects.count() > 0
    assert full.rows_in >= added + 120  # every random finding row and checklist record was read
    scores = refresh.run(triggered_by="test", scores_only=True, today=today)
    assert scores.status == SyncRun.Status.SUCCEEDED and scores.rows_failed == 0, scores.error
    assert not Visit.objects.filter(rules_version=0).exists()

    # every visit page opens (it reads the answers back from their records), and so do the look-ups
    client.force_login(admin_user)
    for key in Visit.objects.values_list("key", flat=True):
        assert client.get(reverse("fmm:visit", args=[key])).status_code == 200, key
    ctx = tools.ChatContext(
        scope=Scope.from_params({"year": "2026", "section": ""}), texts_left=50, cards_max=15
    )
    with tools.bind(ctx):
        for key in Visit.objects.values_list("key", flat=True):
            found = tools.fm_visit(key)
            assert "error" not in found or "not in the current filter" in found["error"], found
        assert "matches" in tools.fm_search("delayed")
        assert "matches" in tools.fm_search("registers")
    hits = parse.search_texts(list(Visit.objects.values_list("key", flat=True)), "on track", 10)
    assert all(hit.where in ("narrative", "answer", "summary") for hit in hits)
