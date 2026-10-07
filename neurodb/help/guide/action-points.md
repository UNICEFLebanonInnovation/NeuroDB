# Action points

The [action points page](/action-points/) (sidebar: Partnerships › Action points) lists the **eTools
action points** (from the Datamart: audits, spot checks, TPM, trips and field monitoring) and, under
them, the **NeuroDB action points**, kept in NeuroDB only. The visit links, the AI review, the PME
verification and the NeuroDB action points come from Monitoring insights and show while it is
switched on.

## Filters

Search (reference, description, action taken, partner, programme document, status, a person's name, a
visit's reference or an eTools activity id), status, *Raised from* (module), office, section, partner,
*Assigned to* (part of a name), *Changed in eTools* from / to, AI verdict, PME verification, visit link,
and *Show only* overdue, high priority or field monitoring. On your first visit you see your section's
action points; *Reset* comes back to it, and clearing the Section filter shows every one.

## Columns

Reference and description, partner and programme document, office and section, who it is assigned to
(shown to staff only, never sent to the AI), due date, status and completion date, and *Raised from*
with the **visit** of a field monitoring action point and its **link confidence**:

- **High**: matched by the visit's eTools activity id;
- **Medium**: matched by the visit's reference;
- **Unmatched**: no visit matches (blank for the other modules).

Then the **AI verdict** and the **PME verification**. The reference opens the details: every field, the
action taken, the AI's verdict and why, and the verifications.

An action point is **open** when its status is open or in progress (as FMS counts them), and
**overdue** when it is also past its due date (the same definition as the overview, Monitoring insights
and NeuroDB Watch). The *Open* status filter keeps the action points in progress too.

## Charts

Over the filter; each has *Download PNG*, and a bar opens its action points:

- by status;
- the due dates of the open ones (Overdue, due within 30 days, On track, No due date);
- raised and completed by month (the last 24 months; *raised* is the eTools record's creation date);
- whether the completed ones were completed on time (On time, Late 1–30 days, Late 31–90 days, Late
  more than 90 days, No dates), with the average days late of the late ones;
- the open ones by office and by section. NeuroDB never ranks staff by name, so offices and sections
  stand in place of FMS's "top assignees".

## AI adequacy review

For each **completed** action point (completed, closed or resolved) with an action taken written in
eTools, ChatGPT reads the description (the issue) and the action taken, cleaned of names, e-mail
addresses, phone numbers and links, and answers **Adequately addressed**, **Partially addressed**,
**Not addressed** or **Generic/vague**, with one sentence why. The review runs every morning at 06:10
on the action points not reviewed yet, most recently completed first, within its own daily budget;
administrators can also start it from the page (*Run AI review*). A verdict is shown only while the
description and the action taken are those it was made with: a changed text hides it until it is
reviewed again.

## PME verification

In the details: *Verified*, *Rejected* or *Pending*, with an optional note (never sent to the AI). Who
and when are recorded, and every decision is kept (*Earlier verifications*). Administrators and the
Section editors of the action point's section may verify.

## AI content summary

A button that reads the action points of the filter (at most 150, the most recent: their reference and
cleaned description, never who they are assigned to) and shows up to 5 dominant themes (name, how many
action points, one example) and one sentence on the overall pattern. A theme whose example was not
sent is left out. 5 summaries a day per person; a summary is not kept.

## NeuroDB action points

FMS's "local action points", never sent to eTools. Administrators and Section editors add one with
**New action point** (title, description, an optional visit, priority High / Medium / Low, due date, a
responsible role or section, or a person in NeuroDB), also from a visit's page. The person who added it,
an Administrator or a Section editor of the visit's sections marks it done, dropped or open again.

**NeuroDB makes one at each refresh** for a scored visit whose quality is Low (below 50) with at least
one of the AI's action point flags (R7, R8 or R32), unless one is already open on the visit: below 30 it
is *High* and due in 5 working days, else *Medium* and due in 10. Its title names the flag that took the
most points ("Follow up on R8 — FM/2026/23"), its description lists the visit's flags, and it is
assigned to the role "PME focal point".

## Follow-up

A visit counts as **followed up** by an eTools action point linked to it, by a NeuroDB action point added
by hand (not dropped), or by one NeuroDB made that someone marked done (an automatic one alone is only a
reminder). The Follow-up block of Monitoring insights and the For you check on field monitoring
follow-up read it at once.

## Exports

The toolbar's **CSV** copies the rows on screen; **CSV of the filter** downloads every action point of
the filter and **CSV of every action point** all of them. **Excel of the filter** and **Excel of every
action point** hold the same columns without *assigned to*, with the description and the action taken
cleaned of names, e-mail addresses, links and phone numbers. **PDF report** prints the filter, the key
figures, the charts and the action points by module (see [Exports](/help/exports/)).
