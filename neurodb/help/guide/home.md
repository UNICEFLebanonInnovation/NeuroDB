# Home

[Home](/) is the first page after you sign in. It answers three questions, in this order: what needs
you today, what changed since the previous working day, and where your section (or the country) stands
this year. Every figure of the year is on the [country overview](/country-overview/); what to decide is
in the [management brief](/brief/); the findings of the morning check are on the
[daily review](/daily-review/). Home opens at once and each block fills in as it is ready (see
[Pages that open at once](/help/overview/#pages-that-open-at-once)).

## What Home shows

Under the title: today's date and the sections the page shows (see
[Your section or every section](#your-section-or-every-section)).

- **Needs you today**, on the left:
  - the **For you** card: what NeuroDB Watch asks you to look at today ([For you](/help/watch/)). When
    an administrator has switched these checks off, the block says so. An account without a role, or
    without a section, is told why the card has little to say and whom to ask.
  - **Needs a decision** (Administrators, and members of the Management group): up to two decisions
    from the latest daily review, as the management brief lists them, each with its severity, section,
    status and due date. Who owns a decision is never shown on Home. *All decisions: Management brief*
    opens the brief.
  - **Daily review**, with the day and time it ran: how many findings of each severity (*Critical*,
    *Warning*, *To note*), the two that matter most, grouped by check ("and 4 more like it") and
    leaving out those already under *Needs a decision*, and up to two lines on what changed since the
    day before. *Open the daily review* opens its own page. A review more than two days old shows its
    date first; before the first review, the block says when it runs.
- **Since** the previous working day, on the right (*Since yesterday*, or *Since Friday* on a Monday):
  up to six notable changes NeuroDB noticed since 00:00 of that day, in your sections, each with its
  link; *Everything new: What's new* opens [What's new](/whats-new/). When your section's name matches
  none of the sections of the changes, the block says *every section* rather than showing the whole
  country under your section's name.
- **Your tasks**, under it (Section editors and Administrators; counts only, and left out when there is
  nothing to do): document review findings not reviewed yet (while the document review is on; your
  section's, the whole country's for Administrators), Makani wellbeing flags to follow up
  (Administrators and Education editors), and, while ActivityInfo is in use, the HPM reports of the
  year to comment on (Section editors).
- **Where your section stands** (or the country): the country overview's own five key figures for this
  year (children reached, programme documents, funds disbursed, cost per child, delivery status),
  exactly the figures the overview shows for the same sections, then a line *Also in* the year: field
  monitoring visits (with a section chosen, the visits to that section's partners), third-party visits
  done of those planned (left out when none is planned), and open action points with the high-priority
  ones overdue. Their links open the field monitoring and action points pages on the same sections; the
  Monitoring insights page shows the whole country, so with a section chosen its figures are not links.
  *All the figures: Country overview* opens the overview on the same sections. Early in the year (January to April), until the first partner reports of the year come in,
  the children reached tile says *Not yet* and links to last year.
- **Programmes and sources**, for the whole country: the country programme cycle (which year of it, and
  how many of its indicators are on track), the Makani, Dirasa and youth figures from Compiler, and
  ActivityInfo (while it is in use, its year and last import; once it is an archive, a link to the
  archive of its years). A source that is not set up is left out; administrators read a short line
  saying where it is set up.
- **Data freshness**, at the bottom: when each source last changed, with a link to Data health.

## Your section or every section

Home, the country overview and the daily review open the same way:

- People with a section in their account open on their section (*Your section, Education, by
  default*), with *every section* one click away.
- Members of the Management group, and Administrators without a section, open on the whole country
  (*Every section*), with *only* their section one click away when they have one.
- Every link Home draws to the country overview, the daily review and the management brief carries the
  sections Home shows, so the next page shows the same figures.
- *Programmes and sources* is always for the whole country, and says so when a section is chosen.

## ActivityInfo: in use or archive

Partners reported their monthly results in ActivityInfo in past years and now report progress in eTools;
the sidebar lists the years the archive holds. NeuroDB keeps every ActivityInfo year: its databases,
dashboards, maps, Neuro and HPM reports stay open, year by year. What changes is how prominent
ActivityInfo is:

| | In use | Archive |
|---|---|---|
| Sidebar | *Monthly results (ActivityInfo)*, at the bottom, open by default | *ActivityInfo archive* with its years, at the bottom, closed (it stays open if you open it) |
| Home | ActivityInfo's year, its results and its last import | a link to the archive of its years |
| Year-end forecasts | made every week | no longer made: the last ones stay, marked as no longer updated |
| The ActivityInfo import | a failed or late import is reported to administrators | nobody is told about it; Data health still lists its runs |
| Search | ActivityInfo results of every year | the same, marked *ActivityInfo archive* |

Either way, a year with ActivityInfo records keeps its ActivityInfo figures: in that year, children
reached is the larger of eTools and ActivityInfo (never their sum), with the months each source covers.
A year without ActivityInfo records shows eTools alone, never an ActivityInfo zero.

**Who decides.** By default it is automatic: ActivityInfo is in use while its import is switched on in
Scheduled jobs and this year has ActivityInfo records from the last three months. In January, last
year counts instead, since the import still brings in the December results until the 22nd. Otherwise
ActivityInfo is an archive, by itself. An administrator can set it to *In use* or *Archived* on the
administration page **ActivityInfo: in use or archive** (under Reporting setup), which also says why it
is what it is now. A change shows on every page within a minute. Nothing is deleted either way.

**When ActivityInfo comes back.** If partners report in ActivityInfo again, an administrator creates
the year's reporting year, ticks it current and adds its databases: from then on a failed import is
reported at once, and ActivityInfo is in use again as soon as the first recent records are imported.
