"""Identity, type and grouping of the eTools master indicators (neurodb.datamart.masters): what a link stores
for a PD indicator, which type it is (on a stored Row too), and the structure key NeuroDB proposes masters
from."""

from __future__ import annotations

import hashlib

import pytest

from neurodb.datamart import headline, masters, monitoring

from . import masters_world as w

OUTPUT = "1.1 Children access quality learning"
OTHER_OUTPUT = "2.3 Children are protected from violence"


# ------------------------------------------------------------------------------------------------ base title
@pytest.mark.parametrize(
    "title, base",
    [
        ("# of girls enrolled in NFE (6-14)", "children enrolled in nfe"),
        ("Number of boys enrolled in NFE", "children enrolled in nfe"),
        ("No. of girls and boys enrolled in NFE", "children enrolled in nfe"),
        (
            "% of Lebanese and Syrian girls and boys (10-17 years) attending school",
            "children attending school",
        ),
        ("Percentage of children attending school", "children attending school"),
        ("Proportion of children attending school", "children attending school"),
        ("# of Syrian children with disabilities enrolled", "children enrolled"),
        ("# of refugee children enrolled", "children enrolled"),
        ("# of women and men reached with awareness sessions", "adults reached with awareness sessions"),
        ("# of female and male caregivers trained", "caregivers trained"),
        ("# of young women trained", "young people trained"),
        ("", ""),
    ],
)
def test_the_base_title_drops_counting_words_gender_nationality_disability_and_age_ranges(title, base):
    assert masters.base_title(title) == base


def test_the_base_title_keeps_age_groups_apart():
    bases = {
        masters.base_title(f"# of {who} reached with PSS")
        for who in ("children", "adolescents", "youth", "caregivers", "girls", "boys")
    }
    assert bases == {
        "children reached with pss",
        "adolescents reached with pss",
        "youth reached with pss",
        "caregivers reached with pss",
    }


# -------------------------------------------------------------------------------------------------- identity
def test_the_identity_is_the_etools_id_else_a_hash_of_the_title_never_the_page_key(db):
    pd = w.programme("A", 1)
    w.indicator(pd, 101, "# of children enrolled")
    w.indicator(pd, None, "# of Children   enrolled in NFE")
    rows = {r.title: r for r in w.rows()}
    with_id, without = rows["# of children enrolled"], rows["# of Children   enrolled in NFE"]
    assert masters.ident_code(with_id.ident) == "s101" == with_id.ident[1]
    code = masters.ident_code(without.ident)
    assert without.key.startswith("r")  # the page's key: the Datamart row, rewritten by each sync
    assert code == "t" + hashlib.sha1(b"# of children enrolled in nfe").hexdigest() and len(code) == 41
    assert code != without.key and masters.key_of(without) == (pd.id, code)
    with pytest.raises(ValueError):
        masters.ident_code((pd.id, without.key))
    with pytest.raises(ValueError):
        masters.ident_code("101")


# ------------------------------------------------------------------------------------------------------ type
def test_the_type_is_read_from_a_stored_row_which_has_no_result(db):
    pd = w.programme("A", 1)
    w.indicator(pd, 1, "# of children enrolled")
    w.indicator(pd, 2, "% of children passing", display_type="percentage")
    w.indicator(pd, 3, "Ratio of girls to boys enrolled", display_type="ratio")
    w.indicator(pd, 4, "Days without a teacher", display_type="text")
    w.indicator(pd, 5, "# of children assessed")  # a number by its type, numerators over denominators by PRP
    w.report(
        pd,
        5,
        "# of children assessed",
        "QPR1",
        w.day(3, 31),
        {"Akkar": w.pct(3, 4)},
        calculation_across_periods="ratio",
    )
    rows = {int(r.key): r for r in w.rows()}
    assert not hasattr(rows[1], "result")
    with pytest.raises(AttributeError):
        headline.is_number(rows[1])  # why masters never use it on stored rows
    assert {key: masters.unit_class(row) for key, row in rows.items()} == {
        1: "number",
        2: "percentage",
        3: "ratio",
        4: "other",
        5: "ratio",
    }
    # an indicator read without its Reached (the job, the admin): by its display type and unit
    found = {
        int(e.key): masters.unit_class(e)
        for e in monitoring.collect(monitoring.Filters(year=w.YEAR)).values()
    }
    assert found == {1: "number", 2: "percentage", 3: "ratio", 4: "other", 5: "number"}


