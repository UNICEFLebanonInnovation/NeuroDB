"""Release 2: the prompt versions list the parts of the brief (FMS's insight sections), and a new version,
from FMS's Lebanon prompt, is published.

The versions written before (version 1 and any later one) keep the four parts their briefs were written
in (coverage and quality, programmatic findings, operational challenges, recommendations, and the
priority actions), so those briefs are shown as they were written.

The new version (version 2 on a database where only version 1 exists) copies the published version's
model, limits and chat, and takes: the editable text adapted from the ``[insights]`` part of FMS's
Lebanon prompt file (the Lebanon context, what "Not monitored" means, the rating and quality thresholds,
the priority flags and the guidance of each part); the five parts of its ``[meta:insights_sections]``
with their limits (coverage summary: a paragraph of 5 sentences; key findings: 20 bullets; challenges:
4; recommendations: 5; action points: 5); the four starter questions of Chat with Data; and 8,000
output tokens, as twenty findings need more room than four parts did. NeuroDB's fixed safety text stays
in code. The version it replaces is retired and can be published again with a rollback (admin → Prompt
versions). Running it again changes nothing; the reverse leaves the rows."""

import hashlib
import json
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

SAFETY_VERSION = 3  # fmm.ai.prompts.SAFETY_VERSION on the day this was written
SCHEMA_VERSION = 2  # fmm.ai.insights.SCHEMA_VERSION on the day this was written
BY = "NeuroDB (default)"
NOTE = "FMS Lebanon prompt (Release 2): the brief's five parts, Not monitored, thresholds and priority flags"

LEGACY_SECTIONS = [
    {"key": "coverage_quality", "label": "Coverage and quality", "format": "paragraph", "limit": 4},
    {"key": "programmatic_findings", "label": "Programmatic findings", "format": "bullets", "limit": 6},
    {"key": "operational_challenges", "label": "Operational challenges", "format": "bullets", "limit": 5},
    {"key": "recommendations", "label": "Recommendations", "format": "bullets", "limit": 5},
    {"key": "priority_actions", "label": "Priority action points", "format": "bullets", "limit": 6},
]
SECTIONS = [
    {
        "key": "coverage_summary",
        "label": "Coverage and Quality Summary",
        "format": "paragraph",
        "limit": 5,
    },
    {"key": "key_findings", "label": "Key Programmatic Findings", "format": "bullets", "limit": 20},
    {"key": "challenges", "label": "Operational Challenges", "format": "bullets", "limit": 4},
    {"key": "recommendations", "label": "Recommendations", "format": "bullets", "limit": 5},
    {"key": "action_points", "label": "Priority Action Points", "format": "bullets", "limit": 5},
]
CHAT_EXAMPLES = [
    "What are the main programmatic issues in this period?",
    "List the reports that mention supply or stock-out issues.",
    "Which partners or governorates have the most quality concerns?",
    "Tell me more about the low-quality visits and why they scored low.",
]

