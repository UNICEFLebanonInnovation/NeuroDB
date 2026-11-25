"""The six questions an owner asked Ask the KM Database in one conversation, replayed on an invented KM
Database (places are public geography; every text, organisation and figure is invented) with the model
faked: what retrieval reads for each, and the line under each answer.

Q3, Q4 and Q5 are follow-ups: Q3 reworks Q2's answer with a lens on gender and disability (Q2's places,
years and cited findings carried), with a short table of its figures and the Excel of the findings it read
under it; Q4 asks the same for all of Lebanon and Q5 by governorate; Q6 is a new question of its own (it
names its subject, children)."""

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
    ("2025", ["Nationally, 8% of children with disabilities had no assistive devices."]),
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


def ask_history(row) -> list[dict]:
    """The earlier turns of ``row``'s conversation, as the stream reads them for it."""
    from neurodb.km import ask

    turns = ask.history(row.user, row.conversation, row.version)
    return [t for t in turns if t["question"] != row.question]


def ids(*pairs) -> set[str]:
    return {factory.finding_id(d, f) for d, f in pairs}


YEARS = (
    "Years 2024–2026, from your question; findings whose year is not reported are left out (the Year filter "
    "changes it)."
)
KEPT = (
    "Years 2024–2026, from your earlier question; findings whose year is not reported are left out (the Year "
    "filter changes it)."
)


# Q3's answer: Q2's paragraph reworked with a lens on gender and disability, a short table (one row gives a
# finding that was not read: it is taken out) and the download pointed to, never refused
Q3_ANSWER = (
    "In the South and Nabatieh, girls with disabilities had less access to school than other children "
    "[TST-0004-F00001]. In Nabatieh, 41% of children lacked safe water, with no figure by sex "
    "[TST-0002-F00002].\n\n"
    "| Indicator or measure | Figure as stated | Year | Finding ID |\n"
    "|---|---|---|---|\n"
    "| Girls with disabilities, access to school | less access | 2024 | [TST-0004-F00001] |\n"
    "| Children lacking safe water, Nabatieh | 41% | 2025 | [TST-0002-F00002] |\n"
    "| Boys with disabilities out of school | 12% | 2025 | [TST-0005-F00009] |\n\n"
    "The Excel of these findings, under this answer, lists each finding read with its figures."
)


