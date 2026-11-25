"""The six questions an owner asked Ask the KM Database in one conversation, replayed on an invented KM
Database (places are public geography; every text, organisation and figure is invented) with the model
faked: what retrieval reads for each, and the line under each answer.

Q1, Q2 and Q6 pin what retrieval does now. **STAGE 2** marks what the next stage changes (a follow-up that
reworks the previous answer, the Excel of the findings): those assertions pin today's behaviour of Q3, Q4
and Q5 and are to be updated then."""

from __future__ import annotations

import io
import uuid

import openpyxl
import pytest

from neurodb.km import columns as cols
from neurodb.km.models import KMQuestion
from tests.km import factory
from tests.km.test_ask import _picked, _sent, ai, answer, done, post, today  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db

Q1 = "number of children out of school in mont lebanon"
Q2 = (
    "i need a paragraph describing situation of children in south and nabatiyeh governorate in the past 2 "
    "years only"
)
Q3 = (
    "can you adjust the text above with a lence on gender, and what you can find by population type "
    "disability. once done i need other than the paragraph an excel sheet with each indicator and value in "
    "numbers plus in percentages"
)
Q4 = "can u do it for all lebanon?"
Q5 = "can you do it for all governorates?"
Q6 = "i need a paragraph describing situation of children in all gov in the past 2 years only"

PLAIN = {
    "Finding Analysis": "Plain analysis of the round.",
    "Limitations": "Plain limitations of the sample.",
    "Methods and Sample": "Plain household survey.",
    "Population": "Plain households.",
    "Measure": "Plain measure",
    "Publishing Entity / Author": "Fakeorg University",
    "Sub-question": "Plain sub-question",
    "Sub-sub-question": "Plain sub-sub-question",
    "Indicator / Evidence Requirement": "Plain indicator",
}
SAME = "17.8% of students considered self-harm during the past year, more among females than males."
ANXIOUS = "In the South Governorate, 46% of households said their children were anxious."
DOCS = [
    (
        "2023",
        [
            "In Mount Lebanon, 18,300 children aged 6-14 were out of school.",
            "23% of children aged 6-14 were out of school nationally.",
            "A number of school principals reported a shortage of teachers.",
            "Out-of-school rates rose among refugee children.",
        ],
    ),
    (
        "2025",
        [
            "In South and El Nabatieh governorates, 38% of children showed signs of distress.",
            "In Nabatieh, 41% of children lacked safe water.",
            "In Nabatiye, 17% of children were displaced.",
            "In Nabatiyeh, 12% of children were in child labour.",
            ANXIOUS,
            ANXIOUS,
        ],
    ),
    (
        "Not reported",
        [
            "Over the past two years, households need more support to buy food.",
            "Each household survey indicator value was reported as a number and as a percentage.",
            "Across all governorates, learning loss was reported in Grade 6.",
        ],
    ),
    (
        "2024",
        [
            "In Nabatiyeh, girls with disabilities had less access to school.",
            "Girls were less active than boys during recess.",
            "Children with disabilities in South Lebanon lacked transport to school.",
        ],
    ),
    ("2024", [SAME] * 5),
    (
        "2025",
        [
            {
                "Key Finding": "Teachers received a training course on literacy.",
                "Indicator / Evidence Requirement": "Number and percentage of out-of-school children by "
                "governorate",
            },
            {
                "Key Finding": "Clinics extended their opening hours.",
                "Sub-question": "What is the situation of children by governorate in the past years?",
            },
        ],
    ),
    (
        "2024",
        [
            "In Akkar, 31% of children were stunted, against 12% in Beirut and 20% in the North.",
            "In Saida and Tyre, 9% of children were out of school.",
            "In Zahle, 14% of children worked.",
        ],
    ),
    ("2019", ["In the South governorate in 2019, 30% of children were out of school."]),
]


