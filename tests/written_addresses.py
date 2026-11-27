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
}
# What a record's or a document's text keeps by design (the record rule): a plain word before "at" and a
# domain in capitals, of another ending or with a space after its dot ("published at Example.Org", "located
# at Tyre.Net", "located at Tyre. Net" stay), and look-alike or styled letters, read as the plain ones only in
# what a person types
RECORD_KEEPS = (
    *("F09", "F11", "F12", "F13", "F14", "F15", "F41", "F42", "F43", "F44", "F45"),
    *("F57", "F58", "F59", "F60", "F67", "F68"),
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
}
# what a person types with an address after a link's scheme: no e-mail address, but its name goes as one would
# (an earlier question kept so is read so too: what is sent and kept is the same on every turn)
SCHEMED = {
    "contact zfakemira at https://example.org": "contact [email withheld]",
    "contact zfakemira at https://example.org/staff for the tables": "contact [email withheld] for the tables",
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
