"""The daily "what's new" note: the notable changes of the last 24 hours, one note for every section
they concern and one for everyone, written by the assistant when it is configured (else listed), shown
on the What's new page and emailed to the people who asked for it.

The model reads one line per change (names, codes and figures as NeuroDB holds them; never personal
data) and writes a few sentences; it is told to use nothing else.

While NeuroDB Watch's morning email is on (``neurodb.watch.delivery``), that email carries the note
with the person's For you points, and :func:`send` steps aside: one email a morning, not two.
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from .models import Change, Digest, DigestSubscription
from .news import sentence

logger = logging.getLogger(__name__)

MAX_LINES = 80  # changes the model reads per note
LISTED = 8  # changes listed under the note (and in the template note)
TEMPLATE = "template"
CARRIED = "carried by the morning note"  # why no What's new email went out: NeuroDB Watch's email has it
INSTRUCTIONS = """\
You write the daily "what's new" note of NeuroDB, UNICEF Lebanon's programme monitoring platform, \
for {audience}. You get the changes NeuroDB noticed in the last 24 hours, one line each. Write two \
to five short sentences of plain text: what matters most first (new programme documents and \
partners, funding, status changes, progress or figures that moved, new documents), grouping similar \
items with their count. Use only these lines, with names and figures exactly as written; never add \
causes, judgements or advice. No headings, no lists, no greeting."""


def _template(lines: list[str]) -> str:
    head = f"{len(lines)} notable change{'s' if len(lines) != 1 else ''} in the last 24 hours."
    return "\n".join([head, *(f"• {line}" for line in lines[:LISTED])])


def narrate(audience: str, lines: list[str]) -> tuple[str, str]:
    """(text, written by): the assistant's note, else the template list."""
    if not getattr(settings, "AI_ASSISTANT_ENABLED", False):
        return _template(lines), TEMPLATE
    try:
        from neurodb.assistant import agent, usage

        response = agent.client().responses.create(
            model=settings.AI_ASSISTANT_MODEL,
            instructions=INSTRUCTIONS.format(audience=audience),
            input="\n".join(lines[:MAX_LINES]),
            max_output_tokens=2000,
            store=False,
            reasoning={"effort": "low"},
        )
        usage.record(usage.DIGEST, settings.AI_ASSISTANT_MODEL, getattr(response, "usage", None))
        text = (getattr(response, "output_text", "") or "").strip()
        if not text:
            raise ValueError("the model returned no text")
        return text, settings.AI_ASSISTANT_MODEL
    except Exception:
        logger.exception("what's new note for %s: the assistant failed, listing the changes", audience)
        return _template(lines), TEMPLATE


def write(now: datetime.datetime | None = None) -> list[Digest]:
    """Today's notes from the last 24 hours (written again if run twice the same day)."""
    from neurodb.accounts.models import Section

    now = now or timezone.now()
    changes = list(
        Change.objects.filter(detected_at__gt=now - datetime.timedelta(hours=24), notable=True).order_by(
            "detected_at", "id"
        )
    )
    if not changes:
        return []
    by_section: dict[int, list[Change]] = defaultdict(list)
    for change in changes:
        for section_id in change.sections:
            by_section[section_id].append(change)
    names = dict(Section.objects.filter(pk__in=by_section).values_list("pk", "name"))
    day = timezone.localdate(now)
    notes = []
    for section_id, items in [(None, changes), *sorted(by_section.items())]:
        if section_id is not None and section_id not in names:
            continue
        audience = f"the {names[section_id]} section" if section_id else "all sections"
        lines = [sentence(c) for c in items]
        text, written_by = narrate(audience, lines)
        digest, _ = Digest.objects.update_or_create(
            date=day,
            section_id=section_id,
            defaults={
                "section_name": names.get(section_id, ""),
                "text": text,
                "written_by": written_by[:100],
                "changes": len(items),
            },
        )
        notes.append(digest)
    return notes


def recipients(digest: Digest, has_own_note: set[int]) -> list[str]:
    """Who asked for the email: the people of the note's section; the note for everyone goes to those
    without a section or whose section has no note today. Donor accounts never get it."""
    subs = DigestSubscription.objects.filter(
        email=True, user__is_active=True, user__donor_account__isnull=True
    ).exclude(user__email="")
    if digest.section_id:
        subs = subs.filter(user__section_id=digest.section_id)
    else:
        subs = subs.exclude(user__section_id__in=has_own_note)
    return sorted(set(subs.values_list("user__email", flat=True)))


def carried_by_morning_email() -> bool:
    """NeuroDB Watch's morning email is on and scheduled: it carries today's note to the people who
    asked for the email, so the What's new email steps aside."""
    from neurodb.watch import delivery

    return delivery.carries_whats_new()


def send(notes: list[Digest]) -> int:
    """Email the notes to the people who asked; the number of emails sent, 0 while NeuroDB Watch's
    morning email carries the note (:data:`CARRIED`)."""
    if not getattr(settings, "DIGEST_EMAIL_ENABLED", False) or not notes:
        return 0
    if carried_by_morning_email():
        logger.info("what's new note: not emailed, %s", CARRIED)
        return 0
    has_own_note = {d.section_id for d in notes if d.section_id}
    link = f"{settings.SITE_URL}{reverse('graph:whats_new')}" if settings.SITE_URL else ""
    sent = 0
    for digest in notes:
        audience = digest.section_name or "all sections"
        body = digest.text + (f"\n\nEverything that changed: {link}" if link else "")
        body += (
            "\n\nYou get this because you asked for it on NeuroDB's What's new page, where you can stop it."
        )
        count = 0
        for address in recipients(digest, has_own_note):
            try:
                count += send_mail(
                    f"NeuroDB — what's new for {audience}, {digest.date:%d %b %Y}",
                    body,
                    settings.DEFAULT_FROM_EMAIL,
                    [address],
                )
            except Exception:
                logger.exception("could not email the what's new note to one recipient")
        digest.emailed_to = count
        digest.save(update_fields=["emailed_to"])
        sent += count
    return sent