def build(docs) -> bytes:
    """A workbook of ``docs`` ((year, [key finding or cells]) each), every other text plain."""
    wb = openpyxl.Workbook()
    first = wb.active
    first.title = factory.READ_ME_SHEET
    first.append(["Fake workbook title"])
    first.append([cols.PREPARED_ON, factory.PREPARED])
    first.append([])
    first.append(["Dataset", "Rows", "Note"])
    first.append([factory.FINDINGS_SHEET, sum(len(f) for _y, f in docs), "fake note"])
    first.append([factory.DOCUMENTS_SHEET, len(docs), "fake note"])
    documents, findings, number = [], [], 0
    for n, (year, texts) in enumerate(docs, 1):
        row = factory.document_row(n, len(texts))
        row["Publishing Entity / Authors"] = "Fakeorg University"
        row["Publication Year"] = year
        documents.append(row)
        for m, text in enumerate(texts, 1):
            number += 1
            cells = factory.finding_row(n, m, number) | PLAIN | {"Publication Year": year}
            cells |= {"Key Finding": text} if isinstance(text, str) else text
            findings.append(cells)
    for title, columns, rows in (
        (factory.DOCUMENTS_SHEET, cols.DOCUMENT_COLUMNS, documents),
        (factory.FINDINGS_SHEET, cols.FINDING_COLUMNS, findings),
    ):
        ws = wb.create_sheet(title)
        headers = [c.header for c in columns]
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h) for h in headers])
    wb.create_sheet(factory.DICTIONARY_SHEET).append(["Field", "Type", "Allowed values"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def ids(*pairs) -> set[str]:
    return {factory.finding_id(d, f) for d, f in pairs}


YEARS = (
    "Years 2024–2026, from your question; findings whose year is not reported are left out (the Year filter "
    "changes it)."
)


def test_the_six_questions_of_one_conversation(client_viewer, ai, today):  # noqa: F811
    factory.load(build(DOCS))
    conversation = uuid.uuid4()
    fake = ai(
        [
            answer("In Mount Lebanon, 18,300 children were out of school [TST-0001-F00001].", "partly"),
            answer("In Nabatieh, 41% lacked safe water [TST-0002-F00002].", "partly"),
            answer("Girls with disabilities had less access [TST-0004-F00001].", "partly"),
            answer("In Akkar, 31% were stunted [TST-0007-F00001].", "partly"),
            answer("In Akkar, 31% were stunted [TST-0007-F00001].", "partly"),
        ]
    )

    def ask(question):
        final = done(post(client_viewer, question, conversation=conversation))
        return final, KMQuestion.objects.order_by("-created_at").first()

    # Q1: out of school as a phrase (never every finding about school), Mount Lebanon in any spelling; the KM
    # Database has 1 such finding, so the other out-of-school findings are read too, figures first
    final, row = ask(Q1)
    assert row.read[0] == "TST-0001-F00001"
    assert set(row.read) == ids((1, 1), (1, 2), (1, 4), (7, 2), (8, 1))
    assert [f.finding_id for f in _picked(Q1).findings][-1] == "TST-0001-F00004"  # no figure: last
    assert final["meta"] == (
        "Answered from 5 matching findings. Only 1 finding names Mount Lebanon: findings from all of Lebanon "
        "were read too."
    )
    assert "Read from the question: only 1 finding names Mount Lebanon" in _sent(fake)

    # Q2: the request and time words are not searched; the South and Nabatiyeh are one place group (the
    # South, the South and Nabatieh governorates, their districts and towns); the past 2 years are 2024-2026,
    # without the years not reported; the identical texts are read once, both IDs citable
    final, row = ask(Q2)
    assert set(row.read) == ids((2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (4, 1), (4, 3), (7, 2))
    assert final["meta"] == (
        "Answered from 9 matching findings. Places from your question: the South (South and Nabatieh "
        f"governorates). {YEARS}"
    )
    text = _sent(fake)
    assert text.count(ANXIOUS) == 1 and 'also="TST-0002-F00006"' in text
    assert "This question was asked on 25 November 2026." in text

    # Q3 (STAGE 2: a rework of Q2 with a gender and disability lens, Q2's places, years and findings carried,
    # and the Excel of the findings): today a new question; the Excel clause is not searched (nor is it the
    # topic), and of its words only "disability" is in a finding
    final, row = ask(Q3)
    assert set(row.read) == ids((4, 1), (4, 3))
    assert _picked(Q3).groups == [["lence"], ["gender"], ["population"], ["disability"]]

    # Q4 (STAGE 2: Q3's topic for all of Lebanon): today no word is left to search
    final, row = ask(Q4)
    assert row.status == "no_match" and final["meta"] == "" and row.model == ""

    # Q5 (STAGE 2: the same topic by governorate): the findings that name governorates, the most first
    final, row = ask(Q5)
    assert row.read[0] == "TST-0007-F00001"  # Akkar, Beirut and the North
    assert not set(row.read) & ids((6, 1), (6, 2))  # "by governorate" only in their framework texts
    assert final["meta"].endswith(
        "By governorate, from your question: findings that name governorates first."
    )

    # Q6: "all gov" asks for a breakdown by governorate, in 2024-2026, without the years not reported
    final, row = ask(Q6)
    assert row.read[0] == "TST-0007-F00001"
    assert set(row.read) == ids((2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (4, 1), (4, 3), (7, 1))
    assert final["meta"] == (
        "Answered from 9 matching findings. By governorate, from your question: findings that name "
        f"governorates first. {YEARS}"
    )
    assert len(fake.calls()) == 5


def test_q1_when_no_finding_names_mount_lebanon(client_viewer, ai, today):  # noqa: F811
    docs = [(year, list(texts)) for year, texts in DOCS]
    docs[0][1][0] = "In Beirut, 9,100 children aged 6-14 were out of school."
    factory.load(build(docs))
    ai([answer("Nationally, 23% were out of school [TST-0001-F00002].", "partly")])
    final = done(post(client_viewer, Q1))
    row = KMQuestion.objects.get()
    assert set(row.read) == ids((1, 1), (1, 2), (1, 4), (7, 2), (8, 1))
    assert final["meta"] == (
        "Answered from 5 matching findings. No finding names Mount Lebanon: findings from all of Lebanon were "
        "read."
    )
