# KM Database

## What the KM Database is

The [KM Database](/km/) holds findings from research, evaluations and assessments, with the documents
they come from. An administrator uploads it as one Excel workbook; each finding is shown as the workbook
gives it.

- **Kept apart.** The KM Database is not linked to partners, programme documents, indicators or any other
  NeuroDB data. Ask NeuroDB, the Documents page, the library, NeuroDB Watch and the search at the top of
  every page do not read it. It has its own assistant, on its **Ask the KM Database** tab, which reads
  the KM Database only (see below).
- **Where it is.** In the full menu, under Resources, after Library. Superusers see it there; a superuser
  can add it to a role's menu in the administration's *Menu* page (no role has it until then). Its
  address opens for every signed-in person whose menu does not show it, from links and bookmarks.
  Donor accounts never see it.
- **One version at a time.** The line under the page's title says when the workbook was prepared (or,
  when the workbook does not say, when it was loaded), when it was loaded, and how many documents and
  findings it holds, with the number marked for review.

## The two tables

- **Findings** (the first tab, with its number of findings): each finding's key finding (the first
  lines; **Show all** opens the rest), its Finding ID, its pillar and research question (hold the pointer
  over a code to read it in full), its document with the year, and the confidence level. On a phone the
  table shows the finding only, with its document, pillar, question and level on a line under it.
- **Documents** (the second tab, with its number of documents): each document with its Document ID, its
  publishing entity or authors as the workbook gives them (on screen only: never printed or downloaded),
  its year, its type, its confidence level and the number of its findings (press it to see them in the
  Findings tab). Under a filter or a search, the number counts the findings that match it ("2", "of 3"),
  and pressing it shows those findings, with the same filter.
- Each table shows 25 rows a page; **Rows a page** under it shows 50 or 100. The newest year comes
  first, then the workbook's order; after a search, the best match comes first. Press a column's title to
  sort by it (the pillar, the document, the year, the confidence level; on the Documents tab the title,
  the year and the number of findings).
- **Marked for review**: the workbook itself marks this finding or document for review. It is shown, never
  hidden.
- **Check this value**: a value that is not one of the expected ones (for example a confidence level that
  is not Level A to D). It is shown as the workbook has it; hold the pointer over the mark to read it.
- **Confidence levels**: Levels A to D, as the workbook grades the evidence. The workbook does not say
  which level is the strongest, so every level is shown in the same colour.

## Finding what you need

- **Search** (always in view, also on a phone): type words and the table keeps the findings that hold
  them all, in their key finding, measure, analysis, population, methods, limitations or document title.
  Words are found in their other forms ("school" finds "schools"), UK and US spellings find each other
  ("labour" and "labor", "diarrhoea" and "diarrhea") and common acronyms find what they stand for ("GBV"
  and "gender-based violence", "OOSC" and "out of school", "MHPSS"). "Out of school", "drop out" and "well
  being" are found as phrases (never every finding that says "school"). Words joined by a slash are looked
  for one by one ("girls/boys"), and "school-age" also finds "school age". Put a phrase in quotes ("child
  marriage"), a minus before a word to leave it out (-camp), or "or" between words for either. A Finding ID
  or a Document ID opens that finding or that document's findings. When no word matches, the table shows
  the findings that contain the letters you typed, and says so.
- **The filters**: *Pillar*, *Research question* (listed under its pillar: the same code is worded
  differently in each pillar), *Confidence level*, *Year* (or *Not reported*) and *Document type*.
- **More filters**: *Analytical domain*, *Age groups*, *Girls and boys*, *Level of the social-ecological
  model*, *Cross-cutting themes*, where the evidence comes from (collected by the document itself, or
  from other sources) and *Data quality* (ready, or marked for review or with a value to check). Tap a
  choice to add it; tap it again to take it out.
- **How choices combine**: several choices in one filter show findings that match any of them; several
  filters together show findings that match all of them. Age groups and themes count a finding marked Yes
  for them. A research question picked in another pillar than the pillars picked is left out,
  and the table says so.
- On a phone the filters fold behind **Filters**; the search box stays in view. **Reset** clears them all.
- The two tabs keep the filter as you move between them, and a filter or search you clear stays cleared
  (the Documents tab has no document filter: it lists each document).

## A finding and its document

- **Open** (on each row) shows the whole finding in a window, without leaving the table; *Open as page*
  gives it its own page, to share or print.
- A finding's page starts with the key finding, then: *The finding* (its analysis, measure and
  limitations), *Where it fits* (pillar, analytical domain, research question and its parts), *Who it is
  about* (the age group as written, then what is marked Yes, by age groups, girls and boys,
  levels and themes; the rest folds under *Not marked Yes*), *The evidence* (origin, confidence level,
  methods and sample, period, data quality) and *Its document*. Empty fields are not
  shown.
