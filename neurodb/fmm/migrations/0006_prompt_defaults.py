"""Seed the prompt profile "lebanon" with version 1, published by "NeuroDB (default)".

The texts and parameters are written here as they were on the day this migration was written: the
brief's and the chat's editable instructions, model blank (FMM_MODEL), effort low, 4,000 output tokens
for the brief (reasoning included) and 6,000 per chat call, temperature 0.30 and top-p not set, narr 20,
comp 15, 5 briefs and 20 questions per person per day, 4 chat rounds and 120 seconds. Later changes are
made in the admin as new versions. Running it again changes nothing; the reverse leaves the rows."""

import hashlib
import json

from django.db import migrations
from django.utils import timezone

SAFETY_VERSION = 2  # fmm.ai.prompts.SAFETY_VERSION on the day this was written
SCHEMA_VERSION = 1  # fmm.ai.insights.SCHEMA_VERSION on the day this was written
BY = "NeuroDB (default)"

INSTRUCTIONS = """\
You write the field monitoring brief for UNICEF Lebanon section chiefs and field office leads, from the
monitoring visits described in the JSON. Be factual, short and constructive; never blame partners or staff.
coverage_quality: 2 to 4 sentences: how many visits and entities, how many entities were rated and how many
were not monitored, how many visits were reported, which governorates were not visited, the average quality
score and the weakest quality rule, compared with the previous period when it helps.
programmatic_findings: 3 to 6 sentences on what the visits found about programme delivery, from the narratives.
Prefer findings seen in more than one visit. Mention off-track and constrained ratings first.
operational_challenges: 2 to 5 sentences on what got in the way of monitoring or delivery: entities not
monitored, late reports, missing follow-up, access, delays, supplies.
recommendations: 2 to 5 sentences, each tied to a finding, saying which section or office should do what.
priority_actions: 3 to 6 actions, most urgent first, each with priority, section, action, owner role and timeframe."""

CHAT_INSTRUCTIONS = """\
You answer questions from UNICEF Lebanon staff about the field monitoring visits in their current filter. Use
fm_summary for counts and comparisons, fm_visits to list visits, fm_visit to read one visit, and fm_search to
find words in visit notes and answers. Start with the look-up that fits the question; there are no other tools.
Keep answers short: a few sentences or a short list."""

CONTENT = {
    "insights_enabled": True,
    "instructions": INSTRUCTIONS,
    "max_output_tokens": 4000,
    "narratives_sampled": 20,
    "comparison_visits": 15,
    "insights_per_user_per_day": 5,
    "chat_enabled": True,
    "chat_instructions": CHAT_INSTRUCTIONS,
    "chat_max_output_tokens": 6000,
    "chat_per_user_per_day": 20,
    "chat_max_rounds": 4,
    "chat_time_limit": 120,
    "model": "",
    "effort": "low",
    "temperature": "0.30",
    "top_p": None,
}


def _hash() -> str:
    # PromptVersion.compute_hash as it was written that day
    blob = json.dumps(
        {"content": CONTENT, "safety": SAFETY_VERSION, "schema": SCHEMA_VERSION}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def seed(apps, schema_editor):
    PromptProfile = apps.get_model("fmm", "PromptProfile")
    PromptVersion = apps.get_model("fmm", "PromptVersion")
    profile, _ = PromptProfile.objects.get_or_create(key="lebanon", defaults={"label": "Lebanon"})
    if PromptVersion.objects.filter(profile=profile).exists():
        return
    now = timezone.now()
    version = PromptVersion.objects.create(
        profile=profile,
        number=1,
        status="published",
        **CONTENT,
        note="Defaults",
        content_hash=_hash(),
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
        ("fmm", "0005_ai"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
