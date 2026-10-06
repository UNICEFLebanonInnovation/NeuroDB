"""Release 2, stage C: the action points page's AI gets its instructions. A new prompt version copies the
published one and adds, beside the AI checks' instructions, the review of a completed action point (FMS
Lebanon's ``ap_adequacy_review``) and the AI content summary of the page (``lebanon.AP_PROMPTS``); it is
published, the one it copies retired (a rollback brings it back). Running it again changes nothing; the
reverse leaves the rows."""

import hashlib
import json
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

from neurodb.fmm import lebanon

SAFETY_VERSION = 3  # fmm.ai.prompts.SAFETY_VERSION on the day this was written
SCHEMA_VERSION = 2  # fmm.ai.insights.SCHEMA_VERSION on the day this was written
BY = "NeuroDB (default)"
NOTE = "Action points (Release 2): the AI review of completed action points and the AI content summary"
CONTENT_FIELDS = (
    "insights_enabled",
    "instructions",
    "max_output_tokens",
    "narratives_sampled",
    "comparison_visits",
    "sections",
    "rule_prompts",
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
    # PromptVersion.compute_hash as it was written that day (no rule_prompts key when there are none)
    plain = {k: _plain(k, v) for k, v in content.items()}
    if not plain.get("rule_prompts"):
        plain.pop("rule_prompts", None)
    blob = json.dumps(
        {"content": plain, "safety": SAFETY_VERSION, "schema": SCHEMA_VERSION}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def seed_prompts(apps, schema_editor):
    PromptProfile = apps.get_model("fmm", "PromptProfile")
    PromptVersion = apps.get_model("fmm", "PromptVersion")
    profile = PromptProfile.objects.filter(key="lebanon").first()
    if profile is None or PromptVersion.objects.filter(profile=profile, note=NOTE).exists():
        return
    source = PromptVersion.objects.filter(profile=profile, status="published").first()
    if source is None:
        return
    content = {name: getattr(source, name) for name in CONTENT_FIELDS}
    prompts = dict(content.get("rule_prompts") or {})
    if all(prompts.get(key) for key in lebanon.AP_PROMPTS):
        return  # an administrator already wrote them
    for key, text in lebanon.AP_PROMPTS.items():
        prompts.setdefault(key, text)
    content["rule_prompts"] = prompts
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
    dependencies = [("fmm", "0014_action_points")]

    operations = [migrations.RunPython(seed_prompts, migrations.RunPython.noop)]