def test_the_six_questions_of_one_conversation(client_viewer, ai, today):  # noqa: F811
    factory.load(build(DOCS))
    conversation = uuid.uuid4()
    fake = ai(
        [
            answer("In Mount Lebanon, 18,300 children were out of school [TST-0001-F00001].", "partly"),
            answer(
                "In Nabatieh, 41% lacked safe water [TST-0002-F00002]. In Nabatiye, 17% were displaced "
                "[TST-0002-F00003]. In the South, 46% of households said their children were anxious "
                "[TST-0002-F00005].",
                "partly",
            ),
            answer(Q3_ANSWER, "partly"),
            answer("Nationally, 8% had no assistive devices [TST-0009-F00001][TST-0004-F00003].", "partly"),
            answer("In Nabatieh, girls with disabilities had less access [TST-0004-F00001].", "partly"),
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
        "Answered from 5 matching findings. Only 1 finding on this subject names Mount Lebanon: findings from "
        "all of Lebanon were read too."
    )
    assert "Read from the question: only 1 finding on this subject names Mount Lebanon" in _sent(fake)

    # Q2: the request and time words are not searched; the South and Nabatiyeh are one place group (the
    # South, the South and Nabatieh governorates, their districts and towns); the past 2 years are 2024-2026,
    # without the years not reported; the identical texts are read once, both IDs citable. It names its own
    # subject (children): no follow-up of Q1
    final, row = ask(Q2)
    assert set(row.read) == ids((2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (4, 1), (4, 3), (7, 2))
    assert final["meta"] == (
        "Answered from 9 matching findings. Places from your question: the South (South and Nabatieh "
        f"governorates). {YEARS}"
    )
    text = _sent(fake)
    assert text.count(ANXIOUS) == 1 and 'also="TST-0002-F00006"' in text
    assert "This question was asked on 25 November 2026." in text

    # Q3: a rework of Q2 ("adjust the text above"): Q2's places and years kept and the findings its answer
    # cited read first, then its own words (the Excel clause is not searched): the findings on disability in
    # the South and Nabatieh, and as only 2 name them, the others of all Lebanon too; Q2's answer is sent whole
    final, row = ask(Q3)
    # (the gender lens reads the finding on girls and boys too)
    assert row.read == [
        *sorted(ids((2, 2), (2, 3), (2, 5))),
        *sorted(ids((4, 1), (4, 3))),
        "TST-0004-F00002",
        "TST-0009-F00001",
    ]
    assert final["meta"] == (
        "Answered from 7 matching findings. A follow-up of your earlier question; the findings its answer "
        "cited were read again. Only 2 findings on this subject name the South (South and Nabatieh "
        f"governorates): findings from all of Lebanon were read too. {KEPT}"
    )
    text = _sent(fake)
    assert "Earlier in this conversation:\nQ: " + Q1 in text
    assert "A: In Nabatieh, 41% lacked safe water [TST-0002-F00002]. In Nabatiye, 17% were displaced" in text
    assert "Read from the question: a follow-up of the previous question" in text
    rules = fake.calls()[-1]["instructions"]
    assert "rewrite it from the findings it cites that are in <findings>" in rules
    assert "you may add one short markdown table" in rules
    assert "an Excel and a CSV of the findings you read under every answer" in rules
    # the rework is shown with its table: the row that gives a finding not read is taken out, and said
    assert row.cited == ["TST-0004-F00001", "TST-0002-F00002"]
    html = final["html"]
    assert "<table>" in html and "<td>Children lacking safe water, Nabatieh</td>" in html
    assert "Boys with disabilities" not in html and "TST-0005-F00009" not in row.answer
    assert "1 row of the table was taken out: it gave no finding read for this answer." in final["notice"]
    # the Excel of the findings Q3 read: those its answer cites first, marked; the About sheet says which
    # question (as it was kept, cleaned) and when; never an author
    link = f"/km/ask/findings/{row.pk}/?export=xlsx"
    assert f'href="{link}"' in html and "Excel of these findings" in html
    response = client_viewer.get(link)
    assert response.status_code == 200
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    header, *rows = book["Findings"].iter_rows(values_only=True)
    assert [(r[0], r[1]) for r in rows] == [
        ("TST-0004-F00001", "Yes"),
        ("TST-0002-F00002", "Yes"),
        *[(i, "No") for i in row.read if i not in row.cited],
    ]
    about = dict(list(book["About"].iter_rows(values_only=True))[1:])
    assert about["Question"] == Q3 and about["Rows"] == 7 and about["Cited in the answer"] == 2
    assert "Asked on" in about and rows[0][header.index("publishing_organisations")] == "Fakeorg University"

    # Q4: no word of its own ("do it for all lebanon"): Q3's subject (disability, gender) and Q2's years, for
    # all of Lebanon, the findings Q3's answer cited read first; answered, never "no findings"
    final, row = ask(Q4)
    assert row.status == "answered" and row.model
    assert row.read == [
        "TST-0004-F00001",
        "TST-0004-F00002",
        "TST-0004-F00003",
        "TST-0002-F00002",
        "TST-0009-F00001",
    ]
    assert [f.finding_id for f in _picked(Q4, earlier=ask_history(row)).findings][:2] == [
        "TST-0004-F00001",
        "TST-0002-F00002",
    ]
    assert final["meta"] == (
        "Answered from 5 matching findings. Same subject as your earlier question; the findings its answer "
        "cited were read again. All of Lebanon, from your question: no place restriction. "
        f"{KEPT}"
    )

    # Q5: the same subject by governorate ("do it for all governorates"): the findings on disability that name
    # a governorate, in Q2's years; the national finding Q4's answer cited first
    final, row = ask(Q5)
    assert row.read == ["TST-0009-F00001", "TST-0004-F00003", "TST-0004-F00001"]
    assert final["meta"] == (
        "Answered from 3 matching findings. Same subject as your earlier question; the findings its answer "
        "cited were read again. By governorate, from your question: findings that name governorates first. "
        f"{KEPT}"
    )

    # Q6: "all gov" asks for a breakdown by governorate, in 2024-2026, without the years not reported (the
    # findings that name a governorate, or a district or town as its governorate: Saida and Tyre, Zahle); it
    # names its own subject (children), so it is a new question, not a follow-up of Q5
    final, row = ask(Q6)
    assert row.read[0] == "TST-0007-F00001"
    assert set(row.read) == ids(
        (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (4, 1), (4, 3), (7, 1), (7, 2), (7, 3)
    )
    assert final["meta"] == (
        "Answered from 11 matching findings. By governorate, from your question: findings that name "
        f"governorates first. {YEARS}"
    )
    assert len(fake.calls()) == 6


Q2_CITED = [factory.finding_id(2, 2), factory.finding_id(2, 3), factory.finding_id(2, 5)]
Q2_TURN = {"question": Q2, "answer": "Fake [TST-0002-F00002].", "cited": Q2_CITED}
SOUTH = "the South (South and Nabatieh governorates)"
REWORKS = (
    # with an angle of its own
    "revise the paragraph for girls",
    "please adjust the paragraph so it covers boys",
    "please revise the text to focus on boys",
    "revise the answer with a focus on disability",
    "can you adjust the paragraph with a lens on gender",
    "adjust the answer for girls",
    "focus on girls instead",
    "Thanks, what about girls?",
    # with no word of its own
    "rephrase the answer in simpler words",
    "rephrase for a donor audience",
    "summarise it in 3 bullet points",
    "can you make the answer shorter and simpler?",
    "can you simplify the answer?",
    "summarize the paragraph in bullet points",
    "make it shorter",
    "can you make it shorter?",
    "put it in a table please",
    "translate it to Arabic",
    "expand the summary",
    "shorten it",
)


@pytest.mark.parametrize("question", REWORKS)
def test_every_way_of_asking_to_rework_q2_keeps_its_places_years_and_findings(ai, today, question):  # noqa: F811
    factory.load(build(DOCS))
    picked = _picked(question, earlier=[Q2_TURN])
    assert picked.follow_up and picked.places == SOUTH
    assert "places" in picked.kept and "years" in picked.kept
    assert picked.filters.year == ["2025", "2024"]  # 2024-2026, of the years the version holds
    assert [f.finding_id for f in picked.findings][:3] == Q2_CITED


@pytest.mark.parametrize("question", ["rephrase the answer in simpler words", "can you make it shorter?"])
def test_a_rework_with_no_word_of_its_own_is_answered_from_q2(client_viewer, ai, today, question):  # noqa: F811
    factory.load(build(DOCS))
    conversation = uuid.uuid4()
    ai([answer("In Nabatieh, 41% lacked safe water [TST-0002-F00002]."), answer("Fake [TST-0002-F00002].")])
    done(post(client_viewer, Q2, conversation=conversation))
    final = done(post(client_viewer, question, conversation=conversation))
    row = KMQuestion.objects.order_by("-created_at").first()
    assert row.status == "answered" and row.read[0] == "TST-0002-F00002"
    assert "No finding" not in final["meta"]
    assert f"Places from your earlier question: {SOUTH}." in final["meta"] and KEPT in final["meta"]


def test_a_reworded_q3_keeps_q2s_years_for_q4(ai, today):  # noqa: F811
    factory.load(build(DOCS))
    rework = "can you adjust the paragraph with a lens on gender and disability"
    earlier = [
        Q2_TURN,
        {"question": rework, "answer": "Fake [TST-0004-F00001].", "cited": [factory.finding_id(4, 1)]},
    ]
    picked = _picked(Q4, earlier=earlier)
    assert picked.follow_up and picked.kept == ["subject", "years"] and picked.national
    assert picked.filters.year == ["2025", "2024"]


def test_q1_when_no_finding_names_mount_lebanon(client_viewer, ai, today):  # noqa: F811
    docs = [(year, list(texts)) for year, texts in DOCS]
    docs[0][1][0] = "In Beirut, 9,100 children aged 6-14 were out of school."
    factory.load(build(docs))
    ai([answer("Nationally, 23% were out of school [TST-0001-F00002].", "partly")])
    final = done(post(client_viewer, Q1))
    row = KMQuestion.objects.get()
    assert set(row.read) == ids((1, 1), (1, 2), (1, 4), (7, 2), (8, 1))
    assert final["meta"] == (
        "Answered from 5 matching findings. No finding on this subject names Mount Lebanon: findings from all "
        "of Lebanon were read."
    )


def test_q6_written_with_for_all_governorates_after_q5_is_a_new_question(ai, today):  # noqa: F811
    factory.load(build(DOCS))
    earlier = [
        {"question": Q2, "answer": "Fake [TST-0002-F00002].", "cited": [factory.finding_id(2, 2)]},
        {"question": Q3, "answer": "Fake [TST-0004-F00001].", "cited": [factory.finding_id(4, 1)]},
        {"question": Q4, "answer": "Fake [TST-0009-F00001].", "cited": [factory.finding_id(9, 1)]},
        {"question": Q5, "answer": "Fake [TST-0004-F00001].", "cited": [factory.finding_id(4, 1)]},
    ]
    rephrased = Q6.replace("in all gov", "for all governorates")
    picked = _picked(rephrased, earlier=earlier)
    assert not picked.follow_up and picked.kept == [] and picked.breakdown
    assert {f.finding_id for f in picked.findings} == {f.finding_id for f in _picked(Q6).findings}
    assert {f.finding_id for f in picked.findings} == {
        f.finding_id for f in _picked(Q6, earlier=earlier).findings
    }
