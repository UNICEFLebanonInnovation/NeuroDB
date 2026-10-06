# Glossary

## Action point
A follow-up action recorded in eTools after an audit, a spot check, a TPM visit, a trip or a field
monitoring visit, or kept in NeuroDB only (a **NeuroDB action point**). **Open** when its status is
open; **overdue** when it is also past its due date. See [Action points](/help/action-points/).

## Average quality
The mean quality score of the scored visits of the filter, rounded half up to one decimal.

## Band
A visit's quality band: **High** from 80, **Medium** from 50, **Low** below 50, or **Pending** when it
is not scored. Its urgency band: **red** from 70, **amber** from 40.

## Category (score category)
A part of the quality score with a weight: Completeness 30, Evidence 20, Alignment 20, Coherence 15,
Q3 quality 10, Actionability 5. A category's deductions count at most its weight.

## CP output
An output of the country programme, as eTools names it on a monitoring finding or a programme document.

## Datamart
The eTools Datamart: the copy of eTools that NeuroDB reads every night.

## Deduction
The points a quality rule takes off its category when it fires.

## Entity
What a monitoring visit looked at: a partner, a programme document (PD/SSFA) or a CP output. Each
entity is one finding row of the visit.

## Flag
A quality rule that fired on a visit. 3 flags or more make a high-flag visit.

## HACT
The Harmonized Approach to Cash Transfers: the assurance of partners (assessments, audits, spot checks,
programmatic visits). **HACT Q1** is the checklist question "Have the activities been implemented as
planned…"; Q2 lists the activities monitored, Q3 the key observations.

## Not monitored
eTools' rating for a visit that was planned but not conducted. A count apart, never part of a share of
ratings.

## PD
A programme document (also PD/SSFA): the agreement with a partner in eTools, with its indicators,
locations, funds and sections.

## Pending
A visit whose eTools status is not one of the scored statuses (report finalization, completed): no
score, no urgency.

## Provisional
A scored visit whose AI checks are not all done: its score so far is shown on its page, but it counts as
not scored until the checks are done.

## PSEA
Protection from sexual exploitation and abuse. A visit is PSEA-flagged when a PSEA answer reads Yes,
Constrained or Off track.

## Quality score
100 less the deductions of the quality rules that fired, each category at most its weight. See
[The quality score](/help/monitoring-insights/#the-quality-score).

## Rating
On track, Constrained, Off track or Not monitored. A visit's rating is its worst rated entity.

## Rules version
A saved state of the quality rules and score settings. Every change makes a new version, and each visit
keeps the version it was scored with.

## Scored statuses
The eTools statuses whose visits get a quality score: report finalization and completed.

## Section
A programme section (Education, Child Protection...). Your own section is what several pages show
first.

## TPM
Third-party monitoring: visits made by a monitoring company on UNICEF's behalf.

## Urgency
0 to 100: 0.50 × (100 − quality) + 0.30 × recency + 0.20 × flags, for scored visits only. See
[Urgency](/help/monitoring-insights/#urgency).

## Visit
One eTools field monitoring activity: the finding rows that share an activity id.
