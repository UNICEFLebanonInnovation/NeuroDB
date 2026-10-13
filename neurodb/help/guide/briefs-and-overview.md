# Overview, management brief and daily review

## The country overview

The [overview](/) is the signed-in home page: one page for the whole intervention, read from the
synced eTools tables and the ActivityInfo history.

| Row | What it shows |
|---|---|
| Impact on children | Children reached (eTools partner reports and ActivityInfo, shown apart), by governorate, achievement against target by section, monthly reach, coverage of the estimated children per governorate. |
| Value for money | Funds disbursed of reserved, cost per child reached by section, spending against delivery, funding by donor, partnerships that need a decision. |
| Delivery and assurance | Indicator status by section, assurance counts (field monitoring, TPM visits, action points, HACT risk), findings by rating, what needs attention. |
| Progress | TPM visits planned, completed and with the report overdue per month; action points due, closed and past due per month. |

- **Filters**: year (the year menu), section and governorate (click a tile). You land on your own
  section, with every section one click away.
- **Which programme documents**: every one running in the selected year, whatever its status now,
  except drafts and cancelled ones.
- **Children reached**: the same children are often reported in both eTools and ActivityInfo, so the
  headline is the **larger** of the two, never their sum; both are shown beside it.
- **One indicator per programme document**: the indicators of one programme document count the same
  children, so they are never added. Each programme document counts once, with its *unique
  beneficiaries indicator* (chosen on the programme document page by an Administrator or a Section
  editor of its section), else its *largest single indicator* (its largest children indicator that
  year). The eTools figure is the sum over the programme documents: partners may reach the same
  children. A value too large to be true is never counted; the page lists it so it can be checked.
  When the chosen indicator cannot count (its value is too large to be true, or it has become a
  percentage), the programme document page says so and its largest single indicator stands in.
  Reports submitted but not yet accepted count, and the tile says how many children come from them
  (a report sent back later lowers the figure).
- **Achievement against target**: the median share of the target reached by the children indicators of
  each section (each capped at 100%); targets of different indicators are never added.
- **Cost per child**: what the programme documents with a children result disbursed to date, over the
  children they reached in the year (each one's main indicator). Compare sections, not absolute values.
- **The page opens at once**: the filters and the strip at the bottom come first, with a grey box
  where each block of figures will go. The tiles, the charts, the daily review, What's new and the
  For you card then appear one after the other as they are ready, and you can use the page
  meanwhile. A block that takes long says "Still working on this part"; a block that could not load
  says so and has a **Try again** button, and the rest of the page keeps working. The daily review's
  other findings load when you open its *Show … more findings* line; they are always those of the
  card you see, even when a newer review has run since the page opened. If the review on the card has
  itself been run again since (an administrator's *Run the daily review now* replaces that day's
  review), the line asks you to reload the page instead. If you print before every block has
  appeared, a block still loading prints a short note instead: wait until the page is complete.
- Figures are kept 2 minutes, so opening the same filter again soon is faster; the end of any sync
  shows at once. The strip at the bottom says when each source last changed.

## The management brief

The [management brief](/brief/) is the overview read for decisions, for senior management. It compares
the selected year with the year before, in eight tabs:

- **Brief**: five tiles with a comparison (children reached against the same months of last year,
  achievement against the elapsed period, disbursed of reserved, cost per child, indicators on track
  against 30 days ago), *Things to decide* (up to five decisions from the daily review, marked
  *AI-suggested*) and *Sections at a glance*. *Copy summary* copies the headline figures for minutes.
- **Ahead or behind**, **How sure** (confidence per section: High, Medium or Low, with what keeps it
  from High), **Who and where**, **Partners**, **Money**, **Action** and **Reference** (where the
  figures come from).
- Values that cannot be right (above the child population of Lebanon, or more than 20 times their
  target) are left out, and the section's confidence is Low.
- **The page opens at once**: the filters, the *Figures as of* line, the tabs and the *Reference*
  block come first, with a grey box where the figures will go. The *Brief* tab's tiles, decisions and
  sections at a glance fill in first, then the six other tabs together, with their charts; you can
  use the page meanwhile. A tab whose box has not filled in yet takes you there as soon as it has. A
  box that takes long says "Still working on this part"; a box that could not load says so and has a
  **Try again** button, and the rest of the page keeps working. Opened again within 2 minutes with
  the same filter, the page comes complete at once.
- **Print** waits for every box: the button reads "Preparing the page…" until every tab and its
  charts are drawn, then opens the print window (after a minute it prints what it has). Ctrl+P
  before the page is complete prints a short note in place of each box still loading.
- Figures are kept 2 minutes; the end of a sync, a new daily review, a section plan or a finding
  assignment entered in the administration, and a changed indicator flag show at once.

## The daily review

Every morning at 06:00 the daily review runs fourteen fixed checks over the synced data and keeps the
result as a dated review with its findings. The overview's *Daily review* card shows the latest one.

- Checks include indicators newly off track, indicators not reported, progress reports overdue,
  spending ahead of achievement, under-disbursed programme documents, programme documents ending soon
  below target, late TPM reports, overdue high-priority action points, field monitoring findings off
  track, and the data problems (failed syncs, data quality, stale sources), summed up in one line that
  links to Data health.
- Each finding is *new*, *still open* or *resolved* compared with the day before.
- With the AI set up, ChatGPT writes four to six plain sentences from the day's findings only, and
  chooses up to five decisions for the management brief; a decision is kept only when every finding it
  cites exists and every number it writes is in those findings. Without the AI, a template writes the
  summary.
- Findings are prompts to look, never verdicts on partners or staff.

## Year-end forecasts

[Year-end forecasts](/insights/forecasts/) estimates, for each ActivityInfo master indicator that adds
up month by month, where it will likely stand in December, with a range: *On course*, *Likely to fall
short* or *Uncertain*. Only months that ended more than 30 days ago count. Forecasts appear on the
dashboards and in Ask NeuroDB only once the method has proved itself on past years; they are always
estimates with a range. They are worked out every Monday at 07:15.