- When a finding's type differs from its document's, the page gives both.
- A document's page lists the document's details, then its findings, 25 a page. *Open in
  the table with filters* shows them in the Findings tab, to filter or download.
- A link to a finding or document that a newer version removed says so, with a link back to the table.

## Downloads and printing

- **Excel of the filter** and **CSV of the filter** (over each table) download every row of the filter
  and the search, with every column. The Excel file has an *About* sheet: when the version
  was prepared and loaded, the filter in words, the number of rows and the rules below. An Excel file
  holds up to 20,000 findings; the CSV file has no limit.
- **Authors are shown on screen only.** The files name the organisations an entry names and count the
  individual authors without naming them: "UNICEF + 2 authors", "Authors: 3", or the organisations alone.
  When it is not sure whether a name is a person or an organisation, it counts it as a person.
- **Names of people inside the texts are withheld** in the files: citations ("Doe et al. (2019)", "(Doe,
  2019)", "(Doe 2019, Roe 2020)", "Doe and Roe 2020"), people quoted by their first name and age, a
  document's own authors and names after a title are written "[name withheld]". Links and figures are kept.
- Your search words are written in the *About* sheet only when none of them names an author.
- **Excel of these findings** and **CSV of these findings**, under each answer of *Ask the KM Database*,
  download the findings that answer read, with every column and the same rules: those it cites first,
  marked *Yes* in the column `cited_in_answer`, then the others it read. The *About* sheet says which
  question (as it was kept, with names taken out) and when it was asked. Only you can download the
  findings of your own answers that were answered, for 90 days, and only while the version the answer
  read is shown: once a newer version is loaded, ask the question again. They open in a new tab, so that
  your conversation stays on screen.
- **Printing** (the page's *Print* button, or the browser's) follows the same rule as a download: the
  authors' column and lines are left off paper (the organisations are printed instead), and names of
  people inside the texts are printed "[name withheld]". The screen still shows the texts in full.
- An administrator can correct how an author entry is written in downloads (an organisation the rule
  missed, a person it took for an organisation, or words that are no name at all) in the administration:
  *KM Database* → *Organisations named in downloads*, or from the version's page (*Count as a person*,
  *Not an organisation*, *Not a name*, *Count as an organisation*). Downloads change at once.

## Ask the KM Database

The third tab, **Ask the KM Database**, answers a question in a paragraph or a few (about 250 words, up to
about 400 when you ask for a paragraph, a description or a summary) from the findings of the KM Database,
and from nothing else. Under every answer, **Excel of these findings** and **CSV of these findings**
download the findings it read.

- **What it reads.** Only the findings of the version shown now that match your question: their key
  finding, measure, ages, analysis, limitations and data collection period, with their document's title,
  year, type, population and methods. It looks for your words in each finding's own text (not in the
  research questions or indicators it is filed under), with UK and US spellings and common acronyms
  joined, and reads the best matches (at most 40, and at most 6 from one document). Nothing else from
  NeuroDB is read: no partner, programme document, indicator, document of the Documents page, library
  entry or person.
- **Write the subject, the rest is understood.** Words that say what you want rather than what it is about ("I
  need a paragraph describing", "adjust the text above", "an excel sheet with each indicator and value",
  "only", "could you provide the available evidence on", "hi", "asap", "a short table") are not searched while
  other words remain, nor are words that ask for figures ("rate", "proportion", "figures", "statistics").
  "Trend" only puts the findings that give one first. A finding is kept when it holds your words: all of them
  when you write one or two (when no finding holds both of two, those holding one are read, and the line under
  the answer says so), and a third of them (at least two) when you write more. A question for a number, a rate
  or a percentage ("how many", "number of") reads the findings that give figures first. "Dropout" also finds
  "dropped out" and "drops out" ("children dropping out of school" is a dropout), and "gender" also finds
  findings on girls or boys. A misspelt word that no finding holds ("povrety") is read as the closest word the
  findings write ("poverty") when the AI suggests no words; a word that is spelt right is never read as another
  ("studying" stays "studying", "Syria" stays "Syria").
