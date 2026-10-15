"""The parity scripts' timing rows (``scripts/parity/timing.py pages <group>``) name rows of their group's matrix.

timing.py keeps the rows of ``matrix()`` whose label is in the group's ``TIMED``, then times the first one: a label
that matches no row leaves it nothing to time (R1 browser-6: Home's ``TIMED`` said "first load" where its rows say
"home first load", and timing.py stopped with an IndexError). Read from the source, so no world is needed; groups
whose matrix writes its labels at run time (f-strings) are left out.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PARITY = Path(__file__).resolve().parents[2] / "scripts" / "parity"


def _labels(path: Path) -> tuple[tuple[str, ...] | None, set[str], bool]:
    tree = ast.parse(path.read_text())
    timed, labels, dynamic = None, set(), False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TIMED" for t in node.targets):
            timed = tuple(ast.literal_eval(node.value))
        if isinstance(node, ast.FunctionDef) and node.name == "matrix":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Tuple) and inner.elts:
                    first = inner.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        labels.add(first.value)
                    elif isinstance(first, ast.JoinedStr):
                        dynamic = True
    return timed, labels, dynamic


GROUPS = sorted(p for p in PARITY.glob("*.py") if _labels(p)[0] is not None and not _labels(p)[2])


def test_home_is_among_the_groups_checked():
    assert PARITY / "home.py" in GROUPS and PARITY / "overview.py" in GROUPS


@pytest.mark.parametrize("path", GROUPS, ids=lambda p: p.stem)
def test_every_timed_label_is_a_row_of_the_matrix(path):
    timed, labels, _dynamic = _labels(path)
    assert timed, f"{path.stem}: TIMED is empty"
    assert [label for label in timed if label not in labels] == []


def test_home_times_its_first_load_and_the_review_page():
    from scripts.parity import home

    assert home.TIMED[0] == "home first load" and "review first load" in home.TIMED
    assert home.first_figure("/") == "standing" and home.first_figure("/daily-review/?section=") == "review"
