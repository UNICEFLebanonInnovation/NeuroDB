"""Make a parity world large: the demo world multiplied by K (SPEC7 §3.5, step 1).

    createdb neurodb_parity_<stage>
    DATABASE_URL=.../neurodb_parity_<stage> python manage.py migrate
    DATABASE_URL=.../neurodb_parity_<stage> python manage.py seed_demo
    DATABASE_URL=.../neurodb_parity_<stage> python -m scripts.parity.scale K [--sync-days 365] [--checkout DIR]

Only on a database whose name starts with ``neurodb_parity_`` (``core.check_database``: never ``neurodb_v3``,
``test_neurodb_v3`` or a system database), and only once: a world already multiplied is refused.

1. The demo eTools programme documents, with their locations, PD indicators, partner reports, funds
   reservations (headers and lines), TPM activities and their locations, action points and monitoring
   findings, and the ActivityInfo records, are copied K times by ``INSERT ... SELECT``. Copy k gets ids and
   references shifted by k x 10,000 (programme documents), k x 10,000,000 (Datamart ids) and k x 1,000,000
   (TPM activities), and reference numbers ending in ``-c<k>``.
2. A year of sync-run history: ``--sync-days`` days x 2.5 K runs a day (18,250 for K = 20, about what a year
   in production holds), over the jobs the demo already ran, 1 in 40 failed and 1 in 25 partial. Every added
   run finishes before the oldest run already there, so the last run of each job (the freshness strip, the
   daily review's sync checks, the overview's cache key) does not change.

Everything is one transaction; the tables are analysed afterwards. Then run the jobs that read the data
(``build_knowledge_hub``, ``daily_review``, ``forecast_indicators``, ``run_watch --daily``) and take the snapshot
(``pg_dump -Fc``) that record and check both restore.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.parity import core

PCA_OFF = 10_000
DM_OFF = 10_000_000
ROW_OFF = 1_000_000
RUNS_PER_DAY_PER_COPY = 2.5
HISTORY = "parity history"  # triggered_by of the added sync runs


def _columns(cursor, table: str) -> list[str]:
    cursor.execute(
        "select column_name from information_schema.columns where table_name = %s order by ordinal_position",
        [table],
    )
    return [row[0] for row in cursor.fetchall()]


def _clone(cursor, table: str, replace: dict[str, str], where: str, *, keep_id: bool = False) -> int:
    """Copy the rows of ``table`` that match ``where``, with ``replace`` giving the SQL of changed columns."""
    columns = _columns(cursor, table)
    if not keep_id:
        columns = [c for c in columns if c != "id"]
    chosen = ", ".join(replace.get(c, f'"{c}"') for c in columns)
    names = ", ".join(f'"{c}"' for c in columns)
    cursor.execute(f"INSERT INTO {table} ({names}) SELECT {chosen} FROM {table} WHERE {where}")
    return cursor.rowcount


def _suffixed(column: str, k: int) -> str:
    return (
        f'CASE WHEN "{column}" IS NULL OR "{column}" = \'\' THEN "{column}" ELSE "{column}" || \'-c{k}\' END'
    )


def multiply(cursor, copies: int) -> dict[str, int]:
    """Step 1 of the module docstring. Returns the rows added per table."""
    cursor.execute("select count(*) from etools_pca where id >= %s", [PCA_OFF])
    if cursor.fetchone()[0]:
        raise SystemExit("this world is already multiplied: start again from migrate and seed_demo")
    cursor.execute("select coalesce(max(id), 0) from pivoting_activityreportnew")
    activityinfo_max = cursor.fetchone()[0]
    total: dict[str, int] = {}
    for k in range(1, copies + 1):
        pca, dm, row = k * PCA_OFF, k * DM_OFF, k * ROW_OFF
        added = {
            "etools_pca": _clone(
                cursor,
                "etools_pca",
                {"id": f"id + {pca}", "etl_id": f"etl_id || '-c{k}'", "number": _suffixed("number", k)},
                f"id < {PCA_OFF}",
                keep_id=True,
            ),
            "etools_pca_locations": _clone(
                cursor, "etools_pca_locations", {"pca_id": f"pca_id + {pca}"}, f"pca_id < {PCA_OFF}"
            ),
            "datamart_pdindicator": _clone(
                cursor,
                "datamart_pdindicator",
                {
                    "datamart_id": f"datamart_id + {dm}",
                    "source_id": f"source_id + {dm}",
                    "intervention_id": f"intervention_id + {pca}",
                    "pd_reference_number": _suffixed("pd_reference_number", k),
                },
                f"datamart_id < {DM_OFF}",
            ),
            "datamart_reportedindicator": _clone(
                cursor,
                "datamart_reportedindicator",
                {
                    "datamart_id": f"datamart_id + {dm}",
                    "intervention_id": f"intervention_id + {pca}",
                    "pd_reference_number": _suffixed("pd_reference_number", k),
                    "progress_report": _suffixed("progress_report", k),
                    "etools_indicator_id": (
                        "CASE WHEN etools_indicator_id ~ '^[0-9]+$' THEN "
                        f"(etools_indicator_id::bigint + {dm})::text ELSE etools_indicator_id END"
                    ),
                },
                f"datamart_id < {DM_OFF}",
            ),
        }
        for table in ("datamart_fundsreservationheader", "datamart_fundsreservation"):
            added[table] = _clone(
                cursor,
                table,
                {
                    "datamart_id": f"datamart_id + {dm}",
                    "intervention_id": f"intervention_id + {pca}",
                    "pd_reference_number": _suffixed("pd_reference_number", k),
                    "fr_number": _suffixed("fr_number", k),
                },
                f"datamart_id < {DM_OFF}",
            )
        added["datamart_tpmactivity"] = _clone(
            cursor,
            "datamart_tpmactivity",
            {
                "id": f"id + {row}",
                "datamart_id": f"datamart_id + {dm}",
                "intervention_id": f"intervention_id + {pca}",
                "pd_reference_number": _suffixed("pd_reference_number", k),
            },
            f"id < {ROW_OFF}",
            keep_id=True,
        )
        added["datamart_tpmactivity_location_links"] = _clone(
            cursor,
            "datamart_tpmactivity_location_links",
            {"tpmactivity_id": f"tpmactivity_id + {row}"},
            f"tpmactivity_id < {ROW_OFF}",
        )
        added["datamart_actionpoint"] = _clone(
            cursor,
            "datamart_actionpoint",
            {
                "datamart_id": f"datamart_id + {dm}",
                "intervention_id": f"intervention_id + {pca}",
                "reference_number": _suffixed("reference_number", k),
                "tpm_activity_id": f"tpm_activity_id + {row}",
            },
            f"datamart_id < {DM_OFF}",
        )
        added["datamart_monitoringfinding"] = _clone(
            cursor,
            "datamart_monitoringfinding",
            {"datamart_id": f"datamart_id + {dm}", "intervention_id": f"intervention_id + {pca}"},
            f"datamart_id < {DM_OFF}",
        )
        added["pivoting_activityreportnew"] = _clone(
            cursor, "pivoting_activityreportnew", {}, f"id <= {activityinfo_max}"
        )
        for table, n in added.items():
            total[table] = total.get(table, 0) + n
    for table in ("etools_pca", "datamart_tpmactivity"):  # identity sequences past the explicit ids
        cursor.execute(
            f"select setval(pg_get_serial_sequence('{table}', 'id'), (select max(id) from {table}))"
        )
    return total


def sync_history(cursor, days: int, per_day: int) -> int:
    """Step 2 of the module docstring. Returns the runs added."""
    cursor.execute("select count(*) from core_syncrun where triggered_by = %s", [HISTORY])
    if cursor.fetchone()[0]:
        raise SystemExit("the sync-run history is already there")
    cursor.execute("select min(started_at) from core_syncrun")
    (oldest,) = cursor.fetchone()
    cursor.execute("select distinct job from core_syncrun order by job")
    jobs = [row[0] for row in cursor.fetchall()]
    if oldest is None or not jobs:
        raise SystemExit("no sync run to build a history on: run seed_demo first")
    # run n of day d: job jobs[n % len], 1 in 40 failed, 1 in 25 partial, the rest succeeded; targets 0-5 for
    # the ActivityInfo data job (one run per database), empty otherwise
    cursor.execute(
        """
        insert into core_syncrun
            (job, target, status, started_at, finished_at, rows_in, rows_written, rows_failed,
             error, details, triggered_by)
        select
            (%(jobs)s::text[])[1 + (n %% cardinality(%(jobs)s::text[]))],
            case when (%(jobs)s::text[])[1 + (n %% cardinality(%(jobs)s::text[]))] = 'ai_data'
                 then (n %% 6)::text else '' end,
            case when n %% 40 = 7 then 'failed' when n %% 25 = 3 then 'partial' else 'succeeded' end,
            %(oldest)s - make_interval(days => d, mins => 1 + n * 25) - interval '5 minutes',
            %(oldest)s - make_interval(days => d, mins => 1 + n * 25),
            case when n %% 40 = 7 then 0 else 1200 end,
            case when n %% 40 = 7 then 0 when n %% 25 = 3 then 1180 else 1200 end,
            case when n %% 25 = 3 and n %% 40 <> 7 then 20 else 0 end,
            case when n %% 40 = 7 then 'Upstream returned 503' else '' end,
            '{}'::jsonb,
            %(history)s
        from generate_series(1, %(days)s) as d, generate_series(0, %(per_day)s - 1) as n
        """,
        {"jobs": jobs, "oldest": oldest, "days": days, "per_day": per_day, "history": HISTORY},
    )
    return cursor.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.parity.scale", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("copies", type=int, help="K: how many copies of the demo world to add")
    parser.add_argument("--sync-days", type=int, default=365, help="days of sync-run history (0: none)")
    parser.add_argument("--checkout", type=Path, default=None, help="the code to run (default: this tree)")
    args = parser.parse_args(argv)
    if args.copies < 1:
        raise SystemExit("K is at least 1")
    info = core.setup(args.checkout, contexts=False)
    from django.db import connection, transaction

    per_day = max(1, round(RUNS_PER_DAY_PER_COPY * args.copies))
    with transaction.atomic(), connection.cursor() as cursor:
        added = multiply(cursor, args.copies)
        runs = sync_history(cursor, args.sync_days, per_day) if args.sync_days else 0
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    print(f"{info['database']}: {args.copies} copies added")
    for table, n in added.items():
        print(f"  {table:40} +{n:,}")
    print(f"  {'core_syncrun (history)':40} +{runs:,} ({args.sync_days} days x {per_day} a day)")
    return 0


if __name__ == "__main__":
    from scripts.parity import scale as _scale

    raise SystemExit(_scale.main())
