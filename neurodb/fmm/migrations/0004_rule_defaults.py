"""Seed the quality rules R1-R6, the score settings and rules version 1 ("Defaults").

The values are written here as they were on the day this migration was written (NeuroDB's proposal,
apart from the points and R2's 80%, which follow the reference dashboard); later changes are made in
the admin and versioned. Running it again changes nothing; the reverse leaves the rows."""

from django.db import migrations

RULES = [
    {
        "code": "R1",
        "label": "Completeness",
        "points": 15,
        "threshold": None,
        "params": {"required": ["narrative", "q2"]},
        "description": "The report has a general observation (narrative), on every rated entity, and an "
        "answer to Q2 (activities monitored). A rating for every entity and the place of "
        "the visit can be required too. Points in proportion to the elements present.",
    },
    {
        "code": "R2",
        "label": "Evidence sufficiency",
        "points": 20,
        "threshold": 80,
        "params": {"require_unanswered_seen": True},
        "description": "The share of the checklist questions answered, counted once per question and "
        "entity: full points at the target share (80%) or above, fewer below it. Not "
        "available when eTools sends no unanswered question at all, since every visit "
        "would then pass.",
    },
    {
        "code": "R3",
        "label": "HACT alignment",
        "points": 20,
        "threshold": None,
        "params": {"strict": False},
        "description": "Every rated entity's overall finding agrees with its HACT Q1 answer (its own, "
        "else the one given for its partner, else the one given for the whole visit). "
        "Only On track against Off track is a conflict: Constrained agrees with either, "
        "unless the strict setting is on.",
    },
    {
        "code": "R4",
        "label": "Narrative coherence",
        "points": 15,
        "threshold": 25,
        "params": {
            "placeholders": [
                "n a",
                "na",
                "none",
                "nil",
                "no comment",
                "no comments",
                "same as above",
                "see above",
                "test",
                "tbd",
                "not applicable",
            ],
            "check_copies": True,
            "copy_window_days": 365,
        },
        "description": "Each narrative has at least 25 words, is not a placeholder such as “n/a”, and is "
        "not word for word the narrative of another visit within a year. Points in "
        "proportion to the narratives that pass.",
    },
    {
        "code": "R5",
        "label": "Q3 quality",
        "points": 15,
        "threshold": 15,
        "params": {
            "placeholders": [
                "n a",
                "na",
                "none",
                "nil",
                "no comment",
                "no comments",
                "same as above",
                "see above",
                "test",
                "tbd",
                "not applicable",
            ]
        },
        "description": "Q3 (key observations and findings) is answered with at least 15 words, answer "
        "and summary together, and not with a placeholder. Points in proportion to the "
        "words, up to 15.",
    },
    {
        "code": "R6",
        "label": "Rating quality",
        "points": 0,
        "threshold": 2,
        "params": {
            "negative_cues": [
                "not implemented",
                "not started",
                "delayed",
                "delay",
                "suspended",
                "stopped",
                "halted",
                "did not take place",
                "not conducted",
                "postponed",
                "cancelled",
                "canceled",
                "shortage",
                "not delivered",
                "behind schedule",
                "low attendance",
                "no activities",
                "closed",
                "unsafe",
                "not functional",
            ],
            "positive_cues": [
                "implemented as planned",
                "as planned",
                "on track",
                "completed as planned",
                "good progress",
                "well implemented",
                "successfully",
                "fully functional",
                "no major issues",
            ],
            "negations": ["no", "not", "without", "never", "nor"],
            "negation_window": 3,
            "access_cues": [
                "could not",
                "no access",
                "not accessible",
                "postponed",
                "not monitored",
                "security",
                "closed",
            ],
            "check_not_monitored": False,
        },
        "description": "The narrative does not contradict the rating: an entity rated On track whose "
        "narrative names two or more problems (delayed, suspended...) and nothing good, "
        "or one rated Off track that names only good points. A word preceded by “no” or "
        "“not” within three words does not count. A flag only: no points unless an "
        "administrator gives it some.",
    },
]

SCORE = {
    "min_evaluated_points": 30,
    "band_high": 80,
    "band_medium": 50,
    "high_flag_count": 3,
    "urgency_red": 70,
    "urgency_amber": 40,
    "urgency_weights": {
        "off_track": 40,
        "constrained": 20,
        "quality_gap": 25,
        "unscored_reported": 10,
        "per_flag": 5,
        "flags_max": 15,
        "no_follow_up": 20,
        "ap_overdue": 12,
        "ap_high_overdue": 8,
        "ap_high_open": 5,
        "follow_up_max": 20,
        "report_late": 15,
    },
    "follow_up_days": 14,
    "report_late_days": 30,
    "question_patterns": {
        "q1": ["implemented as planned", "activities been implemented"],
        "q2": ["^q2", "^q 2", "activities monitored"],
        "q3": ["^q3", "^q 3"],
        "psea": ["psea", "sexual exploitation", "sexual abuse"],
    },
    "role_flag_answers": {"psea": ["yes", "constrained", "off_track"]},
}


def seed(apps, schema_editor):
    RuleSetting = apps.get_model("fmm", "RuleSetting")
    ScoreSetting = apps.get_model("fmm", "ScoreSetting")
    RuleSetVersion = apps.get_model("fmm", "RuleSetVersion")
    for row in RULES:
        values = {k: v for k, v in row.items() if k != "code"}
        RuleSetting.objects.get_or_create(code=row["code"], defaults=values)
    ScoreSetting.objects.get_or_create(pk=1, defaults=SCORE)
    if not RuleSetVersion.objects.exists():
        snapshot = {
            "rules": [{**row, "enabled": True} for row in RULES],
            "score": SCORE,
            "mappings": {},
        }
        RuleSetVersion.objects.create(
            number=1, snapshot=snapshot, note="Defaults", created_by_name="NeuroDB (default)"
        )


class Migration(migrations.Migration):
    dependencies = [("fmm", "0003_rules")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
