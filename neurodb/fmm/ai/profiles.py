"""Prompt versions: draft, publish, roll back, delete a draft; and what each call sends.

A profile ("lebanon") has numbered versions. A version is a **draft** while an administrator edits and
tries it, then **published** (the one the page, the nightly briefs and the chat use) and later
**retired** when another is published. Published and retired versions never change
(``PromptVersion.save`` refuses it): a rollback copies an old version's content into a new version and
publishes that, so the history stays linear ("v9 = rolled back to v6"). Only drafts can be deleted, with
their test runs.

What a call sends is put together here, so that the admin's Preview shows exactly what is sent:

- the brief: :func:`compose` (``version, "insights"``) is its whole ``instructions``;
- the chat: :func:`chat_instructions`, i.e. ``agent.effective_instructions`` of the chat's options: the
  composed chat prompt, the date, then :func:`scope_line`. Ask NeuroDB's own prompt is not sent.
"""

from __future__ import annotations

from typing import Any, Literal

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.formats import date_format

from ..models import Insight, ModelCapability, PromptProfile, PromptVersion
from . import prompts

DEFAULT_PROFILE = "lebanon"
LOW_TOKENS = 2500  # below this, effort medium or higher may use the whole limit on reasoning
EFFORTS_THAT_REASON_LONG = ("medium", "high", "xhigh", "max")


def user_name(user) -> str:
    if user is None or not getattr(user, "pk", None):
        return "NeuroDB"
    full = (user.get_full_name() or "").strip() if hasattr(user, "get_full_name") else ""
    return (full or user.get_username())[:150]


def _user(user):
    return user if getattr(user, "pk", None) else None


# ------------------------------------------------------------------------------------------ reading
def published(key: str = DEFAULT_PROFILE) -> PromptVersion | None:
    """The published version of profile ``key``; None when there is none."""
    profile = PromptProfile.objects.select_related("published").filter(key=key).first()
    version = profile.published if profile else None
    if version is None or version.status != PromptVersion.Status.PUBLISHED:
        return (
            PromptVersion.objects.select_related("profile")
            .filter(profile__key=key, status=PromptVersion.Status.PUBLISHED)
            .first()
        )
    version.profile = profile  # the same row: its label is shown with the version
    return version


def model_of(version: PromptVersion) -> str:
    """The model a version calls: its own, else ``FMM_MODEL`` (which defaults to the assistant's)."""
    return (version.model or "").strip() or settings.FMM_MODEL


def compose(version: PromptVersion, kind: Literal["insights", "chat"]) -> str:
    """The whole prompt of ``kind`` (``prompts.compose``)."""
    return prompts.compose(version, kind)


def scope_line(scope) -> str:
    """The chat's last line: which visits the question is about ("The visits in scope: …")."""
    return f"The visits in scope: {scope.label()}."


def chat_instructions(version: PromptVersion, scope) -> str:
    """Exactly the ``instructions`` the chat sends for ``scope``: the composed chat prompt, the date and
    the scope line (``agent.effective_instructions`` of the chat's options)."""
    from neurodb.assistant import agent

    options = agent.RunOptions(base_prompt=compose(version, "chat"), instructions=scope_line(scope))
    return agent.effective_instructions(options)


def test_runs(version: PromptVersion) -> int:
    """The test-run briefs written with ``version`` (deleted with a draft)."""
    return Insight.objects.filter(version=version, trigger=Insight.Trigger.TEST).count()


# ------------------------------------------------------------------------------------------ checks
def text_problems(text: str, names_: frozenset[str] | None = None) -> list[str]:
    """Why an editable prompt text may not be saved: it holds an e-mail address, a link or the name of a
    person NeuroDB knows (prompts name roles and sections, never people)."""
    from neurodb.watch import people, redact

    from .. import privacy

    text = str(text or "")
    found = []
    if people.EMAIL.search(text):
        found.append("It contains an e-mail address; prompts never name people.")
    if redact.LINK.search(text):
        found.append("It contains a link; links are never sent to the AI.")
    known = privacy.names() if names_ is None else names_
    if people.mentions(people.EMAIL.sub(" ", text), known):
        found.append("It contains the name of a person NeuroDB knows; write roles or sections instead.")
    return found


def refused(version: PromptVersion, parameter: str) -> ModelCapability | None:
    """The record that the version's model refused ``parameter`` at its effort, while it still holds
    (within ``FMM_SAMPLING_RECHECK_DAYS``); None otherwise."""
    from . import sampling

    return sampling.known_rejection(model_of(version), version.effort, parameter)


