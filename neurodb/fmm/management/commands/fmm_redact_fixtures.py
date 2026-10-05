"""``fmm_redact_fixtures [path ...] [--dry-run]``: make recorded field monitoring samples safe to commit.

``record_datamart_samples`` removes contact keys (e-mail, phone) from the records it writes to
``tests/fixtures/datamart/``. The field monitoring records also name the visit lead and the team, and
their free texts (narratives, answers, summaries) can name anyone. Before those fixtures are
committed, this command rewrites them in place:

- every text under a key that holds a person (``privacy.person_like``: "visit_lead", "team_members",
  "person_responsible", "first_name"...) becomes "Person 1", "Person 2"... (the same person keeps the
  same number across the files; numbers and yes/no stay);
- every other text is cleaned (``privacy.clean``): e-mail addresses, links, the names NeuroDB knows,
  the names found under person keys in these files, phone numbers and names after a title are
  replaced by placeholders, and a text longer than 300 characters is cut.

With a folder (the default: ``tests/fixtures/datamart/``) it rewrites the datasets recorded for
Monitoring insights (:data:`DATASETS`); a file named on the command line is rewritten whatever its
dataset. Read the diff before committing: a name NeuroDB does not know, written without a title in a
short text, can remain.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from neurodb.datamart import catalogue
from neurodb.fmm import privacy
from neurodb.integrations.etools import samples

DATASETS = frozenset(
    {
        *catalogue.FM_PRIVATE,
        "offices",
        "sections",
        "action_points",
        "intervention_locations",
        "location_sites",
    }
)
TEXT_CHARS = 300
PERSON = re.compile(r"^Person \d+$")


class Redactor:
    """Rewrites records: person texts to "Person N", other texts cleaned and cut."""

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self.persons: dict[str, str] = {}
        self.counts: Counter[str] = Counter()

    def person(self, text: str) -> str:
        if PERSON.match(text):
            return text
        key = " ".join(text.casefold().split())
        if key not in self.persons:
            self.persons[key] = f"Person {len(self.persons) + 1}"
        self.counts["persons"] += 1
        return self.persons[key]

    def value(self, value: Any, under_person: bool = False) -> Any:
        if isinstance(value, dict):
            return {k: self.value(v, under_person or privacy.person_like(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, under_person) for v in value]
        if not isinstance(value, str) or not value.strip():
            return value
        if under_person:
            return self.person(value)
        cleaned, placeholders = privacy.clean(value, TEXT_CHARS, self.names)
        flat = " ".join(value.split())
        if len(flat) > TEXT_CHARS:
            self.counts["cut"] += 1
        if placeholders:
            self.counts["texts"] += 1
        # a text that needed nothing keeps its line breaks and spaces
        return cleaned if (placeholders or len(flat) > TEXT_CHARS) else value


def person_texts(value: Any, under_person: bool = False) -> list[str]:
    """Every text written under a key that holds a person, at any depth."""
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in person_texts(v, under_person or privacy.person_like(k))]
    if isinstance(value, list):
        return [t for v in value for t in person_texts(v, under_person)]
    return [value] if under_person and isinstance(value, str) and value.strip() else []


class Command(BaseCommand):
    help = (
        "Replace people by 'Person N' and clean the texts of recorded field monitoring samples "
        "(tests/fixtures/datamart) before they are committed"
    )

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="*", help=f"files or folders (default {samples.FIXTURES})")
        parser.add_argument("--dry-run", action="store_true", help="count what would change; write nothing")

    def handle(self, *args, **options):
        files = self._files([Path(p) for p in options["paths"]] or [samples.FIXTURES])
        if not files:
            self.stdout.write("No recorded field monitoring sample to rewrite.")
            return
        loaded = {path: json.loads(path.read_text(encoding="utf-8")) for path in files}
        found = [t for sample in loaded.values() for t in person_texts(sample.get("results", []))]
        redactor = Redactor(privacy.names() | privacy.name_forms(found))
        for path, sample in loaded.items():
            before = Counter(redactor.counts)
            sample["results"] = [redactor.value(record) for record in sample.get("results", [])]
            done = redactor.counts - before
            if not options["dry_run"]:
                path.write_text(
                    json.dumps(sample, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8"
                )
            self.stdout.write(
                f"{path.name}: {len(sample['results'])} records, {done['persons']} person values replaced, "
                f"{done['texts']} texts redacted, {done['cut']} cut to {TEXT_CHARS} characters"
                + (" (dry run: not written)" if options["dry_run"] else "")
            )
        self.stdout.write(
            f"{len(redactor.persons)} distinct people replaced. Read the diff before committing."
        )

    def _files(self, paths: list[Path]) -> list[Path]:
        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                for candidate in sorted(path.glob("*.json")):
                    try:
                        dataset = json.loads(candidate.read_text(encoding="utf-8")).get("dataset")
                    except (ValueError, AttributeError):
                        continue
                    if dataset in DATASETS:
                        files.append(candidate)
            elif path.is_file():
                files.append(path)
            else:
                raise CommandError(f"{path} does not exist")
        return files
