"""A Django comment ({# … #}) must fit on one line: one that runs over two lines is not a comment and
shows on the page as text (it did once, above the Overview's For you card)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "neurodb"
OPEN_COMMENT = re.compile(r"\{#(?:(?!#\}).)*\n", re.S)


def test_no_template_comment_runs_over_two_lines():
    broken = [
        f"{path.relative_to(ROOT.parent)}:{text[: m.start()].count(chr(10)) + 1}"
        for path in sorted(ROOT.rglob("templates/**/*.html"))
        for text in [path.read_text(encoding="utf-8")]
        for m in OPEN_COMMENT.finditer(text)
    ]
    assert broken == [], "use {% comment %}…{% endcomment %} for comments over several lines"