def warnings(version: PromptVersion) -> list[str]:
    """What an administrator should know about a saved version, without blocking the save."""
    out = []
    if version.effort in EFFORTS_THAT_REASON_LONG and (
        version.max_output_tokens < LOW_TOKENS or version.chat_max_output_tokens < LOW_TOKENS
    ):
        out.append(
            f"Effort {version.effort} with under {LOW_TOKENS:,} output tokens: reasoning uses part of the "
            "limit and the brief may be cut off."
        )
    if version.temperature is not None and version.top_p is not None:
        out.append("Temperature and top-p are both set: OpenAI advises changing one of them only.")
    for parameter, label in (("temperature", "Temperature"), ("top_p", "Top-p")):
        if getattr(version, parameter) is None:
            continue
        row = refused(version, parameter)
        if row is not None:
            day = date_format(timezone.localtime(row.checked_at), "j M Y")
            out.append(
                f"{label} was refused by {row.model} at effort {row.effort} on {day}: it will not be applied."
            )
    return out


# ------------------------------------------------------------------------------------------ changes
def next_number(profile: PromptProfile) -> int:
    return (PromptVersion.objects.filter(profile=profile).aggregate(n=Max("number"))["n"] or 0) + 1


def draft_from(version: PromptVersion, user, note: str, **changes: Any) -> PromptVersion:
    """A new draft with ``version``'s content and ``changes`` (content fields only), numbered after the
    profile's last version and based on ``version``."""
    unknown = sorted(set(changes) - set(PromptVersion.CONTENT_FIELDS))
    if unknown:
        raise ValueError(f"not prompt content: {', '.join(unknown)}")
    with transaction.atomic():
        profile = PromptProfile.objects.select_for_update().get(pk=version.profile_id)
        content = {name: getattr(version, name) for name in PromptVersion.CONTENT_FIELDS}
        content.update(changes)
        draft = PromptVersion(
            profile=profile,
            number=next_number(profile),
            status=PromptVersion.Status.DRAFT,
            note=(note or "").strip()[:300],
            based_on=version,
            created_by=_user(user),
            created_by_name=user_name(user),
            **content,
        )
        draft.save()
    return draft


def publish(version: PromptVersion, user) -> None:
    """Publish a draft: in one transaction the published version is retired, ``version`` is published
    (by whom, when) and becomes the profile's. Raises ValueError for a version that is not a draft."""
    with transaction.atomic():
        profile = PromptProfile.objects.select_for_update().get(pk=version.profile_id)
        version = PromptVersion.objects.select_for_update().get(pk=version.pk)
        if version.status != PromptVersion.Status.DRAFT:
            raise ValueError(f"Prompt v{version.number} is {version.status}: only a draft can be published.")
        for current in PromptVersion.objects.select_for_update().filter(
            profile=profile, status=PromptVersion.Status.PUBLISHED
        ):
            current.status = PromptVersion.Status.RETIRED
            current.save(update_fields=["status"])
        version.status = PromptVersion.Status.PUBLISHED
        version.published_by = _user(user)
        version.published_by_name = user_name(user)
        version.published_at = timezone.now()
        version.save(update_fields=["status", "published_by", "published_by_name", "published_at"])
        profile.published = version
        profile.save(update_fields=["published"])


def roll_back(to: PromptVersion, user, note: str = "") -> PromptVersion:
    """Publish a new version with ``to``'s content (``restored_from=to``): the old row is never published
    again. Raises ValueError for a draft (a draft is published, not rolled back to)."""
    if to.status == PromptVersion.Status.DRAFT:
        raise ValueError(f"Prompt v{to.number} is a draft: publish it instead.")
    text = f"Rolled back to v{to.number}" + (f": {note.strip()}" if (note or "").strip() else "")
    with transaction.atomic():
        copy = draft_from(to, user, text)
        copy.restored_from = to
        copy.save(update_fields=["restored_from"])
        publish(copy, user)
    copy.refresh_from_db()
    return copy


def delete_draft(version: PromptVersion, user=None) -> int:
    """Delete a draft and, first, its test-run briefs (the only briefs a draft has); how many test runs
    were deleted. Raises ValueError for a published or retired version, which is never deleted."""
    with transaction.atomic():
        version = PromptVersion.objects.select_for_update().get(pk=version.pk)
        if version.status != PromptVersion.Status.DRAFT:
            raise ValueError(f"Prompt v{version.number} is {version.status}: only drafts can be deleted.")
        deleted, _ = Insight.objects.filter(version=version, trigger=Insight.Trigger.TEST).delete()
        version.delete()
    return deleted