- **Places.** Every governorate (Akkar, Baalbek-Hermel, Beirut, Bekaa, Keserwan-Jbeil, Mount Lebanon,
  Nabatieh, North, South) is understood in its usual spellings ("Mont Lebanon", "Jabal Lubnan", "Nabatiyeh",
  "Nabatiye", "Beqaa", "Beyrouth", "Liban-Nord", "Liban-Sud"), with or without accents or a French article
  ("la Békaa", "au Mont-Liban") and in Arabic ("عكار", "البقاع"), with its districts, main towns and camps,
  each town in its district: a question about the South also reads findings about Saida, Tyre, Jezzine, Qana
  or Ein el-Hilweh, one about Zahle also Bar Elias, and Mount Lebanon also Jbeil and Keserwan, as older
  reports count them ("Bint Jbeil" is in Nabatieh, not Jbeil; "South Metn" is Baabda, in Mount Lebanon; "North
  Bekaa" is Baalbek-Hermel; "South Bekaa" is the Bekaa). A finding that names both, such as "the Bekaa and
  North Bekaa", counts for both places, and a list of places names each of them: "the South, Bekaa and Akkar",
  "the North, Bekaa and Akkar" or "the South. Bekaa ..." is never South Bekaa or North Bekaa (only a space, a
  hyphen or a dash joins the words of one place's name, as in "North-Bekaa" or "Bint–Jbeil"), and "the
  North/Bekaa" names the North and the Bekaa. "Bint Jbeil-based" is Bint Jbeil, never Jbeil. "South Lebanon",
  "the south", "Liban-Sud", "the southern governorates", "the southern region" or "areas" and "Janoub" mean
  the South and Nabatieh governorates, whatever words follow ("the southern governorates in light of the
  escalation"); "the north", "North Lebanon", "Liban-Nord", "the northern governorates" and "the northern
  region" mean the North and Akkar; "North governorate" or "South governorate", and "North Lebanon
  governorate" or "the governorate of South Lebanon", mean that governorate only; the line under the answer
  says so. "The southern region of the Bekaa" (a place after "of", "in" or "within")
  is the Bekaa. The answer reads only findings that name the places you write; when fewer than 10 do, it also
  reads the others that match your words, and the line under the answer says so ("No finding on this subject
  names Mount Lebanon: findings from all of Lebanon were read."). When none of your words is in any finding,
  the findings that name the place are read. "All governorates", "by governorate", "per governorate", "across
  governorates", "across Lebanon's governorates", "in all eight governorates", "at governorate level", "all
  gov", "by district" or "by governorate and district" reads the findings that name a governorate, a district
  or a town, those naming the most governorates first ("per", "across", "eight" or "level" is never looked for
  itself), and a finding must still hold your subject's words; "all Lebanon", "nationwide", "Lebanon-wide",
  "across the country" or "national level" reads every place. "National" with a place ("compare the national
  rate with Akkar") reads every place, the findings on that place first; without a place it is a word of your
  subject ("the national education strategy"), never looked for alone: when no finding holds your other words,
  nothing matches, rather than findings that only say "nationally".
- **Years.** "In the past 2 years" means this year and the two before it (in 2026: 2024 to 2026), "over the
  last decade" 2016 to 2026, "in the past year" 2025 and 2026, and "last year" alone 2025 only ("compared to
  last year" 2025 and 2026); "since 2023",
  "2023 onwards", "2019-2021", "in 2023", "2023" alone, "2022 and 2025" (each year), "this year", "recent
  figures", "latest" and "anything newer" work too, also before a comma or a full stop ("In 2024, ..."), as a
  school year ("the 2024 school year") and as the year of the data ("the 2024 out of school figures", "the
  2024 situation", "the 2024 child labour rate"): they set the Year filter, without the findings whose year is
  not reported, and the line under the answer says which years. To change them, use the Year filter of the
  Findings tab. The years are those the documents were published. For "latest" or "most recent", when fewer
  than 10 findings of the past 2 years match, the newest older ones are read too, and the line says so.
  "Recently displaced children" or "targets for 2030" set no year, nor does the year of an event ("the 2020
  explosion", "after the 2024 war", "the 2019 financial and economic crisis", "the 2020 port of Beirut
  explosion"; "in 2024 amid the escalation" and "in 2025 following the 2024 war" are the years 2024 and 2025)
  or a year you compare with on its own ("compared to 2019", "before the 2019 crisis"): the findings that
  mention that year or the event only come first, and the event's words are never required (unless you write
  nothing else: "the 2020 explosion"). A year you compare with next to a year of your own is read too: "in
  2025 than in 2019" reads 2025 and 2019, and so does "and compared to 2019?" after a question on 2025
  ("before 2020" never reads 2020). "Since the 2019 crisis" means since 2019.
- **The same text twice.** A finding whose text is word for word another one's in the same document is
  read once; the answer may cite either, and both open their finding. The same text in two documents is
  read twice, each with its own document and year.
- **Your filters.** Open the tab from the Findings tab after filtering (a pillar, a year, one document...)
  and the answer reads only the findings of that filter; the line over the box names it, with **Clear**.
  The words of the search box are not used: your question is its own search. On a finding's or a
  document's page, **Ask about its document** opens the tab for that document. You can also write a
  code in the question: "Level A", "P2" (a pillar), "RQ3" (a research question), a Finding ID or a
  Document ID.
- **What is sent to the AI.** Your question, the earlier questions and answers of the conversation, the
  matching findings it reads, your filters in words, the date you asked and what the search understood of
  places, years and a follow-up. Never the authors, never Document IDs (one you type goes as "this document"),
  links or phone numbers, and never a name of a person: names of NeuroDB's people and of the KM Database's
  authors are taken out of your question, in capitals or not, and names inside the findings (citations,
  reference lists, people quoted, people named with their job) are written "[name withheld]". A town's name
  that is also a family name ("Saida", "Naameh"), or a place's name made only of words a name has ("Saad
  Nayel", "Tal Abbas", "Bar Elias"), is kept as a place only before a year after "in", "for" or "from" ("in
  Saida 2023", "numbers for Sayda, 2024", "in Tyre, Qana and Houla 2024", "What about Saida and Tyre 2023?"),
  and never when it is the surname of one of the KM Database's authors; written as a citation ("Naameh (2021)", "Naameh et al.", "Saad Nayel et
  al.", "Jabal Mohsen et al.", "from Akar & Naameh 2020", "As reported in Naameh 2021", "Similar results were
  found in Naameh 2021", "Akar and Naameh 2020 found", "Building on the work in Akar 2020", "Figures from
  Kousba 2019", "(for Naameh, 2021)", "Table adapted from Naameh 2021", "According to Naameh 2021", "Sources:
  Akar 2018; Naameh 2021", "This was examined in Naameh 2021", "See the chapter in Hasroun 2021", "This was
  collected in Sayda 2021", "Families from Houla 2021 adds that ...", a list with a year after each name "Rates
  in Akar 2016, Naameh 2018 and Kousba 2019 rose", "in Naameh
  2021a", "in Naameh 2021, p. 12") it is a name. "Show", "Compare" or "In" before two places stays ("Show
  Beirut and Tyre 2023"), and so does an exam, a school level, a subject or a tool after "In", "Source:" or
  "Data:" ("In Brevet 2023", "Source: Fieldwork 2023", "Data: Interviews 2023"). So is a person quoted with an age ("“…,” said Fakename, 14",
  "In an interview, Fakename, 14,") and a name before a year where the word is no event or topic ("Storm 2021
  (Lebanon)"; "Cholera 2022 (Bebnine)" stays). An e-mail address is taken out whole, also when it is written
  out with "at" and "dot" in words, with dashes or in brackets ("x [ at ] example [ dot ] org", "x -at-
  example -dot- org"), in your question and in the downloads; in your question also any name before "at" and a
  web address, in capitals or not, while a common word stays ("children at [link withheld]"). If a name is
  still found, nothing is sent.
- **The answer.** Each sentence ends with the findings it rests on, as [references]: each one opens its
  finding, in a new tab. An opening sentence that sums up the answer may have none. Figures are given
  exactly as each finding states them: never added up or averaged, compared only when each carries its
  reference. A reference to a finding that was not read for this answer is taken out, and
  the answer says how many. Under the answer, **Findings used for this answer** lists them (with their
  document, year and confidence level), a fold lists every finding read, and **Excel of these findings**
  and **CSV of these findings** download them. The line under the answer says "Answered from N of about M
  matching findings": when more match than were read, narrow the filters to read others.
- **Tables and Excel.** Ask for a table, the figures, each indicator or "numbers and percentages", and the
  answer may end with one short table: the indicator or measure, the figure exactly as its finding states it
  (a percentage is never worked out from a number), the year and the finding's reference, at most about 15
  rows. A row that gives no finding read for this answer is taken out, whatever way the table is written
  (a reference written as code is read as any other), and the answer says how many; a table the AI writes
  in HTML is taken out whole. The text you copy, and the one a follow-up rewrites, is the answer as shown.
  On a phone, a wide table scrolls sideways inside the answer. Ask for an Excel or a sheet and the answer
  points to **Excel of these findings** under it: every finding it read, with the figures inside their key
  finding and measure (the KM Database has no separate column of figures).
- **When the findings do not answer.** The answer says "The KM Database does not answer this question." and
  what related evidence it holds; "answers this only in part" when part is missing, with what the search could
  not find when it can say ("no finding on this subject names Mount Lebanon", "no finding of 2022", "no
  finding read compares governorates"). A sentence that cites no finding is counted under the answer: check it
  against the findings (the opening sentence, a sentence whose reference ends the next one of its paragraph,
  a sentence saying what the findings lack or what is missing from them, such as "Figures by disability
  are missing for the South", unless it goes on with a figure, such as "..., where 60% of girls left
  school", and one that only points to the Excel under the answer, such as "An Excel
  and a CSV of the findings read are available under this answer", are not counted; a sentence about
  people, such as "girls do not report it", "girls are missing from secondary school" or "teachers said
  they do not give information", is, and so is a pointer to the Excel that says something more, such as
  "..., mostly from Akkar, where girls left school", and a claim written as a table's heading).
- **When the AI is off or a limit is reached** the tab still lists the findings that match your question,
  without a written answer, and says why (to download them, use **Excel of the filter** on the Findings
  tab). Search and the filters work as usual.
- **Limits.** 30 questions a day per person, 2 at the same time, and the KM Database's own share of the
  day's AI budget (see [AI limits](/help/ai-limits/)). A question counts once the AI has worked on it,
  even when no finding matches (the AI first suggests search words, also when a word of your question is
  in no finding, such as a word misspelt). A question over a limit, or one the AI declines, does not
  count. How much the AI thinks before it answers is set by an administrator in AI settings (*KM
  Database assistant effort*, medium by default): more is slower and uses more of the day's budget.
- **Long questions** are read whole; the findings that match the end of the question (most often its
  topic) come first, unless the end only asks for a format ("once done, an excel sheet").
- **A conversation.** A follow-up keeps the subject, places and years of your earlier question and reads
  the findings its answer cited again. A question is a follow-up when it is short ("and for boys?"), when
  it refers to the answer ("do it for all Lebanon", "adjust the text above", "the same for Akkar", "redo",
  "what about child marriage?", "and for girls", "and children with disabilities?", "and Syrian refugee
  children?", "now with a focus on boys", "now focus on refugees", "focus on girls instead"), when it asks
  to rework the answer in any words, at its start, after a mark or after praise ("revise the paragraph for
  girls", "rephrase the answer in simpler words", "make
  it shorter", "make it more detailed", "can you elaborate on this?", "give more details", "summarise it
  in bullet points", "convert it to a table", "write it for a donor audience", "the same answer but with
  a gender lens", "translate it to Arabic", "translate everything into Arabic", "in French please", "this
  is good but can you shorten it", "i like it but can you shorten it", "good job, now rewrite it for girls",
  "the paragraph is too long please shorten it"), or when it has no
  subject of its own (only places, years or what you want: "what about Akkar since 2024?", "once done I
  need an excel sheet", "the same in Arabic", "I need it shorter", "is there an update this year?", which
  keeps the subject and place with this year). A "Thanks," "great," or "this is good" in front changes
  nothing. Any other question that names its subject, even in general words ("the situation of children
  for all governorates") or with words such as "again", "instead", "update" or "summarise this year's"
  ("why did enrolment fall again?", "why are boys sent to work instead?", "is there an update this year on
  child marriage?", "translate the key findings on child marriage into Arabic", "give me the number of out
  of school children in lebanon and break it down by governorate", "Focusing on Akkar, what are the main
  barriers to education?"), starts afresh, and so does a short question with a subject and a place or
  years of its own ("stunting in Baalbek?", "child marriage Akkar", "child marriage 2024"; "child marriage"
  alone or "girls in Akkar?" stay follow-ups). A question that matched no finding counts in the
  conversation too: "same for Akkar please" after it keeps its subject. Words such
  as "Arabic", "informal", "focus" or "points" are searched as any other ("Arabic literacy", "informal
  settlements", "water points"), except in a request to rework the answer or to have it in another
  language or length. When no finding of the place joins a follow-up's own words to the earlier subject,
  a group of people ("and for girls?", "what about girls?", "what about refugees?") or an addition ("and
  the trend?", "and by nationality?") that findings elsewhere join keeps the subject, and the findings of
  all of Lebanon are read; otherwise its own words
  are read alone, in the earlier place and years. An aspect of the earlier subject ("what about the
  barriers?", "how about the main reasons?", "barriers in Akkar?", "sources for Tripoli?") stays on that
  subject: its barriers, its reasons. A switch to another subject ("what about vaccination?",
  "how about vaccination?", "vaccination instead", "do the same for vaccination" after a question on child
  marriage in Akkar) reads that subject alone in the earlier place and years at once, never only the
  findings that hold the earlier subject too. A follow-up's own words on a subject of their own ("what
  about child marriage?", "child marriage", "and for child labour?", "what about ECD?" after a question on
  Tripoli: that subject in Tripoli) become the subject of the next follow-up, as they were read (when a
  finding with one of its words was enough, so it is next time), and the earlier answer's findings on
  another subject are not read first; a rework or a group of people (girls, boys, an age group, disability,
  refugees, displaced, Syrian, Palestinian or migrant children, the host community) stays an angle on the
  earlier subject. A group of people replaces the earlier one of its kind ("and for girls?" after "and for
  boys?" reads girls, not girls and boys); groups of other kinds add up ("and for girls?" after "what about
  refugees?": refugee girls). A follow-up with no words of its own ("can you do it for all governorates?",
  "can you do it for all Lebanon?" after a question on a place) is answered from the earlier subject, and
  counts like any question. Its own place, years, "all Lebanon" (no place) or "all governorates" (findings
  that name a governorate) replace the earlier ones; "and by district?" after a place reads that place by
  its districts (the findings that name more of them first); "anything newer?", "more recent?" or "any
  update?" reads the years after the earlier ones up to this year (the newest findings of the same place
  when none is newer, and the line says so); its own words are angles added to the subject ("with a lens on gender, and
  disability": a finding on either). The places and years of an earlier question are kept however many
  reworks come between (the last 20 questions of the conversation that were answered or matched nothing
  are read for them; the AI is sent the last 4 answered ones). The line under the answer says what was
  kept. You can ask to rewrite the previous answer (another angle, place, period, length, form or
  language): it is rewritten from the findings it cited, read again, keeping their references, and never
  from the earlier answer alone. A short question on a new topic ("Child marriage?") that no finding joins
  to the earlier subject reads its own findings. **New conversation** starts afresh. **Your recent questions** lists your last ten; press
  one to ask it again. Your questions are kept 90 days.
- The AI can be wrong: open the findings it cites before you use an answer.

## When a new version is loaded

An administrator uploads a new version of the workbook in the administration: *KM Database* →
*Versions* → **Upload a new version** (or *Upload the KM Database* on the administration's home page).
The workbook is checked first; if something is wrong, nothing is loaded and the page says what to correct,
with the row numbers as Excel shows them. A workbook that passes is loaded in the background (about a
minute). Until it is ready, the page shows the version it showed before, with a line saying a new version
is loading (the very first time, it says the KM Database is being loaded). Once it is ready, the page shows
the new version: every document and finding of the old one is replaced. If the load does not finish, the
version shown stays as it was.

A link to a finding that the new version no longer holds says so. A reference in an older answer opens
the finding with a line saying the answer was written from an older version, so the finding may have
changed since; earlier answers are not sent again once a new version is shown.
