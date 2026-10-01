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
- The personal-data warning of the Add form is shown next to the attach button.

**Work involved**: a `save_to_knowledge` proposal tool (editors only) returning a draft; a
confirmation card in `ask.js` posting to a new endpoint that creates the `Document` and starts
`index_knowledge`; file attachment in the chat form; tests; a paragraph in the knowledge base
section of `OPERATIONS.md`.