def test_the_types_each_method_takes():
    assert masters.misfit("sum", "total", "number") == ""
    assert "numbers only" in masters.misfit("sum", "total", "percentage")
    assert (
        masters.misfit("sum", "shown", "percentage") == "" and masters.misfit("sum", "shown", "other") == ""
    )
    assert "only in a percentage or a ratio master" in masters.misfit("sum", "numerator", "number")
    assert masters.misfit("percent", "total", "percentage") == ""
    assert masters.misfit("percent", "total", "ratio") and masters.misfit("ratio", "total", "percentage")
    assert masters.misfit("percent", "total", "number")
    assert (
        masters.misfit("percent", "numerator", "number")
        == masters.misfit("ratio", "denominator", "number")
        == ""
    )
    assert "must be a number" in masters.misfit("percent", "denominator", "percentage")
    for method in ("average", "maximum", "minimum"):
        assert all(masters.misfit(method, "total", unit) == "" for unit in ("number", "percentage", "ratio"))
        assert masters.misfit(method, "total", "other")


# --------------------------------------------------------------------------------------------- structure key
def test_the_structure_key_is_section_cp_output_type_and_base_title(db):
    a = w.programme("A", 1, cp_outputs=[OUTPUT, OTHER_OUTPUT])
    b = w.programme("B", 2, cp_outputs=[OUTPUT])  # one CP output: an indicator without its own takes it
    c = w.programme("C", 3, cp_outputs=[OUTPUT, OTHER_OUTPUT])
    w.indicator(a, 1, "# of girls enrolled in NFE", cp_output=OUTPUT)
    w.indicator(a, 2, "% of girls enrolled in NFE", display_type="percentage", cp_output=OUTPUT)
    w.indicator(a, 3, "# of girls enrolled in NFE", section=w.CP, cp_output=OUTPUT)
    w.indicator(b, 4, "Number of boys enrolled in NFE")
    w.indicator(c, 5, "# of children enrolled in NFE")  # two CP outputs, none its own: not known
    w.indicator(c, 6, "# of children enrolled in NFE", section="")
    w.indicator(c, 7, "Days of NFE", display_type="text", cp_output=OUTPUT)
    found = {int(e.key): e for e in monitoring.collect(monitoring.Filters(year=w.YEAR)).values()}
    keys = {k: masters.structure_key(e) for k, e in found.items()}
    assert keys[1] == keys[4]  # girls and boys, its own CP output and the PD's only one
    assert keys[1] != keys[2]  # a number and a percentage are never one family
    assert len(keys[1]) == 40 and keys[1] != masters.structure_key(found[3])  # nor two sections
    assert (
        masters.cp_output_key(found[4])
        == masters.cp_output_key(found[1])
        == "1.1 children access quality learning"
    )
    # the PD's outputs joined (what partner monitoring shows until a report names one) are never a key
    assert found[5].cp_output == f"{OUTPUT}, {OTHER_OUTPUT}"
    assert masters.cp_output_key(found[5]) == "?"
    assert keys[6] == keys[7] == ""  # no section, or a type that never counts: never proposed
    # a stored row carries no PD outputs: they are passed
    rows = {int(r.key): r for r in w.rows()}
    assert masters.structure_key(rows[4], [OUTPUT]) == keys[4]
    assert masters.cp_output_key(rows[5], [OUTPUT, OTHER_OUTPUT]) == "?"


def test_an_unknown_cp_output_joins_the_one_family_of_its_title(db):
    a = w.programme("A", 1, cp_outputs=[OUTPUT])
    b = w.programme("B", 2, cp_outputs=[OUTPUT, OTHER_OUTPUT])
    c = w.programme("C", 3, cp_outputs=[OUTPUT, OTHER_OUTPUT])
    w.indicator(a, 1, "# of children enrolled in NFE")
    w.indicator(b, 2, "# of girls enrolled in NFE")  # CP output not known yet
    w.indicator(a, 3, "# of children reached with PSS", cp_output=OUTPUT)
    w.indicator(b, 4, "# of children reached with PSS", cp_output=OTHER_OUTPUT)
    w.indicator(c, 5, "# of boys reached with PSS")  # two families of its title: ambiguous
    found = list(monitoring.collect(monitoring.Filters(year=w.YEAR)).values())
    groups = {tuple(sorted(int(e.key) for e in members)) for members in masters.clusters(found).values()}
    assert groups == {(1, 2), (3,), (4,), (5,)}
    # stored rows: the PDs' outputs passed by PD
    rows = w.rows()
    outputs = {a.id: [OUTPUT], b.id: [OUTPUT, OTHER_OUTPUT], c.id: [OUTPUT, OTHER_OUTPUT]}
    stored = {
        tuple(sorted(int(r.key) for r in members)) for members in masters.clusters(rows, outputs).values()
    }
    assert stored == groups
