# Ice box

Ideas discussed and parked: not planned, kept so they are not lost. Each says what it is, how it
would work and what was decided so far.

## Save to the knowledge base from Ask NeuroDB

*Parked on 1 October 2026. Estimate: about a day.*

Give Ask NeuroDB the content directly in the chat and let the assistant store it, instead of the
separate **Add a document or text** form.

**How it would work**

- In the chat, an editor pastes a text or attaches a file (a paperclip next to the question box) and
  says, e.g., "Save these minutes" or "Remember that the transport allowance went up to $15 in May".
- The assistant proposes a card: title, source, section, year and a short preview of what it
  understood. The person clicks **Save** or **Cancel**; nothing is stored without that click.
- Once saved, it goes through the existing pipeline (`index_knowledge`: text, passages, full-text
  index, links, AI summary); the assistant answers with the document's link, and the content can be
  asked about in the same conversation.
- Short notes ("remember that…") are stored as small knowledge documents, searchable and linked
  like the others.

**Decisions to keep**

- **A person confirms every save.** The assistant reads text written by others (documents, quoted
  passages); without the confirmation, instructions hidden in that text could make it save or
  change content. The model only proposes; the click saves.
- **Only administrators and section editors** get the save action (the same rule as the Add form,
  `neurodb.knowledge.access.can_add`); viewers keep a read-only assistant.
- **Attachments go to NeuroDB first**: the file is stored and its text extracted on the server
  (`neurodb.knowledge.text`); only the text is sent to OpenAI, as today.
- **Pasted content needs its own limit**: questions are capped at 1,000 characters
  (`MAX_QUESTION_CHARS`); a pasted text would allow about 100,000, and anything longer is attached
  as a file.
- The **Add a document or text** form stays for people who prefer it.
- **NeuroDB Watch's memory is not a save.** The background assistant behind the *For you* page
  (October 2026, `neurodb/watch`) keeps its own memory of what it follows: each point with its
  evidence and dated story, what each person was told, and what its AI looked up on a critical point.
  That memory is not the knowledge base: nothing the watch or its AI concludes is saved as a
  document, into the knowledge hub, or into what Ask NeuroDB searches, so the rule above (a person
  confirms every save) still holds. Should the watch ever propose a save, it goes through the same
  confirmation.
- The personal-data warning of the Add form is shown next to the attach button.

**Work involved**: a `save_to_knowledge` proposal tool (editors only) returning a draft; a
confirmation card in `ask.js` posting to a new endpoint that creates the `Document` and starts
`index_knowledge`; file attachment in the chat form; tests; a paragraph in the knowledge base
section of `OPERATIONS.md`.

## Accountability to affected populations (AAP) page

*Parked on 20 October 2026, until the data is confirmed. Estimate: half a day for the check page,
about 1.5 weeks for the page.*

A staff page on accountability to affected populations, built from what NeuroDB already holds. It is
not a CFM (complaints and feedback mechanism) dashboard: NeuroDB holds no complaints register, no
hotline or helpline log, no U-Report results, no case numbers and no closing times.

**What NeuroDB holds today**

- **Field monitoring checklist answers** (eTools, refreshed nightly): if the Lebanon checklist asks
  whether a feedback and complaints mechanism is in place, or about satisfaction, visits can be
  counted by governorate, section, partner and quarter. The strongest source, but **not confirmed**:
  the real Lebanon checklist questions have not been seen.
- **Checklist categories per visit**: whether a community engagement or accountability category was
  covered on a visit (that AAP was checked, not what was found). Not confirmed either.
- **ActivityInfo archive**: indicator 7.2.1.b, UNICEF-supported AAP initiatives in place, by
  governorate (history only), and the participation indicators 7.2.1.a and 7.2.2.c.
- **Emergency snapshot reports** (knowledge base): people with safe and accessible channels to report
  sexual exploitation and abuse, as a share of the target, and an SBC/AAP engagement figure.
- **PSEA assessments of partners** (eTools): which partners with active programme documents have a
  final assessment and how recent it is, and the rating per standard, "Reporting" included.
- **Document review findings** tagged "Accountability to affected populations" or "Feedback and
  complaints", once documents have been reviewed.
- Action points, PD indicators and report narratives by keyword only: many false positives, a last
  resort.

**How it would work**

1. **A check page first** (administrators only, counts only, no text): the checklist questions and
   categories Lebanon really uses, any PD indicator about feedback, the PSEA answer subjects and the
   snapshot figures. It runs from the admin, as a button, never from a command line. Every part of
   the page below depends on what it shows.
2. **Then the Accountability (AAP) page** for staff, in four sections: *On the ground* (the feedback
   mechanism as seen on monitoring visits), *Results* (AAP initiatives and access to reporting
   channels), *Partner readiness* (PSEA assessments) and *Evidence* (document review counts).
3. **A real CFM dashboard needs data to come in**: a monthly CFM figures feed (feedback received by
   channel and type, share closed, days to close, satisfaction), a few standard AAP questions on the
   Lebanon field monitoring checklist, or aggregate hotline or U-Report results.

**Decisions to keep**

- **Counts only**, never the text of a complaint, a comment or a PSEA answer; groups of fewer than 5
  are hidden; no PSEA "flagged" figures; never an allegation of sexual exploitation or abuse.
- **Staff only**: not on the donor page, not in exports with names, and nothing of it is sent to the
  AI beyond the counts (Ask NeuroDB already reads only the standard, rating, partner and date of a
  PSEA answer, `catalogue.SAFEGUARDING_KEYS`).

**To restart**: confirm which checklist questions and categories Lebanon uses (or whether a CFM feed
can be shared), then build the check page and decide the page's sections from what it shows.
