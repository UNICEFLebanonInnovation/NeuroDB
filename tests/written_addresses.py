"""E-mail addresses written out in every form the AI privacy rules know (an invented name, example.org and
.invalid domains only), the sentences they are written in, and how what is left of one is read in a text
sent to the AI. Shared by the tests of each assistant (Monitoring insights, Help, Ask NeuroDB, NeuroDB
Watch, the daily review, What's new, the knowledge base, the KM Database)."""

import re
import unicodedata

LOCAL = "zfakemira"
DOTTED = "zayna.fakemira"
FORMS = {
    "F00 plain @": f"{LOCAL}@example.org",
    "F01 at": f"{LOCAL} at example.org",
    "F02 (at)": f"{LOCAL}(at)example.org",
    "F03 [at]": f"{LOCAL}[at]example.org",
    "F04 {at}": f"{LOCAL}{{at}}example.org",
    "F05 <at>": f"{LOCAL} <at> example.org",
    "F06 -at- -dot-": f"{LOCAL} -at- example -dot- org",
    "F07 [ at ] [ dot ]": f"{LOCAL} [ at ] example [ dot ] org",
    "F08 at dot": f"{LOCAL} at example dot org",
    "F09 at Example.Org": f"{LOCAL} at Example.Org",
    "F10 capitals": "ZFAKEMIRA AT EXAMPLE.ORG",
    "F11 .invalid": f"{LOCAL} at mail.example.invalid",
    "F12 spaced dot": f"{LOCAL} at example. org",
    "F13 Cyrillic a": f"{LOCAL} \u0430t example.org",
    "F14 full-width at": f"{LOCAL} \uff41\uff54 example.org",
    "F15 full-width dot": f"{LOCAL} at example\uff0eorg",
    "F16 invisible in at": f"{LOCAL} a\u200bt example.org",
    "F17 invisible in the name": "zfake\u200bmira at example.org",
    "F18 (at) (dot)": f"{LOCAL} (at) example (dot) org",
    "F19 dotted name": f"{DOTTED} at example.org",
    "F20 dotted name, Example.Org": f"{DOTTED} at Example.Org",
    "F21 underscore, .invalid": "zayna_fakemira at example.invalid",
    "F22 dotted name, at dot": f"{DOTTED} at example dot org",
    # an at sign before a spelled dot, a spelled dot before any ending, a space before the dot
    "F23 @ and dot": f"{LOCAL} @ example dot org",
    "F24 [@] [dot]": f"{LOCAL} [@] example [dot] org",
    "F25 @, dot com": f"{LOCAL}@example dot com",
    "F26 dot .invalid": f"{LOCAL} at example dot invalid",
    "F27 (at) (dot) .invalid": f"{LOCAL} (at) example (dot) invalid",
    "F28 [.] .invalid": f"{LOCAL} at example[.]invalid",
    "F29 underscore, dot .invalid": "zayna_fakemira at example dot invalid",
    "F30 dot, a country's ending": f"{LOCAL} at example dot sy",
    "F31 space before the dot": f"{LOCAL} at example .org",
    "F32 underscore, spaced dot": "zayna_fakemira at example. org",
    # every invisible character: the marks of right-to-left text, the isolates, the joiners
    "F33 left-to-right mark": f"{LOCAL}\u200e@example.org",
    "F34 right-to-left mark in at": f"{LOCAL} a\u200ft example.org",
    "F35 isolates": f"\u2066{LOCAL}\u2069 at example.org",
    "F36 right-to-left mark in the name": "zfake\u200fmira@example.org",
    "F37 grapheme joiner": "zfake\u034fmira@example.org",
    "F38 vowel separator": "zfake\u180emira@example.org",
    "F39 invisible times": f"{LOCAL} a\u2062t example.org",
    "F40 left-to-right mark after @": f"{LOCAL}@\u200eexample.org",
    # styled letters (as a "fancy text" site writes them)
    "F41 mathematical at": f"{LOCAL} \U0001d68a\U0001d69d example.org",
    "F42 small capitals at": f"{LOCAL} \u1d00\u1d1b example.org",
    "F43 circled at": f"{LOCAL} \u24d0\u24e3 example.org",
    "F44 mathematical at, dot": f"{LOCAL} \U0001d68a\U0001d69d example \U0001d68d\U0001d698\U0001d69d org",
    "F45 all mathematical": "".join(
        chr(0x1D5EE + ord(c) - 97) if c.isalpha() else c for c in f"{LOCAL} at example.org"
    ),
    # more ways of writing "at" and the dot
    "F46 arobase": f"{LOCAL} arobase example.org",
    "F47 quoted at": f"{LOCAL} 'at' example.org",
    "F48 starred at": f"{LOCAL} *at* example.org",
    "F49 at-sign": f"{LOCAL} at-sign example.org",
    "F50 slashed at": f"{LOCAL} /at/ example.org",
    "F51 doubled at": f"{LOCAL} at at example.org",
    "F52 period": f"{LOCAL} at example period org",
    "F53 dashes": f"{LOCAL}-at-example-dot-org",
    # a web address's shape: never an e-mail address's, but a name before "at" is still one
    "F54 www": f"{LOCAL} at www.example.org",
    "F55 a path after": f"{LOCAL} at example.org/ops",
    "F56 a path after a spelled dot": f"{LOCAL} at example dot org/ops",
    # a space after the dot before another ending, or a capital a phone's keyboard puts there; a quoted,
    # starred or slashed "dot"; "<.>"
    "F57 spaced dot, a country's ending": f"{LOCAL} at example. lb",
    "F58 spaced dot, a capital": f"{LOCAL} at example. Org",
    "F59 spaced dots, two labels": f"{LOCAL} at example. net. lb",
    "F60 spaced dot, .invalid": f"{LOCAL} at example. invalid",
    "F61 quoted at and dot": f"{LOCAL} 'at' example 'dot' org",
    "F62 starred at and dot": f"{LOCAL} *at* example *dot* org",
    "F63 slashed at and dot": f"{LOCAL} /at/ example /dot/ org",
    "F64 <.>": f"{LOCAL} at example<.>org",
    # a name holding a digit before a domain whose ending is also a word of two letters
    "F65 a digit in the name, .it": "zfakemira2 at example.it",
    # "(a)" for the at sign, look-alike dots and the Latin alpha, a dashed or underscored .invalid, any ending
    # after a spelled dot that follows a name with a full stop
    "F66 (a)": f"{DOTTED}(a)example.org",
    "F67 middle dot": f"{LOCAL} at example\u00b7org",
    "F68 Latin alpha": f"{LOCAL} \u0251t example.org",
    "F69 dashes, .invalid": f"{LOCAL}-at-example-dot-invalid",
    "F70 underscores": f"{LOCAL}_at_example_dot_org",
    "F71 dotted name, dot and a long ending": f"{DOTTED} at example dot online",
    # an ending mistyped or no known one, after a plain name ("x at example.ogr", "x at example.online"): an
    # ending of three letters or more counts, unless it is a word that goes on with a sentence ("at
    # Halba.please")
    "F72 a mistyped ending": f"{LOCAL} at example.ogr",
    "F73 a mistyped ending, dot": f"{LOCAL} at example dot ogr",
    "F74 a long ending": f"{LOCAL} at example.online",
    "F75 a mistyped .com": f"{LOCAL} at example.con",
    "F76 a capital, a mistyped ending": "Zfakemira at example.orf",
    # a space after the dot before .net, .info or .de, then no word of a sentence ("x at example. net before")
    "F77 spaced dot, .net": f"{LOCAL} at example. net",
    "F78 spaced dot, .info": f"{LOCAL} at example. info",
    "F79 spaced dot, .de": f"{LOCAL} at example. de",
    "F80 @, spaced dot, .info": f"{LOCAL}@example. info",
    "F81 @, spaced dot, a capital": f"{LOCAL}@example. Net",
    "F82 spaced dot, a capital, .net": f"{LOCAL} at example. Net",
    # an ending that is also a word a chat goes on with (".today", ".show", ".plus", ".cool", ".lol", ".np"):
    # an address's after a plain name, unless a word of the sentence follows ("at akkar.show me by district")
    "F83 .today": f"{LOCAL} at example.today",
    "F84 .show": f"{LOCAL} at example.show",
    "F85 .plus": f"{LOCAL} at example.plus",
    "F86 .cool": f"{LOCAL} at example.cool",
    "F87 .lol": f"{LOCAL} at example.lol",
    "F88 .np": f"{LOCAL} at example.np",
    # an ending a sentence may start with after a dot and a space ("x at hotmail. fr"), a common word's ending
    # (".school", ".health"), a digit inside the name or "AT" in capitals before a word of two letters
    "F89 spaced dot, a starter": f"{LOCAL} at example. fr",
    "F90 a common word's ending": f"{LOCAL} at example.school",
    "F91 a digit inside the name, .it": "z2fakemira at example.it",
    "F92 capitals, .it": "ZFAKEMIRA AT example.it",
    "F93 capitals, .IT": "ZFAKEMIRA AT EXAMPLE.IT",
    "F94 letters then digits in capitals, .be": "ZFAKEMIRA2 at example.be",
}
# What a record's or a document's text keeps by design (the record rule): a plain word before "at" and a
# domain in capitals, of another ending or with a space after its dot ("published at Example.Org", "located
# at Tyre.Net", "located at Tyre. Net" stay), and look-alike or styled letters, read as the plain ones only in
# what a person types
RECORD_KEEPS = (
    *("F09", "F11", "F12", "F13", "F14", "F15", "F41", "F42", "F43", "F44", "F45"),
    *("F57", "F58", "F59", "F60", "F67", "F68"),
    # a plain word before an ending no address has in lower case ("located at Tyre.Net"), or before a space
    # and .net, .info or .de ("at Halba. net enrolment"): only in what a person types
    *("F72", "F74", "F75", "F76", "F77", "F78", "F79", "F82"),
    *("F83", "F84", "F85", "F86", "F87", "F88", "F89", "F90"),
)
RECORD_FORMS = {form: written for form, written in FORMS.items() if form[:3] not in RECORD_KEEPS}
# a sentence a person types, and one of a record or a document
TYPED = "out of school children in Akkar, contact {} before June"
RECORD = "For the data, write to {} before June."
# sentences of records that are no address, and how a record's text is sent (links withheld as links)
NOT_ADDRESSES = {
    "The report was published at Example.Org last year.": "The report was published at [link withheld] last year.",
    "The office is located at Tyre.Net in the south.": "The office is located at [link withheld] in the south.",
    "Children at school.it was said": "Children at school.it was said",
    "We meet at 5 on Monday.": "We meet at 5 on Monday.",
    "Teachers look at data each week.": "Teachers look at data each week.",
    "More children at school this year.": "More children at school this year.",
    "The guide is at UNICEF.ORG/report now.": "The guide is at [link withheld] now.",
    "The guide is at unicef.org/report now.": "The guide is at [link withheld] now.",
    "Reports are available at www.unicef.org/lebanon for all.": "Reports are available at [link withheld] for all.",
    # a common word or a web address's word before "at": the domain goes as a link, the words stay
    "Learn more at unicef.org.": "Learn more at [link withheld].",
    "The dashboard at data.unicef.org was not updated.": "The dashboard at [link withheld] was not updated.",
    "The guidance is available at unicef.org.": "The guidance is available at [link withheld].",
    "Find it at mehe.gov.lb.": "Find it at [link withheld].",
    "Is the data at data.unicef.org up to date?": "Is the data at [link withheld] up to date?",
    "Children at school.info sessions were held for parents.": "Children at [link withheld] sessions were held for parents.",
    "The tool is hosted at kobo.org.": "The tool is hosted at [link withheld].",
    "Results are posted on the website at mehe.gov.lb.": "Results are posted on the website at [link withheld].",
    # a time, an ordinal or a code before "at", and a sentence missing its space after a full stop
    "The visit began at 2pm at Halba.We met the head teacher.": "The visit began at 2pm at Halba.We met the head teacher.",
    "The session started at 10am at school.It was well attended.": "The session started at 10am at school.It was well attended.",
    "Visit No.2 at Akkar.It was rescheduled.": "Visit No.2 at Akkar.It was rescheduled.",
    "Follow-up planned for Q3 at Akkar.It will check the latrines.": "Follow-up planned for Q3 at Akkar.It will check the latrines.",
    "DISTRIBUTION AT SITE.NO ISSUES REPORTED.": "DISTRIBUTION AT SITE.NO ISSUES REPORTED.",
    "THE VISIT TOOK PLACE AT BAALBEK.IT WAS A GOOD VISIT.": "THE VISIT TOOK PLACE AT BAALBEK.IT WAS A GOOD VISIT.",
    "Kits arrived at 9am at halba.no delays.": "Kits arrived at 9am at halba.no delays.",
    "Teachers look at the dot plot each week.": "Teachers look at the dot plot each week.",
    # a time or an item's number written with a full stop before "at" ("9.30am", "no.2"): no address's name
    "The meeting began at 9.30am at halba.we met the principal.": (
        "The meeting began at 9.30am at halba.we met the principal."
    ),
    "Arrived 10.15am at tyre.it was raining.": "Arrived 10.15am at tyre.it was raining.",
    "Arrived 10.15am at tyre.rain was heavy.": "Arrived 10.15am at tyre.rain was heavy.",
    "The session ended 12.30pm at saida.no further action.": "The session ended 12.30pm at saida.no further action.",
    "Arrived 8.45am at school.no teachers were present.": "Arrived 8.45am at school.no teachers were present.",
    "Visit no.2 at akkar.we met the principal.": "Visit no.2 at akkar.we met the principal.",
    "Visit nb.3 at bekaa.no issues.": "Visit nb.3 at bekaa.no issues.",
    "Follow-up planned for q3 at akkar.it will check the latrines.": (
        "Follow-up planned for q3 at akkar.it will check the latrines."
    ),
    # a citation's or a page's words before "at" and a web address: the domain goes as a link, the words stay
    "Report accessed at www.example.org/reports on 3 May 2024.": "Report accessed at [link withheld] on 3 May 2024.",
    "The full report can be accessed at www.example.org/report.": "The full report can be accessed at [link withheld]",
    "Contact us at www.example.org.": "Contact us at [link withheld]",
    "Visit us at www.example.org for the tables.": "Visit us at [link withheld] for the tables.",
    "The focal point at example.org confirmed.": "The focal point at [link withheld] confirmed.",
    "Further details on the methodology at survey.example.org.": (
        "Further details on the methodology at [link withheld]."
    ),
    "Guidelines at example.org were updated.": "Guidelines at [link withheld] were updated.",
    # a word holding a digit (a disease's, a grade's) before "at" and a sentence missing its space: no
    # address's name ("Cases of COVID-19 at school.no classes")
    "Cases of COVID-19 at school.no classes were held for a week.": (
        "Cases of COVID-19 at school.no classes were held for a week."
    ),
    "25 children in KG2 at school.it was crowded.": "25 children in KG2 at school.it was crowded.",
    "25 children in kg2 at halba.it was crowded.": "25 children in kg2 at halba.it was crowded.",
    "Cases of covid19 at halba.no classes were held.": "Cases of covid19 at halba.no classes were held.",
    # a class, a disease, a saint's school or an at sign with a space before a place, a full stop and a
    # sentence that starts with "Info", "Net", "NGO", "eg" or "uk": no address
    "Training @ Halba. Info session to follow.": "Training @ Halba. Info session to follow.",
    "Meeting @ Zahle. Net enrolment was discussed.": "Meeting @ Zahle. Net enrolment was discussed.",
    "Students from St.Joseph at Tyre. Info sessions were held.": (
        "Students from St.Joseph at Tyre. Info sessions were held."
    ),
    "Students of 9B at Halba. Info sessions were held for parents.": (
        "Students of 9B at Halba. Info sessions were held for parents."
    ),
    "Cases of H1N1 at school. Info sessions were held.": "Cases of H1N1 at school. Info sessions were held.",
    "Visit @ Akkar. NGO staff were present.": "Visit @ Akkar. NGO staff were present.",
    "Visit @ school. eg water and latrines were checked.": "Visit @ school. eg water and latrines were checked.",
    "Distribution @ Akkar. uk funded kits arrived.": "Distribution @ Akkar. uk funded kits arrived.",
    "Class 4B at Tyre. Info sessions for parents were held.": (
        "Class 4B at Tyre. Info sessions for parents were held."
    ),
    "Training @ CERD. Info packs were distributed.": "Training @ CERD. Info packs were distributed.",
    "Session @ school. Net enrolment rose by 5%.": "Session @ school. Net enrolment rose by 5%.",
    "Students of Grade 9B at Halba. NGO staff ran the session.": (
        "Students of Grade 9B at Halba. NGO staff ran the session."
    ),
    "Children from St.Elias at Zahle. NGO volunteers helped.": (
        "Children from St.Elias at Zahle. NGO volunteers helped."
    ),
    "Meeting @ Halba. NGO partners attended.": "Meeting @ Halba. NGO partners attended.",
    "Students in room-2 at school.it was hot.": "Students in room-2 at school.it was hot.",
    "Cases of h1n1 at school.no classes were held.": "Cases of h1n1 at school.no classes were held.",
    # a version before a link with its scheme: no address's name
    "Release v2.0 at https://example.org is out.": "Release v2.0 at [link withheld] is out.",
    # a field's code holding a digit (a visit's, a shift's, a camp's, a school's) before a place, a full stop
    # and a sentence that starts with "NGO", "Info", "Net" or "eg": no address
    "Visit2 at Akkar. NGO staff were present.": "Visit2 at Akkar. NGO staff were present.",
    "ITS12 at Bekaa. NGO staff were present.": "ITS12 at Bekaa. NGO staff were present.",
    "Students of shift2 at Tyre. NGO staff ran the session.": (
        "Students of shift2 at Tyre. NGO staff ran the session."
    ),
    "Visit2 at Halba. Net enrolment rose.": "Visit2 at Halba. Net enrolment rose.",
    "Lab3 at Halba. eg chairs were broken.": "Lab3 at Halba. eg chairs were broken.",
    "camp3 at Bekaa. NGO staff were present.": "camp3 at Bekaa. NGO staff were present.",
    "School2 at Saida. Info sessions were held.": "School2 at Saida. Info sessions were held.",
    "Monitoring visit to School2 at Akkar. NGO staff were present.": (
        "Monitoring visit to School2 at Akkar. NGO staff were present."
    ),
    "Shift2 at Saida. Net enrolment rose by 5%.": "Shift2 at Saida. Net enrolment rose by 5%.",
    "Centre2 at Zahle. Info desk was closed.": "Centre2 at Zahle. Info desk was closed.",
    "Visit to ITS12 at Bekaa. NGO staff were present.": "Visit to ITS12 at Bekaa. NGO staff were present.",
    "ITS012 at Bekaa. NGO staff were present.": "ITS012 at Bekaa. NGO staff were present.",
    "School12 at Tripoli. Info sessions were held.": "School12 at Tripoli. Info sessions were held.",
    "Cohort2 at Zahle. Info sessions were held.": "Cohort2 at Zahle. Info sessions were held.",
    # a field's code before a place and a sentence missing its space after it
    "Visit3 at halba.no issues were found.": "Visit3 at halba.no issues were found.",
    "Lab2 at school.it was closed for repairs.": "Lab2 at school.it was closed for repairs.",
    "Bus3 at akkar.no delays.": "Bus3 at akkar.no delays.",
    "PD2023 at akkar.no changes were needed.": "PD2023 at akkar.no changes were needed.",
    # capitals throughout (a note written in capitals): a sentence missing its space after a place
    "KITS DISTRIBUTED AT SITE.NO ISSUES.": "KITS DISTRIBUTED AT SITE.NO ISSUES.",
}
# a word that names no one before "at" and a domain of an ending the link rule does not know (".ngo", ".de",
# ".jo"): the domain goes as a link in a record's text too, the words stay
RECORD_LINKS = {
    "The focal point at zaynafakemira.ngo confirmed.": "The focal point at [link withheld] confirmed.",
    "Contact us at zaynafakemira.de for the files.": "Contact us at [link withheld] for the files.",
    "Write to us at zfakemira.ae for the files.": "Write to us at [link withheld] for the files.",
    "See the chart at zfakemira.ngo for details.": "See the chart at [link withheld] for details.",
    "The results at zfakemira.jo were shared.": "The results at [link withheld] were shared.",
    "The data is available at zaynafakemira.de for all.": "The data is available at [link withheld] for all.",
    "See the guidance at zaynafakemira.ngo for details.": "See the guidance at [link withheld] for details.",
    "Write to info at zaynafakemira.de for the files.": "Write to info at [link withheld] for the files.",
    "Email us at zfakemira.de.": "Email us at [link withheld].",
    "Contact us at zaynafakemira.online for the files.": "Contact us at [link withheld] for the files.",
    "Write to info at zaynafakemira.sy for the files.": "Write to info at [link withheld] for the files.",
}
# questions a person types that hold no address, and how they are sent: a place written with an abbreviation,
# a sentence missing its space, a web address after a word (withheld as a link, its path too)
TYPED_NOT_ADDRESSES = {
    "How many visits at Mt.Lebanon had red flags?": "How many visits at Mt.Lebanon had red flags?",
    "What happened at Mt.Lebanon?": "What happened at Mt.Lebanon?",
    "Show findings for schools at St.Joseph and Mar.Elias": "Show findings for schools at St.Joseph and Mar.Elias",
    "The AP was closed at Akkar.No evidence received": "The AP was closed at Akkar.No evidence received",
    "Can you look at the dot plot?": "Can you look at the dot plot?",
    "How many children per 10\u00b3 km\u00b2 in Tyre?": "How many children per 10\u00b3 km\u00b2 in Tyre?",
    "More children at example.org": "More children at [link withheld]",
    "Where is the dashboard at neurodb.example.org/fmm?": "Where is the dashboard at [link withheld]?",
    "How do I find the guide at www.unicef.org?": "How do I find the guide at [link withheld]?",
    "Who wrote the profile at example.org/staff/layla-saab?": "Who wrote the profile at [link withheld]?",
    "Which visits cite the report at example.org/reports/2024?id=5&owner=rhaddad": (
        "Which visits cite the report at [link withheld]"
    ),
    # a sentence missing its space after a place ("at Halba.please"): no address, whatever the word after it
    "What were the main issues at Halba.please list them": "What were the main issues at Halba.please list them",
    "how many schools were visited at zahle.thanks": "how many schools were visited at zahle.thanks",
    "dropout at saida.compare with 2024": "dropout at saida.compare with 2024",
    "issues at akkar.sorry I meant north": "issues at akkar.sorry I meant north",
    "visite at Halba.les enfants": "visite at Halba.les enfants",
    "how many visits at akkar.show me by district": "how many visits at akkar.show me by district",
    "how many visits at akkar.ok": "how many visits at akkar.ok",
    "any red flags at tripoli.thx": "any red flags at tripoli.thx",
    # a sentence after a place and a full stop, its first word capitalised as a phone's keyboard writes it
    "How many visits at Halba. Net enrolment rose?": "How many visits at Halba. Net enrolment rose?",
    "Which partner works at Tyre. De facto the office moved?": (
        "Which partner works at Tyre. De facto the office moved?"
    ),
    "Any visits at Akkar. Gov staff joined?": "Any visits at Akkar. Gov staff joined?",
    # a time before "at"
    "The meeting at 9.30am at halba.we met the principal?": "The meeting at 9.30am at halba.we met the principal?",
    # a page's words before a web address, with its scheme or without
    "I get an error at neurodb.example.org/fmm/visits": "I get an error at [link withheld]",
    "what does the chart at neurodb.example.org/fmm show": "what does the chart at [link withheld] show",
    # a link with its scheme is never an e-mail address: the word before "at" stays, the link goes as a link
    "What does the red flag at https://neurodb.example.org/fmm/visits/12 mean?": (
        "What does the red flag at [link withheld] mean?"
    ),
    "What does the score at https://neurodb.example.org/fmm/visits/12 mean?": (
        "What does the score at [link withheld] mean?"
    ),
    "The page is blank at https://neurodb.example.org/fmm/visits": "The page is blank at [link withheld]",
    "Data looks wrong at https://neurodb.example.org/fmm/visits": "Data looks wrong at [link withheld]",
    "I am stuck at https://neurodb.example.org/fmm/visits": "I am stuck at [link withheld]",
    "The export fails at https://neurodb.example.org/km/search.": "The export fails at [link withheld].",
    "Which file is broken at ftp://files.example.org/fmm?": "Which file is broken at [link withheld]?",
    # the same words before a web address with a path and no scheme
    "What does the red flag at neurodb.example.org/fmm/visits/12 mean?": (
        "What does the red flag at [link withheld] mean?"
    ),
    "The page is blank at neurodb.example.org/fmm/visits": "The page is blank at [link withheld]",
    # a place, a full stop and a sentence that starts with a word that is also a domain's ending ("eg", "uk",
    # "NGO", "ORG", "Invalid"): no address after a plain word
    "How many visits at Akkar. NGO staff were present?": "How many visits at Akkar. NGO staff were present?",
    "What were the problems at schools. eg water, latrines?": (
        "What were the problems at schools. eg water, latrines?"
    ),
    "Which issues were reported at schools. eg latrines, in Akkar?": (
        "Which issues were reported at schools. eg latrines, in Akkar?"
    ),
    "issues at akkar. uk funded projects only": "issues at akkar. uk funded projects only",
    "Which projects at Halba. ORG chart missing?": "Which projects at Halba. ORG chart missing?",
    "findings at halba. qa checks done?": "findings at halba. qa checks done?",
    "Any visits at Akkar. Invalid entries removed?": "Any visits at Akkar. Invalid entries removed?",
    "dropout at Tyre. fr version please": "dropout at Tyre. fr version please",
    "dropout at Halba. Ngo staff left": "dropout at Halba. Ngo staff left",
    "dropout at Akkar. LB 500 per child?": "dropout at Akkar. LB 500 per child?",
    "dropout at Akkar. Com staff left": "dropout at Akkar. Com staff left",
    "dropout at Akkar. ngo staff left": "dropout at Akkar. ngo staff left",
    # a place, a full stop and a sentence that goes on after "net", "info" or "de" ("net enrolment")
    "dropout at Halba. net enrolment fell": "dropout at Halba. net enrolment fell",
    "dropout at Halba. info sessions were held?": "dropout at Halba. info sessions were held?",
    "dropout at Tyre. de facto closures?": "dropout at Tyre. de facto closures?",
    "dropout at Halba. info on dropout?": "dropout at Halba. info on dropout?",
    "dropout at Halba. Info about girls?": "dropout at Halba. Info about girls?",
    "dropout at Tyre. De facto the office moved?": "dropout at Tyre. De facto the office moved?",
    # a sentence missing its space after a place, its next word one that goes on with a sentence
    "what happened at saida.also show 2024": "what happened at saida.also show 2024",
    "dropout at halba.please": "dropout at halba.please",
    "dropout at halba.thank you": "dropout at halba.thank you",
    "dropout at halba.next show tyre": "dropout at halba.next show tyre",
    # a measure of education before "at", a place, a full stop and "Net", "Info" or "De" that a sentence goes
    # on with, or ends with: no address
    "What was the enrolment at Halba. Net or gross?": "What was the enrolment at Halba. Net or gross?",
    "enrolment at Tyre. Net for girls": "enrolment at Tyre. Net for girls",
    "Show enrolment at Halba. Net only": "Show enrolment at Halba. Net only",
    "Show enrolment at Halba. net please": "Show enrolment at Halba. net please",
    "What is the dropout at Akkar. Info please": "What is the dropout at Akkar. Info please",
    "Was there a distribution at Tyre. Info is missing": "Was there a distribution at Tyre. Info is missing",
    "enrolment at Tyre. Net by grade": "enrolment at Tyre. Net by grade",
    "enrolment at Tyre. Net and gross": "enrolment at Tyre. Net and gross",
    "What was attendance at Zahle. Net or gross?": "What was attendance at Zahle. Net or gross?",
    "Enrolment rates at Saida. Net and gross, please.": "Enrolment rates at Saida. Net and gross, please.",
    "Enrollment at Saida. Net or gross?": "Enrollment at Saida. Net or gross?",
    "Rates at Halba. Net please": "Rates at Halba. Net please",
    "Show net enrolment at Halba. Net is higher than gross?": (
        "Show net enrolment at Halba. Net is higher than gross?"
    ),
    "Enrolment at Halba. Net?": "Enrolment at Halba. Net?",
    "Enrolment at Halba. Net.": "Enrolment at Halba. Net.",
    "What is the enrolment at Halba. Net": "What is the enrolment at Halba. Net",
    "What was the GER at Halba. Net or gross?": "What was the GER at Halba. Net or gross?",
    "Which schools at Halba. Net only?": "Which schools at Halba. Net only?",
    # a class, a saint's school or an at sign with a space before a place and a sentence: no address
    "Students from St.Joseph at Tyre. Info sessions were held?": (
        "Students from St.Joseph at Tyre. Info sessions were held?"
    ),
    "Class 4B at Tyre. Info sessions for parents?": "Class 4B at Tyre. Info sessions for parents?",
    "Meeting @ Halba. NGO partners attended?": "Meeting @ Halba. NGO partners attended?",
    # a place written with an abbreviation in lower case, or two places joined by a full stop, after "at": no
    # address and no link
    "How many visits at mt.lebanon had red flags?": "How many visits at mt.lebanon had red flags?",
    "any red flags at st.elias school?": "any red flags at st.elias school?",
    "visits at st.george hospital": "visits at st.george hospital",
    "red flags at akkar.district wise please": "red flags at akkar.district wise please",
    "enrolment at mt.lebanon schools": "enrolment at mt.lebanon schools",
    "attendance at st.charbel school 2024": "attendance at st.charbel school 2024",
    "how many visits at st.joseph school had red flags?": "how many visits at st.joseph school had red flags?",
    "red flags at mar.elias camp": "red flags at mar.elias camp",
    "how many students at mt.lebanon schools dropped out?": (
        "how many students at mt.lebanon schools dropped out?"
    ),
    "visits at halba.bekaa next": "visits at halba.bekaa next",
    "red flags at saida.tyre too": "red flags at saida.tyre too",
    "how many visits at tripoli.halba": "how many visits at tripoli.halba",
    # a page's words before a link with its scheme stay, on the first turn and when the question is sent again
    "The map hangs at https://neurodb.example.org/fmm/visits": "The map hangs at [link withheld]",
    "The page freezes at https://neurodb.example.org/fmm/visits": "The page freezes at [link withheld]",
    "What does the red flag mean at https://neurodb.example.org/fmm/visits/12": (
        "What does the red flag mean at [link withheld]"
    ),
    # "eg" then a mark ("eg, latrines": "e.g.," as it is typed), a measure or a word after "the" before "at", a
    # place, a full stop and "Net" or "Info": no address
    "Which issues were reported at schools. eg, latrines?": "Which issues were reported at schools. eg, latrines?",
    "Which issues were reported at schools. eg: latrines, water?": (
        "Which issues were reported at schools. eg: latrines, water?"
    ),
    "Which issues were reported at schools. eg. latrines?": "Which issues were reported at schools. eg. latrines?",
    "What was reported at schools. eg, latrines?": "What was reported at schools. eg, latrines?",
    "Any incidents at schools. eg, violence?": "Any incidents at schools. eg, violence?",
    "What was the GER at Halba. Net?": "What was the GER at Halba. Net?",
    "What was the NER at Tyre. Net?": "What was the NER at Tyre. Net?",
    "Show GER at Halba. Net, not gross": "Show GER at Halba. Net, not gross",
    "What was the GER at Halba. Net, gross?": "What was the GER at Halba. Net, gross?",
    "What were the findings at Halba. Info?": "What were the findings at Halba. Info?",
    "any findings at Halba. Info?": "any findings at Halba. Info?",
    "How many refugees at Saida. Info?": "How many refugees at Saida. Info?",
    "How many cases at Halba. Info?": "How many cases at Halba. Info?",
    "Which partners at Halba. Info?": "Which partners at Halba. Info?",
    "What was the Xyzzy at Halba. Net?": "What was the Xyzzy at Halba. Net?",
    # a field's code holding a digit before a place and a sentence
    "Monitoring visit to School2 at Akkar. NGO staff were present?": (
        "Monitoring visit to School2 at Akkar. NGO staff were present?"
    ),
    # a sentence missing its space after a place, its next word one a request goes on with
    "how many visits at akkar.show the total by district": "how many visits at akkar.show the total by district",
    "numbers at akkar.total by district": "numbers at akkar.total by district",
    "Compare Halba at akkar.show the total": "Compare Halba at akkar.show the total",
    "Compare Halba at akkar.how many visits?": "Compare Halba at akkar.how many visits?",
    "Compare Halba at akkar.who reported red flags?": "Compare Halba at akkar.who reported red flags?",
    "What was reported at halba.who is responsible?": "What was reported at halba.who is responsible?",
    "What happened at saida.how are the schools?": "What happened at saida.how are the schools?",
    "how many schools were visited at zahle.ok thanks": "how many schools were visited at zahle.ok thanks",
    "Compare Halba at akkar.show schools by district": "Compare Halba at akkar.show schools by district",
    # a sentence missing its space after a place, after a word that names no one
    "how many children at school.it was crowded": "how many children at school.it was crowded",
}
# what a person types that holds an address, and how it is sent: a word after the domain that follows an
# address ("before", "thanks"), the end of the text or a mark, or a word before the name that writes to one
# ("contact", "write to")
TYPED_ADDRESSES = {
    "contact zfakemira at example. net - thanks": "contact [email withheld] - thanks",
    "contact zfakemira at example. net (work)": "contact [email withheld] (work)",
    "contact zfakemira at example. net & thanks": "contact [email withheld] & thanks",
    "contact zfakemira at example. net about the data": "contact [email withheld] about the data",
    "contact zfakemira at example. info on Monday": "contact [email withheld] on Monday",
    "write to Zfakemira at hotmail. fr": "write to [email withheld]",
    "write to zfakemira at example. uk before June": "write to [email withheld] before June",
    "write to zfakemira at example. eg": "write to [email withheld]",
    "zfakemira at example. NGO": "[email withheld]",
    "contact Zfakemira at hotmail. fr please": "contact [email withheld] please",
    "contact zfakemira at example.health before June": "contact [email withheld] before June",
    "contact zfakemira at example.foundation before June": "contact [email withheld] before June",
    "email zfakemira at example.school": "email [email withheld]",
    "zfakemira at example.show before June": "[email withheld] before June",
    "Contact: MHADDAD AT example.it": "Contact: [email withheld]",
    "Write to ZFAKEMIRA AT example.it for the annex.": "Write to [email withheld] for the annex.",
    "mira-2 at example.it": "[email withheld]",
    "contact z.fakemira at hxxps://example.org": "contact [email withheld]",
    "contact zfakemira2 at hxxps://example.org": "contact [email withheld]",
    # a word that names no one before "at" and a domain of a country's ending, a new ending or a port: the
    # domain goes as a link (a role's address, "info at ...", is a link's or an address's: never sent)
    "Please write to info at zaynafakemira.online for the list": "Please write to info at [link withheld] for the list",
    "Can I send the forms to data at zaynafakemira.sy before June": (
        "Can I send the forms to data at [link withheld] before June"
    ),
    "Is support at zaynafakemira.tech the right contact": "Is support at [link withheld] the right contact",
    "write to info at zaynafakemira.it": "write to info at [link withheld]",
    "write to info at zaynafakemira.in": "write to info at [link withheld]",
    "write to info at zaynafakemira.es": "write to info at [link withheld]",
    "write to info at zaynafakemira.be": "write to info at [link withheld]",
    "write to info at zaynafakemira.tv": "write to info at [link withheld]",
    "contact info at zaynafakemira.online:8080": "contact info at [link withheld]:8080",
    "For more info at zaynafakemira.online what do the guides say": (
        "For more info at [link withheld] what do the guides say"
    ),
    "email me at zaynafakemira.site": "email me at [link withheld]",
    "more details at zaynafakemira.sy": "more details at [link withheld]",
    "the guide at zaynafakemira.blog please": "the guide at [link withheld] please",
    "I emailed them at zaynafakemira.it yesterday": "I emailed them at [link withheld] yesterday",
    "write to info at Zaynafakemira.Sy": "write to info at [link withheld]",
    # an ending that is also a word a request goes on with (".show", ".total"), then a verb, "please", "if"
    "zfakemira at example.show has the data": "[email withheld] has the data",
    "the address is zfakemira at example.show please": "the address is [email withheld] please",
    "zfakemira at example.show if needed": "[email withheld] if needed",
    "zfakemira at example.total has the data": "[email withheld] has the data",
    "mhaddad at example.show sent the file": "[email withheld] sent the file",
    "zfakemira at example.show is the focal point": "[email withheld] is the focal point",
    # a word a chat goes on with as the ending, after a word that writes to the name
    "write to zfakemira at example.ok thanks": "write to [email withheld] thanks",
    "contact zfakemira at example.ok": "contact [email withheld]",
    # an ending of a mixed case, or a name in capitals before a domain in capitals
    "zfakemira at Example.Sy has the data": "[email withheld] has the data",
    "zfakemira at Example.Sy": "[email withheld]",
    "Who is zfakemira at Example.Sy?": "Who is [email withheld]?",
    "Contact: ZFAKEMIRA at EXAMPLE.IT for the files.": "Contact: [email withheld] for the files.",
    # a dot and a space before more countries' endings, in lower case or capitalised
    "zfakemira at example. sa": "[email withheld]",
    "zfakemira at example. se": "[email withheld]",
    "zfakemira at example. ca": "[email withheld]",
    "zfakemira at example. it": "[email withheld]",
    "Her email is zfakemira at hotmail. Fr": "Her email is [email withheld]",
    "send it to zfakemira at hotmail. Fr please": "send it to [email withheld] please",
    "zfakemira@hotmail. Fr": "[email withheld]",
    "zfakemira@example. sa": "[email withheld]",
    "zfakemira@example. SA": "[email withheld]",
    "Draft an email to zfakemira@hotmail. Fr about Akkar enrolment": (
        "Draft an email to [email withheld] about Akkar enrolment"
    ),
    "The focal point is zfakemira at example. net Monday to Friday": (
        "The focal point is [email withheld] Monday to Friday"
    ),
    "zfakemira at example. info Monday": "[email withheld] Monday",
    # a link's scheme mistyped, cut short or defanged, after an address's name
    "contact zayna.fakemira at https:/example.org": "contact [email withheld]",
    "contact zayna.fakemira at http//example.org": "contact [email withheld]",
    "contact zayna.fakemira at //example.org": "contact [email withheld]",
    "contact zayna.fakemira at mailto:example.org": "contact [email withheld]",
    "contact zayna.fakemira at htps://example.org": "contact [email withheld]",
    "contact zayna.fakemira at https//example.org": "contact [email withheld]",
    "contact zfakemira2 at hxxps[:]//example[.]org": "contact [email withheld]",
}
# what a record's or a document's text holds that is an address, and how it is sent: a name holding a digit
# anywhere, letters then digits in capitals, "AT" in capitals before a word of two letters, an address's name
# before a link with its scheme
RECORD_ADDRESSES = {
    "For questions write to m2haddad at example.it.": "For questions write to [email withheld].",
    "Fake survey: 2zfakemira at example.me holds the files.": "Fake survey: [email withheld] holds the files.",
    "Fake survey: z2fakemira at example.no holds the files.": "Fake survey: [email withheld] holds the files.",
    "Write to mira-2 at example.it for the annex.": "Write to [email withheld] for the annex.",
    "Write to MHADDAD2 at example.it for the annex.": "Write to [email withheld] for the annex.",
    "Contact: MHADDAD AT example.it": "Contact: [email withheld]",
    "Write to ZFAKEMIRA AT example.it for the annex.": "Write to [email withheld] for the annex.",
    "Contact: MIRA AT EXAMPLE.IT": "Contact: [email withheld]",
    "Contact: MIRA2 AT example.be.": "Contact: [email withheld].",
    "For the data, write to zayna.fakemira at https://example.org before June.": (
        "For the data, write to [email withheld] before June."
    ),
    "Contact zfakemira2 at https://example.org/contact for the forms.": (
        "Contact [email withheld] for the forms."
    ),
    "Zayna.Fakemira at https://www.example.org/staff": "[email withheld]",
    "For the data, write to zayna.fakemira (at) https://example.org before June.": (
        "For the data, write to [email withheld] before June."
    ),
    "Fake notes: zfakemira @ example. info": "Fake notes: [email withheld]",
    "Fake notes: zayna.fakemira at example. info sessions": "Fake notes: [email withheld] sessions",
    # a name and "AT" in capitals before a domain in capitals, in a text of mixed case (no note written in
    # capitals): any word after it; an ending of three letters or more that is no word of a sentence
    "Fake survey: ZFAKEMIRA AT EXAMPLE.IT holds the files.": "Fake survey: [email withheld] holds the files.",
    "Focal point: ZFAKEMIRA AT EXAMPLE.IT for questions.": "Focal point: [email withheld] for questions.",
    "Fake survey: ZFAKEMIRA AT EXAMPLE.ONLINE holds the files.": "Fake survey: [email withheld] holds the files.",
    "Fake survey: write to ZFAKEMIRA AT EXAMPLE.ONLINE": "Fake survey: write to [email withheld]",
    "Data held by ZFAKEMIRA AT EXAMPLE.ONLINE for review.": "Data held by [email withheld] for review.",
    "Data held by ZFAKEMIRA AT EXAMPLE.SCHOOL for review.": "Data held by [email withheld] for review.",
    # a name in capitals then up to four digits, a dash and digits, or two capitals and a digit
    "Fake survey: write to ZFAKEMIRA1990 at example.it for the files.": (
        "Fake survey: write to [email withheld] for the files."
    ),
    "Fake survey: write to ZFAKEMIRA-2 at example.it for the files.": (
        "Fake survey: write to [email withheld] for the files."
    ),
    "Fake survey: write to ZM2 at example.it for the files.": "Fake survey: write to [email withheld] for the files.",
    # a name holding a digit, a dot and a space before an ending a sentence may start with, at the text's end
    "Fake survey: z2fakemira at example. info": "Fake survey: [email withheld]",
    "Fake survey: z2fakemira at example. info before June": "Fake survey: [email withheld] before June",
    # a link's scheme defanged or mistyped after an address's name
    "Fake notes: zfakemira2 at hxxps[:]//example[.]org": "Fake notes: [email withheld]",
    "Fake notes: zayna.fakemira at http//example.org for the files.": "Fake notes: [email withheld] for the files.",
    "Fake notes: zayna.fakemira at mailto:example.org": "Fake notes: [email withheld]",
}
# what a person types with an address's name before a link's scheme (a name's shape, or "at" written as no
# sentence writes it): no e-mail address, but its name goes as one would (what is sent and kept is the same on
# every turn)
SCHEMED = {
    "contact zayna.fakemira at https://example.org": "contact [email withheld]",
    "contact zfakemira2 at https://example.org/staff for the tables": (
        "contact [email withheld] for the tables"
    ),
    "contact zfakemira (at) https://example.org.": "contact [email withheld].",
}
# a plain word before a link with its scheme: never an e-mail address, the word stays and the link goes as a
# link (sent again, it is read as a question kept before the typed rule: "x at [link withheld]" loses "x")
SCHEMED_KEPT = {
    "contact zfakemira at https://example.org": "contact zfakemira at [link withheld]",
    "contact Zfakemira at https://example.org/staff for the tables": (
        "contact Zfakemira at [link withheld] for the tables"
    ),
}
# addresses joined by a slash (a list of focal points) or before a path: each goes whole
JOINED = (
    "Contacts: zfakemira at example dot org/zaynafake at example dot org",
    "Contacts: zfakemira -at- example -dot- org/ops",
    "Contacts: zfakemira (at) example (dot) org/ops",
    "Contacts: zfakemira [at] example [dot] org/ops",
    "Focal points: zfakemira at example.org/zaynafake at example.org.",
    "Email zfakemira at example.org/ops for details",
    "zfakemira at example dot org/",
    "Contact: zfakemira at www.example.org",
    "Write to zfakemira at example.org/staff/zaynafake for the data.",
)


# every character nobody sees, for reading what is left of an address (Unicode's format characters and the
# other invisible marks), whichever ones the rules know
_FORMAT = "".join(chr(c) for c in range(0x80, 0x30000) if unicodedata.category(chr(c)) == "Cf")
_INVISIBLE = re.compile(f"[{_FORMAT}\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0]")


def left(sent: str) -> str:
    """What of the address is left in ``sent``: "whole", "name", "domain", or "" when nothing is."""
    folded = unicodedata.normalize("NFKC", _INVISIBLE.sub("", sent)).casefold()
    name = "fakemira" in folded
    domain = "example" in folded
    if name and domain:
        return "whole"
    return "name" if name else "domain" if domain else ""


def leaks(sent: dict[str, str]) -> dict[str, str]:
    """``sent`` (where: the text sent) reduced to the texts that still hold part of an address."""
    return {where: f"{left(text)}: {text}" for where, text in sent.items() if left(text)}
