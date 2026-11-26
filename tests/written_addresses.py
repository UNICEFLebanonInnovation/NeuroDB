"""E-mail addresses written out in every form the AI privacy rules know (an invented name, example.org and
.invalid domains only), the sentences they are written in, and how what is left of one is read in a text
sent to the AI. Shared by the tests of each assistant (Monitoring insights, Help, Ask NeuroDB, NeuroDB
Watch, the daily review, What's new, the knowledge base, the KM Database)."""

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
}
# What a record's or a document's text keeps by design (the record rule): a plain word before "at" and a
# domain in capitals, of another ending or with a spaced dot ("published at Example.Org", "located at
# Tyre.Net" stay), and look-alike letters, read as the plain ones only in what a person types
RECORD_KEEPS = ("F09", "F11", "F12", "F13", "F14", "F15")
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
}


def left(sent: str) -> str:
    """What of the address is left in ``sent``: "whole", "name", "domain", or "" when nothing is."""
    folded = unicodedata.normalize("NFKC", sent.replace("\u200b", "")).casefold()
    name = "fakemira" in folded
    domain = "example" in folded
    if name and domain:
        return "whole"
    return "name" if name else "domain" if domain else ""


def leaks(sent: dict[str, str]) -> dict[str, str]:
    """``sent`` (where: the text sent) reduced to the texts that still hold part of an address."""
    return {where: f"{left(text)}: {text}" for where, text in sent.items() if left(text)}