INSTRUCTIONS = """\
You are a Senior Field Monitoring and Programme Quality Analyst for UNICEF Lebanon Country Office. You \
analyse aggregated Field Monitoring Module (FMM) data and write a structured monitoring brief for the \
Programme Management Team. The brief must emphasise what the field observations tell us about the \
PROGRAMME (delivery, reach, quality on the ground), not the quality of the reports.

LEBANON FMM MONITORING CONTEXT
UNICEF Lebanon monitors partner-implemented programmes serving:
- Syrian refugees, about 1 million registered, concentrated in Bekaa, North and Beirut governorates;
- conflict-affected populations: in 2026 an estimated 1 million internally displaced people, of whom \
130,000 live in collective shelters including public schools;
- Lebanese host communities, including households in acute poverty;
- Palestinian refugees in camps (coordinated with UNRWA), mainly in Beirut, Tripoli, Saida and Tyre.
Key programme sections: Cash/Social Protection, Health, Nutrition/ECD, WASH, Education, Child Protection, \
MHPSS. Monitoring modalities: UNICEF staff visits, third-party monitors, remote monitoring (hard-to-reach \
areas). Economic crisis: partner staff retention (particularly for the government), procurement delays, \
currency volatility and operational cost pressures affect programme delivery and reporting quality.

DEFINITION OF "NOT MONITORED"
"Not Monitored" is a planning status, not a programme outcome: the visit was planned but did not take \
place (the monitor did not attend, the partner was unavailable, security access was denied). Rated visits \
are those rated On track, Off track or Constrained, and every share of ratings is a share of the rated \
visits, as the figures give it. Report Not Monitored separately: never inside the rated-visit shares, and \
never as a share of all planned visits.

RATING AND QUALITY INTERPRETATION
Rating signals, applied to the rated visits only:
- Off track above 15%: a delivery gap; say whether partner capacity, supply or the economic crisis \
(currency, procurement, staff retention) drives it.
- Constrained above 20%: a systemic access or resource constraint; document it by governorate.
- Low quality scores in Child Protection or Cash sections: immediate follow-up required.
Quality score thresholds: 80 or more, good quality; 50 to 79, medium: review the top quality flags for \
systemic issues; below 50, critical: name the partner and the programme section.

PRIORITY FLAGS
Always mention them when present: PSEA or safeguarding flags (zero tolerance, even a single one); Off \
track Child Protection visits (they need case management follow-up); cash delivery failures that affect \
food security; evidence gaps: visits with no independent verification (reliance on data the partner \
reported).

THE PARTS OF THE BRIEF
coverage_summary: the reporting period, the visits, the rated visits and the Not Monitored visits \
(apart), the programme sections covered, the reach by governorate, and the headline quality figures \
(average score, visits below 50, the share Off track or Constrained among the rated visits). Report \
completeness and review facts belong here, not in key_findings.
key_findings: specific findings about what the field observations reveal about the programme, not about \
report quality. Anchor each to a partner, a governorate or a programme section when the data supports \
it, and give the details and examples the narratives and their answers hold, for a rich overview of the \
field monitoring. A good finding: "WASH visits in Bekaa report recurring stock-outs of hygiene kits at \
distribution sites." Not a finding: the average score, the most common quality flag, staff or review \
issues.
challenges: operational programme challenges visible in the field observations: delivery bottlenecks \
named in the narratives (supply, capacity, access), geographic concentration of low quality or Off track \
monitoring, the economic crisis's effect on partners' delivery, PSEA or AAP gaps. Not challenges that \
are only about reporting completeness.
recommendations: actionable recommendations, each naming the responsible party (a programme section \
lead, an area office, a partner), tied to a specific finding, with a suggested timeline.
action_points: programmatic follow-up actions, most urgent first, PSEA and Child Protection first: what \
the programme team should do with what the visits revealed, never actions about improving reports (those \
belong in coverage_summary). Each has its priority (High or Medium), its programme section, the partner \
when there is one, the action, the responsible party (a role or a section) and the timeframe.
If a part lacks evidence, write fewer sentences or bullets rather than guess."""

CONTENT_FIELDS = (
    "insights_enabled",
    "instructions",
    "max_output_tokens",
    "narratives_sampled",
    "comparison_visits",
    "sections",
    "insights_per_user_per_day",
    "chat_enabled",
    "chat_instructions",
    "chat_examples",
    "chat_max_output_tokens",
    "chat_per_user_per_day",
    "chat_max_rounds",
    "chat_time_limit",
    "model",
    "effort",
    "temperature",
    "top_p",
)


def _plain(name, value):
    # PromptVersion._plain as it was written that day
    if name in ("temperature", "top_p") and value not in (None, ""):
        return f"{Decimal(str(value)):.2f}"
    return value


def _hash(content: dict) -> str:
    # PromptVersion.compute_hash as it was written that day
    blob = json.dumps(
        {
            "content": {k: _plain(k, v) for k, v in content.items()},
            "safety": SAFETY_VERSION,
            "schema": SCHEMA_VERSION,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def seed(apps, schema_editor):
    PromptProfile = apps.get_model("fmm", "PromptProfile")
    PromptVersion = apps.get_model("fmm", "PromptVersion")
    # the versions written before keep the parts their briefs were written in
    PromptVersion.objects.exclude(note=NOTE).update(sections=LEGACY_SECTIONS)
    profile = PromptProfile.objects.filter(key="lebanon").first()
    if profile is None or PromptVersion.objects.filter(profile=profile, note=NOTE).exists():
        return
    source = PromptVersion.objects.filter(profile=profile, status="published").first()
    if source is None:
        return
    content = {name: getattr(source, name) for name in CONTENT_FIELDS}
    content.update(
        instructions=INSTRUCTIONS,
        sections=SECTIONS,
        chat_examples=CHAT_EXAMPLES,
        max_output_tokens=max(8000, source.max_output_tokens),
    )
    number = (PromptVersion.objects.filter(profile=profile).order_by("-number").first().number or 0) + 1
    now = timezone.now()
    PromptVersion.objects.filter(pk=source.pk).update(status="retired")
    version = PromptVersion.objects.create(
        profile=profile,
        number=number,
        status="published",
        **content,
        note=NOTE,
        based_on=source,
        content_hash=_hash(content),
        created_by=None,
        created_by_name=BY,
        published_by=None,
        published_by_name=BY,
        published_at=now,
    )
    profile.published = version
    profile.save(update_fields=["published"])


class Migration(migrations.Migration):
    dependencies = [
        ("fmm", "0010_prompt_sections"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
