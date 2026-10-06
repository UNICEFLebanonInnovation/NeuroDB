"""Release 2, stage B: the quality rules become FMS Lebanon's rule set (``fmm.lebanon``, from its rules
file), and the AI checks get their instructions.

- Release 1's rules R1-R6 are replaced by FMS Lebanon's 32 rules: R1, R2, R3, R5, R6, R7, R8, R19, R20,
  R21, R23 and R32 switched on; R4, R9-R18, R22, R24-R31 seeded switched off, their lists empty. No staff
  e-mail address is part of it: R19's offices are seeded as field office staff lists with no address
  (the rule skips while a list is empty), R22's list is empty.
- The score settings take FMS Lebanon's six categories and weights (completeness 30, evidence 20,
  alignment 20, coherence 15, Q3 quality 10, actionability 5).
- A new prompt version copies the published one and adds the instructions of the six AI checks from
  FMS Lebanon's prompt file; it is published, the one it copies retired (a rollback brings it back).
- The change is recorded as a new rules version, so every visit is rescored by the next refresh.

Running it again changes nothing; the reverse leaves the rows."""

import hashlib
import json
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

from neurodb.fmm import lebanon

SAFETY_VERSION = 3  # fmm.ai.prompts.SAFETY_VERSION on the day this was written
SCHEMA_VERSION = 2  # fmm.ai.insights.SCHEMA_VERSION on the day this was written
BY = "NeuroDB (default)"
NOTE = "FMS Lebanon rules (Release 2): the AI checks' instructions from FMS's Lebanon prompt file"
RULES_NOTE = "FMS Lebanon rule set (Release 2): 32 rules, six score categories"
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
RULE_FIELDS = (
    "label",
    "description",
    "type",
    "category",
    "group",
    "hact_spec",
    "enabled",
    "deduction",
    "flag_template",
    "params",
)
SCORE_FIELDS = (
    "categories",
    "band_high",
    "band_medium",
    "high_flag_count",
    "urgency_red",
    "urgency_amber",
    "urgency_weights",
    "recency_days",
    "scored_statuses",
    "follow_up_days",
    "report_late_days",
    "question_patterns",
    "role_flag_answers",
    "ai_checks",
    "ai_model",
    "ai_max_output_tokens",
    "ai_temperature",
    "ai_text_chars",
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


def _number(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _order(code: str):
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 0, code)


def seed_rules(apps, schema_editor):
    RuleSetting = apps.get_model("fmm", "RuleSetting")
    ScoreSetting = apps.get_model("fmm", "ScoreSetting")
    RuleSetVersion = apps.get_model("fmm", "RuleSetVersion")
    FieldOfficeStaff = apps.get_model("fmm", "FieldOfficeStaff")
    FieldMapping = apps.get_model("fmm", "FieldMapping")
    if RuleSetVersion.objects.filter(note=RULES_NOTE).exists():
        return
    wanted = {rule["id"]: lebanon.model_values(rule) for rule in lebanon.RULE_SET["rules"]}
    RuleSetting.objects.exclude(code__in=list(wanted)).delete()  # Release 1's R1-R6 that FMS has not
    for code, values in wanted.items():
        RuleSetting.objects.update_or_create(
            code=code, defaults={**values, "deduction": Decimal(str(values["deduction"]))}
        )
    setting, _created = ScoreSetting.objects.get_or_create(pk=1)
    setting.categories = lebanon.categories()
    thresholds = lebanon.RULE_SET.get("quality_thresholds") or {}
    setting.band_high = thresholds.get("high", setting.band_high)
    setting.band_medium = thresholds.get("medium", setting.band_medium)
    setting.save()
    for office in lebanon.STAFF_OFFICES:
        FieldOfficeStaff.objects.get_or_create(office=office, defaults={"emails": ""})
    rows = [
        {"code": rule.code, **{name: _number(getattr(rule, name)) for name in RULE_FIELDS}}
        for rule in sorted(RuleSetting.objects.all(), key=lambda r: _order(r.code))
    ]
    mappings = {
        f"{m.dataset}.{m.field}": m.override_key.strip()
        for m in FieldMapping.objects.exclude(override_key="").order_by("dataset", "field")
        if m.override_key.strip()
    }
    snapshot = {
        "rules": rows,
        "score": {name: _number(getattr(setting, name)) for name in SCORE_FIELDS},
        "mappings": mappings,
    }
    number = (RuleSetVersion.objects.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    RuleSetVersion.objects.create(number=number, snapshot=snapshot, note=RULES_NOTE, created_by_name=BY)


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
    content["rule_prompts"] = dict(lebanon.RULE_PROMPTS)
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
        ("fmm", "0012_fms_rule_model"),
    ]

    operations = [
        migrations.RunPython(seed_rules, migrations.RunPython.noop),
        migrations.RunPython(seed_prompts, migrations.RunPython.noop),
    ]
