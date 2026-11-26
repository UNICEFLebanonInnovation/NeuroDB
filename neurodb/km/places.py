"""The places of Lebanon a question to Ask the KM Database names (:func:`read`), written in code: public
geography (every governorate, its districts and main towns, each town in its district, with the spellings
reports use, the French names and the Arabic script), never the workbook's text and never another NeuroDB
table.

- **A place named** (any spelling, its accents or none: "Békaa", "Zahlé"; "the", "El", "Al" or a French
  article, "la", "le", "du", "de la", "au", "l'", before it; in Arabic script with or without the letters
  joined before it: "البقاع", "بالبقاع", "والشمال") is taken out of the words searched; all the places of one
  question make **one** place group (:attr:`Reading.alternatives`): a finding qualifies when its own words
  name any of them. A governorate also finds its districts and towns ("South" finds "Saida", "Tyre",
  "Jezzine"); Mount Lebanon also finds Keserwan-Jbeil, as older reports count it in Mount Lebanon. A district
  also finds its towns and camps (Zahle: Bar Elias, Tyre: Qana and Rashidieh); a town or a camp finds its own
  spellings ("Tripoli (the North)"). Arabic spellings only read a question (the findings are in English). A
  spelling inside another governorate's place is not its place where it is written inside it ("Jbeil" in
  "Bint Jbeil", "North" in "North Metn" or "North Bekaa", "South" in "South Metn" or "South Bekaa":
  :func:`shadows`), but a text that also writes it on its own names it ("the Bekaa and North Bekaa":
  :func:`standalone`), and the other place's findings still count for that other place in the same group
  ("Mount Lebanon and Nabatieh", a breakdown). Only a space or one hyphen or dash joins the words of a place's
  name (:data:`JOIN`): "the South, Bekaa and Akkar", "the South. Bekaa ..." and "the North, Bekaa and
  Akkar" name each place, never South Bekaa or North Bekaa.
- **The regions (the owner's rule):** "South Lebanon", "the south", "Liban-Sud", "Sud-Liban", "the southern
  governorates", "the southern region" or "areas" mean the South and Nabatieh governorates; "the north",
  "North Lebanon", "northern Lebanon", "Liban-Nord", "Shamal", "the northern governorates" or "region" mean
  the North and Akkar; "South governorate", one "southern governorate", "North governorate" and one "northern
  governorate" that governorate only. A region's part of a place is that place ("the southern region of the
  Bekaa": a place's name after "of", "in" or "within"); any other word after it keeps the region ("the
  southern governorates in light of the escalation"). "South Metn" is Baabda (Mount Lebanon), "North Bekaa"
  Baalbek-Hermel, "South Bekaa" the Bekaa.
- **Governorate words** ("governorate", "gov", "mohafaza", "محافظة") and **district words** ("district",
  "caza", "قضاء") are part of the place when a place is named, and otherwise ask for a breakdown
  (:attr:`Reading.breakdown`: "all governorates", "by governorate", "all gov", "per governorate", "across
  governorates", "across Lebanon's governorates", "in all eight governorates", "broken down by governorate",
  "at governorate level"; "by district", "each caza", "the districts", "by governorate and district":
  :attr:`Reading.by_district`), the words that ask for it taken out with them. A breakdown keeps the
  findings that name a place: a governorate, or a district or town as its governorate.
- **"All Lebanon", "nationally", "nationwide", "national level", "Lebanon-wide", "across the country", "at
  country level", "throughout the country", "tout le Liban", "au niveau national", "كل لبنان", "على المستوى
  الوطني"** ask for no place (:attr:`Reading.national`). The word "national" alone does so when the question
  names a place ("the national rate and Akkar": every place, the findings naming Akkar first); without one it
  is a word of the subject ("the national education strategy"), read as all of Lebanon only when nothing else
  is left to search (:attr:`Reading.national_word`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache


@dataclass(frozen=True)
class Place:
    key: str
    name: str  # as the line under an answer names it
    spellings: tuple[str, ...]  # lower case, as questions and texts write it
    governorates: tuple[str, ...] = ()  # a district or a town: the governorate(s) it is in
    includes: tuple[str, ...] = ()  # a governorate: other governorates its findings count (older reports)
    camp: bool = False  # a camp: "Shatila camp" is the place, "camp" no word to search
    district: str = (
        ""  # a town: the district (caza) it is in (a district's key, or a governorate's that is one)
    )
    arabic: tuple[str, ...] = ()  # in Arabic script, as a question writes it (never looked for in a finding)


def _town(
    key: str, name: str, spellings: tuple[str, ...], district: str, governorates: tuple[str, ...], *arabic
):
    return Place(key, name, spellings, governorates, district=district, arabic=arabic)


ML = ("mount-lebanon",)
KJ = ("keserwan-jbeil", "mount-lebanon")
GOVERNORATES: tuple[Place, ...] = (
    Place("akkar", "Akkar", ("akkar", "akar", "aakar", "aakkar", "akkar governorate"), arabic=("عكار",)),
    Place(
        "baalbek-hermel",
        "Baalbek-Hermel",
        (
            "baalbek-hermel",
            "baalbeck-hermel",
            "baalbak-hermel",
            "baalbek hermel",
            "baalbeck hermel",
            "baalbek el hermel",
            "baalbek-el hermel",
            "north bekaa",
            "northern bekaa",
            "north beqaa",
        ),
        arabic=("بعلبك الهرمل", "بعلبك-الهرمل", "بعلبك والهرمل", "البقاع الشمالي"),
    ),
    Place(
        "beirut",
        "Beirut",
        ("beirut", "beyrouth", "bayrut", "beyrut", "bairut", "beirut governorate"),
        arabic=("بيروت",),
    ),
    Place(
        "bekaa",
        "Bekaa",
        (
            "bekaa",
            "beqaa",
            "bekka",
            "beka'a",
            "beqa'a",
            "biqa",
            "bekaa valley",
            "south bekaa",  # West Bekaa and Rashaya: never the South
            "southern bekaa",
            "south beqaa",
        ),
        arabic=("البقاع", "سهل البقاع", "البقاع الجنوبي"),
    ),
    Place(
        "keserwan-jbeil",
        "Keserwan-Jbeil",
        (
            "keserwan-jbeil",
            "kesrouan-jbeil",
            "kesrwan-jbeil",
            "keserwan jbeil",
            "kesrouan jbeil",
            "keserwan-byblos",
        ),
        arabic=("كسروان جبيل", "كسروان-جبيل", "كسروان وجبيل"),
    ),
    Place(
        "mount-lebanon",
        "Mount Lebanon",
        (
            "mount lebanon",
            "mont lebanon",
            "mont liban",
            "mont-liban",
            "mount-lebanon",
            "mt lebanon",
            "mt. lebanon",
            "jabal lubnan",
            "jabal loubnan",
        ),
        includes=("keserwan-jbeil",),
        arabic=("جبل لبنان",),
    ),
    Place(
        "nabatieh",
        "Nabatieh",
        (
            "nabatieh",
            "nabatiyeh",
            "nabatiye",
            "nabatiyah",
            "nabatiya",
            "nabatiyé",
            "nabatie",
            "nabatiyeh governorate",
            "nabatieh governorate",
        ),
        arabic=("النبطية",),
    ),
    Place(
        "north",
        "North",
        (
            "north governorate",
            "north lebanon",
            "northern lebanon",
            "northern governorate",
            "north",
            "liban-nord",
            "liban nord",
            "nord-liban",
            "nord liban",
            "shamal",
            "chamal",
        ),
        arabic=("محافظة الشمال", "محافظة لبنان الشمالي"),
    ),
    Place(
        "south",
        "South",
        (
            "south governorate",
            "southern governorate",
            "south lebanon",
            "southern lebanon",
            "southern border",
            "southern borders",
            "south",
            "liban-sud",
            "liban sud",
        ),
        arabic=("محافظة الجنوب", "محافظة لبنان الجنوبي"),
    ),
)
# the districts (cazas), main towns and camps, each with its governorate(s) and, for a town, its district: the
# official cazas, public geography (a governorate that is one caza, Akkar, Beirut and Nabatieh's own, is the
# district of its towns). A town whose Arabic name is also a common word has none ("الخيام" is "the tents",
# "الميناء" "the port", "الحدث" "the event", "القاع" "the bottom")
DISTRICTS: tuple[Place, ...] = (
    # Akkar
    _town("halba", "Halba", ("halba",), "akkar", ("akkar",), "حلبا"),
    _town(
        "qobayat",
        "Qobayat",
        ("qobayat", "qoubaiyat", "kobayat", "qoubayat", "qbayat"),
        "akkar",
        ("akkar",),
        "القبيات",
    ),
    _town("wadi-khaled", "Wadi Khaled", ("wadi khaled",), "akkar", ("akkar",), "وادي خالد"),
    _town("bebnine", "Bebnine", ("bebnine", "bibnin", "bebnin"), "akkar", ("akkar",), "ببنين"),
    _town("minyara", "Minyara", ("minyara", "minyarah"), "akkar", ("akkar",), "منيارة"),
    _town("qlayaat", "Qlayaat", ("qlayaat", "qlayat", "kleiat", "qleiaat"), "akkar", ("akkar",), "القليعات"),
    _town("berqayel", "Berqayel", ("berqayel", "berkayel"), "akkar", ("akkar",), "برقايل"),
    _town("fneideq", "Fneideq", ("fneideq", "fnaydeq"), "akkar", ("akkar",), "فنيدق"),
    _town("tal-abbas", "Tal Abbas", ("tal abbas", "tell abbas"), "akkar", ("akkar",), "تل عباس"),
    _town("mhammara", "Mhammara", ("mhammara", "mhamra"), "akkar", ("akkar",), "المحمرة"),
    # Baalbek-Hermel
    Place(
        "baalbek",
        "Baalbek",
        ("baalbek", "baalbeck", "baalbak", "ba'albek", "baalbeq"),
        ("baalbek-hermel",),
        arabic=("بعلبك",),
    ),
    Place("hermel", "Hermel", ("hermel", "hirmil"), ("baalbek-hermel",), arabic=("الهرمل",)),
    _town("arsal", "Arsal", ("arsal", "aarsal", "ersal"), "baalbek", ("baalbek-hermel",), "عرسال"),
    _town("labweh", "Labweh", ("labweh", "labwe", "laboueh"), "baalbek", ("baalbek-hermel",), "اللبوة"),
    _town("qaa", "Al-Qaa", ("qaa",), "baalbek", ("baalbek-hermel",)),
    _town("ras-baalbek", "Ras Baalbek", ("ras baalbek",), "baalbek", ("baalbek-hermel",), "رأس بعلبك"),
    _town("douris", "Douris", ("douris", "dours"), "baalbek", ("baalbek-hermel",), "دورس"),
    _town("britel", "Britel", ("britel", "brital"), "baalbek", ("baalbek-hermel",), "بريتال"),
    _town("chmestar", "Chmestar", ("chmestar", "shmustar"), "baalbek", ("baalbek-hermel",), "شمسطار"),
    _town("bednayel", "Bednayel", ("bednayel",), "baalbek", ("baalbek-hermel",), "بدنايل"),
    _town(
        "deir-el-ahmar",
        "Deir el Ahmar",
        ("deir el ahmar", "deir al ahmar", "deir el-ahmar"),
        "baalbek",
        ("baalbek-hermel",),
        "دير الأحمر",
    ),
    _town("younine", "Younine", ("younine", "yunin"), "baalbek", ("baalbek-hermel",), "يونين"),
    # Bekaa
    Place("zahle", "Zahle", ("zahle", "zahleh", "zahlé", "zahlah"), ("bekaa",), arabic=("زحلة",)),
    Place(
        "west-bekaa",
        "West Bekaa",
        ("west bekaa", "western bekaa", "west beqaa"),
        ("bekaa",),
        arabic=("البقاع الغربي",),
    ),
    Place(
        "rashaya",
        "Rashaya",
        ("rashaya", "rachaya", "rashayya", "rachaiya"),
        ("bekaa",),
        arabic=("راشيا", "راشيا الوادي"),
    ),
    _town("chtaura", "Chtaura", ("chtaura", "chtoura", "shtaura"), "zahle", ("bekaa",), "شتورة"),
    _town(
        "bar-elias",
        "Bar Elias",
        ("bar elias", "bar elyas", "barr elias"),
        "zahle",
        ("bekaa",),
        "بر الياس",
        "برالياس",
    ),
    _town("saadnayel", "Saadnayel", ("saadnayel", "saad nayel"), "zahle", ("bekaa",), "سعدنايل"),
    _town(
        "qab-elias",
        "Qab Elias",
        ("qab elias", "qabb elias", "kab elias", "qab elyas"),
        "zahle",
        ("bekaa",),
        "قب الياس",
    ),
    _town("majdal-anjar", "Majdal Anjar", ("majdal anjar", "majdel anjar"), "zahle", ("bekaa",), "مجدل عنجر"),
    _town("anjar", "Anjar", ("anjar",), "zahle", ("bekaa",), "عنجر"),
    _town("taanayel", "Taanayel", ("taanayel", "taanail"), "zahle", ("bekaa",), "تعنايل"),
    _town("ferzol", "Ferzol", ("ferzol", "fourzol"), "zahle", ("bekaa",), "فرزل"),
    _town("rayak", "Rayak", ("rayak", "riyaq"), "zahle", ("bekaa",), "رياق"),
    _town(
        "joub-jannine",
        "Joub Jannine",
        ("joub jannine", "jub jannin", "joub jenin"),
        "west-bekaa",
        ("bekaa",),
        "جب جنين",
    ),
    _town("qaraoun", "Qaraoun", ("qaraoun", "karaoun"), "west-bekaa", ("bekaa",), "القرعون"),
    _town("saghbine", "Saghbine", ("saghbine",), "west-bekaa", ("bekaa",), "صغبين"),
    _town(
        "kamed-el-loz",
        "Kamed el Loz",
        ("kamed el loz", "kamed al lawz"),
        "west-bekaa",
        ("bekaa",),
        "كامد اللوز",
    ),
    _town("sohmor", "Sohmor", ("sohmor", "suhmur"), "west-bekaa", ("bekaa",), "سحمر"),
    _town(
        "dahr-el-ahmar",
        "Dahr el Ahmar",
        ("dahr el ahmar", "dahr al ahmar"),
        "rashaya",
        ("bekaa",),
        "ضهر الأحمر",
    ),
    # Beirut
    _town(
        "achrafieh",
        "Achrafieh",
        ("achrafieh", "ashrafieh", "ashrafiyeh", "achrafiyeh"),
        "beirut",
        ("beirut",),
        "الأشرفية",
    ),
    _town("karantina", "Karantina", ("karantina", "quarantina"), "beirut", ("beirut",), "الكرنتينا"),
    _town(
        "tariq-el-jdideh",
        "Tariq el Jdideh",
        (
            "tariq el jdideh",
            "tariq al jdideh",
            "tarik el jdide",
            "tarik el jdideh",
            "tariq el jdeideh",
            "tariq al jadida",
        ),
        "beirut",
        ("beirut",),
        "الطريق الجديدة",
    ),
    _town("ras-beirut", "Ras Beirut", ("ras beirut",), "beirut", ("beirut",), "رأس بيروت"),
    _town("bachoura", "Bachoura", ("bachoura", "bashoura"), "beirut", ("beirut",), "الباشورة"),
    _town("gemmayzeh", "Gemmayzeh", ("gemmayzeh", "gemmayze"), "beirut", ("beirut",), "الجميزة"),
    _town("msaitbeh", "Msaitbeh", ("msaitbeh", "msaitbe", "moussaitbeh"), "beirut", ("beirut",), "المصيطبة"),
    _town(
        "zuqaq-el-blat",
        "Zuqaq el Blat",
        ("zuqaq el blat", "zokak el blat"),
        "beirut",
        ("beirut",),
        "زقاق البلاط",
    ),
    _town("mar-mikhael", "Mar Mikhael", ("mar mikhael",), "beirut", ("beirut",), "مار مخايل"),
    # Mount Lebanon
    Place(
        "keserwan",
        "Keserwan",
        ("keserwan", "kesrouan", "kesrwan", "kisrawan", "keserouan"),
        KJ,
        arabic=("كسروان",),
    ),
    Place("jbeil", "Jbeil", ("jbeil", "jbail", "jubail", "byblos"), KJ, arabic=("جبيل",)),
    _town("jounieh", "Jounieh", ("jounieh", "junieh", "jouniyeh"), "keserwan", KJ, "جونية"),
    _town("zouk-mosbeh", "Zouk Mosbeh", ("zouk mosbeh", "zouk mousbeh"), "keserwan", KJ, "ذوق مصبح"),
    _town("zouk-mikael", "Zouk Mikael", ("zouk mikael", "zouk mekayel"), "keserwan", KJ, "ذوق مكايل"),
    _town("ghazir", "Ghazir", ("ghazir",), "keserwan", KJ),
    _town("faraya", "Faraya", ("faraya",), "keserwan", KJ, "فاريا"),
    _town("ajaltoun", "Ajaltoun", ("ajaltoun",), "keserwan", KJ, "عجلتون"),
    _town("tabarja", "Tabarja", ("tabarja",), "keserwan", KJ, "طبرجا"),
    _town("amchit", "Amchit", ("amchit", "amsheet"), "jbeil", KJ, "عمشيت"),
    _town("qartaba", "Qartaba", ("qartaba", "kartaba"), "jbeil", KJ, "قرطبا"),
    Place("aley", "Aley", ("aley", "aaley", "alay"), ML, arabic=("عاليه",)),
    Place("chouf", "Chouf", ("chouf", "shouf", "chuf", "shuf"), ML, arabic=("الشوف",)),
    Place("baabda", "Baabda", ("baabda", "ba'abda", "south metn", "southern metn"), ML, arabic=("بعبدا",)),
    Place("metn", "Metn", ("metn", "matn", "maten", "north metn", "el metn"), ML, arabic=("المتن",)),
    _town("bourj-hammoud", "Bourj Hammoud", ("bourj hammoud", "burj hammoud"), "metn", ML, "برج حمود"),
    _town("dekwaneh", "Dekwaneh", ("dekwaneh", "dekwane", "dikwaneh"), "metn", ML, "الدكوانة"),
    _town("sin-el-fil", "Sin el Fil", ("sin el fil", "sin el-fil"), "metn", ML, "سن الفيل"),
    _town("antelias", "Antelias", ("antelias", "antilyas"), "metn", ML, "أنطلياس"),
    _town("zalka", "Zalka", ("zalka", "zalqa"), "metn", ML, "الزلقا"),
    _town("broumana", "Broumana", ("broumana", "brummana"), "metn", ML, "برمانا"),
    _town("bikfaya", "Bikfaya", ("bikfaya", "bekfaya"), "metn", ML, "بكفيا"),
    _town("beit-mery", "Beit Mery", ("beit mery", "beit meri"), "metn", ML, "بيت مري"),
    _town("ouzai", "Ouzai", ("ouzai", "awzai", "uzai"), "baabda", ML, "الأوزاعي"),
    _town("ghobeiry", "Ghobeiry", ("ghobeiry", "ghobeiri", "ghbeiry"), "baabda", ML, "الغبيري"),
    _town("haret-hreik", "Haret Hreik", ("haret hreik", "haret hreyk"), "baabda", ML, "حارة حريك"),
    _town("chiyah", "Chiyah", ("chiyah", "shiyah", "chiyeh"), "baabda", ML, "الشياح"),
    _town("hadath", "Hadath", ("hadath",), "baabda", ML),
    _town("hazmieh", "Hazmieh", ("hazmieh", "hazmiyeh"), "baabda", ML, "الحازمية"),
    _town("dahiyeh", "Dahiyeh", ("dahiyeh", "dahieh", "dahiya", "dahiye"), "baabda", ML, "الضاحية"),
    _town(
        "furn-el-chebbak",
        "Furn el Chebbak",
        ("furn el chebbak", "furn el shebbak"),
        "baabda",
        ML,
        "فرن الشباك",
    ),
    _town(
        "ain-el-remmaneh",
        "Ain el Remmaneh",
        ("ain el remmaneh", "ain al rummaneh"),
        "baabda",
        ML,
        "عين الرمانة",
    ),
    _town("kfarshima", "Kfarshima", ("kfarshima", "kfarchima"), "baabda", ML, "كفرشيما"),
    _town("damour", "Damour", ("damour",), "chouf", ML, "الدامور"),
    _town("barja", "Barja", ("barja",), "chouf", ML, "برجا"),
    _town("chhim", "Chhim", ("chhim", "shhim", "chehim"), "chouf", ML, "شحيم"),
    _town(
        "beiteddine", "Beiteddine", ("beiteddine", "beit ed-dine", "beit eddine"), "chouf", ML, "بيت الدين"
    ),
    _town(
        "deir-el-qamar",
        "Deir el Qamar",
        ("deir el qamar", "deir al qamar", "deir el kamar"),
        "chouf",
        ML,
        "دير القمر",
    ),
    _town("jiyeh", "Jiyeh", ("jiyeh", "jieh"), "chouf", ML, "الجية"),
    _town("naameh", "Naameh", ("naameh", "naame"), "chouf", ML, "الناعمة"),
    _town("ketermaya", "Ketermaya", ("ketermaya", "katermaya"), "chouf", ML, "كترمايا"),
    _town("baakline", "Baakline", ("baakline", "baaqlin"), "chouf", ML, "بعقلين"),
    _town(
        "iqlim-al-kharroub",
        "Iqlim al-Kharroub",
        ("iqlim al-kharroub", "iqlim al kharroub", "iqlim el kharroub", "kharroub"),
        "chouf",
        ML,
        "إقليم الخروب",
    ),
    _town(
        "choueifat", "Choueifat", ("choueifat", "chouaifat", "shweifat", "shwayfat"), "aley", ML, "الشويفات"
    ),
    _town("aramoun", "Aramoun", ("aramoun", "aramun"), "aley", ML, "عرمون"),
    _town("bchamoun", "Bchamoun", ("bchamoun", "bshamoun"), "aley", ML, "بشامون"),
    _town("khaldeh", "Khaldeh", ("khaldeh", "khalde"), "aley", ML, "خلدة"),
    _town("bhamdoun", "Bhamdoun", ("bhamdoun", "bhamdun"), "aley", ML, "بحمدون"),
    _town("souk-el-gharb", "Souk el Gharb", ("souk el gharb", "souq al gharb"), "aley", ML, "سوق الغرب"),
    # Nabatieh
    Place(
        "bint-jbeil",
        "Bint Jbeil",
        ("bint jbeil", "bint jbail", "bent jbeil", "bint jubayl", "bint-jbeil"),
        ("nabatieh",),
        arabic=("بنت جبيل",),
    ),
    Place(
        "marjayoun",
        "Marjayoun",
        ("marjayoun", "marjeyoun", "marjayun", "marjaayoun"),
        ("nabatieh",),
        arabic=("مرجعيون",),
    ),
    Place(
        "hasbaya",
        "Hasbaya",
        ("hasbaya", "hasbaiya", "hasbayya", "hasbeya"),
        ("nabatieh",),
        arabic=("حاصبيا",),
    ),
    _town("khiam", "Khiam", ("khiam",), "marjayoun", ("nabatieh",)),
    _town(
        "kfar-kila", "Kfar Kila", ("kfar kila", "kfarkila", "kfar kela"), "marjayoun", ("nabatieh",), "كفركلا"
    ),
    _town(
        "odaisseh", "Odaisseh", ("odaisseh", "adaisseh", "odaysseh"), "marjayoun", ("nabatieh",), "العديسة"
    ),
    _town("houla", "Houla", ("houla",), "marjayoun", ("nabatieh",), "حولا"),
    _town(
        "mays-al-jabal",
        "Mays al-Jabal",
        ("mays al-jabal", "mays al jabal", "mais al jabal", "meiss el jabal"),
        "marjayoun",
        ("nabatieh",),
        "ميس الجبل",
    ),
    _town("deir-mimas", "Deir Mimas", ("deir mimas",), "marjayoun", ("nabatieh",), "دير ميماس"),
    _town(
        "aitaroun", "Aitaroun", ("aitaroun", "aytaroun", "eitaroun"), "bint-jbeil", ("nabatieh",), "عيترون"
    ),
    _town("rmeish", "Rmeish", ("rmeish", "rmaich", "rmeich"), "bint-jbeil", ("nabatieh",), "رميش"),
    _town("ain-ebel", "Ain Ebel", ("ain ebel", "ain ibl"), "bint-jbeil", ("nabatieh",), "عين إبل"),
    _town("tebnine", "Tebnine", ("tebnine", "tibnin"), "bint-jbeil", ("nabatieh",), "تبنين"),
    _town(
        "aita-al-shaab",
        "Aita al-Shaab",
        ("aita al-shaab", "aita al shaab", "aita el shaab", "ayta ash shab"),
        "bint-jbeil",
        ("nabatieh",),
        "عيتا الشعب",
    ),
    _town(
        "maroun-al-ras",
        "Maroun al-Ras",
        ("maroun al-ras", "maroun al ras", "maroun el ras"),
        "bint-jbeil",
        ("nabatieh",),
        "مارون الراس",
    ),
    _town("yaroun", "Yaroun", ("yaroun",), "bint-jbeil", ("nabatieh",), "يارون"),
    _town("shebaa", "Shebaa", ("shebaa", "chebaa", "shebaa farms"), "hasbaya", ("nabatieh",), "شبعا"),
    _town(
        "kfar-shouba",
        "Kfar Shouba",
        ("kfar shouba", "kfarchouba", "kfarshouba"),
        "hasbaya",
        ("nabatieh",),
        "كفرشوبا",
    ),
    _town("kfarhamam", "Kfarhamam", ("kfarhamam", "kfar hamam"), "hasbaya", ("nabatieh",), "كفرحمام"),
    _town(
        "kfar-roummane",
        "Kfar Roummane",
        ("kfar roummane", "kfar rumman", "kfarroummane"),
        "nabatieh",
        ("nabatieh",),
        "كفررمان",
    ),
    _town("arnoun", "Arnoun", ("arnoun",), "nabatieh", ("nabatieh",), "أرنون"),
    _town("kfar-tebnit", "Kfar Tebnit", ("kfar tebnit", "kfartebnit"), "nabatieh", ("nabatieh",), "كفرتبنيت"),
    _town("yohmor", "Yohmor", ("yohmor",), "nabatieh", ("nabatieh",)),
    # North
    Place(
        "tripoli", "Tripoli", ("tripoli", "trablous", "tarablus", "trablus"), ("north",), arabic=("طرابلس",)
    ),
    Place("koura", "Koura", ("koura", "kura"), ("north",), arabic=("الكورة",)),
    Place("batroun", "Batroun", ("batroun", "batrun"), ("north",), arabic=("البترون",)),
    Place("zgharta", "Zgharta", ("zgharta", "zghorta"), ("north",), arabic=("زغرتا",)),
    Place(
        "bcharre",
        "Bcharre",
        ("bcharre", "bsharri", "bcharreh", "becharre", "bsharre"),
        ("north",),
        arabic=("بشري",),
    ),
    Place(
        "minieh-danniyeh",
        "Minieh-Danniyeh",
        (
            "minieh-danniyeh",
            "minieh-dinnieh",
            "miniyeh-danniyeh",
            "minnieh",
            "minieh",
            "miniyeh",
            "dinnieh",
            "danniyeh",
        ),
        ("north",),
        arabic=("المنية الضنية", "المنية-الضنية", "المنية والضنية", "المنية", "الضنية"),
    ),
    _town("el-mina", "El Mina", ("el mina", "al mina", "el-mina", "al-mina"), "tripoli", ("north",)),
    _town(
        "bab-al-tabbaneh",
        "Bab al-Tabbaneh",
        ("bab al-tabbaneh", "bab al tabbaneh", "bab el tabbaneh", "bab el-tabbaneh", "tabbaneh", "tebbaneh"),
        "tripoli",
        ("north",),
        "باب التبانة",
        "التبانة",
    ),
    _town(
        "jabal-mohsen",
        "Jabal Mohsen",
        ("jabal mohsen", "jabal mohsin", "jebel mohsen"),
        "tripoli",
        ("north",),
        "جبل محسن",
    ),
    _town("qobbeh", "Qobbeh", ("qobbeh", "qobbe", "kobbeh"), "tripoli", ("north",)),
    _town("bhannine", "Bhannine", ("bhannine", "bhanine"), "minieh-danniyeh", ("north",), "بحنين"),
    _town("deir-ammar", "Deir Ammar", ("deir ammar",), "minieh-danniyeh", ("north",), "دير عمار"),
    _town("ehden", "Ehden", ("ehden",), "zgharta", ("north",), "إهدن"),
    _town("miziara", "Miziara", ("miziara",), "zgharta", ("north",), "مزيارة"),
    _town("amioun", "Amioun", ("amioun", "amyoun"), "koura", ("north",), "أميون"),
    _town("kousba", "Kousba", ("kousba",), "koura", ("north",), "كوسبا"),
    _town("chekka", "Chekka", ("chekka", "shekka"), "batroun", ("north",)),
    _town("tannourine", "Tannourine", ("tannourine", "tannourin"), "batroun", ("north",), "تنورين"),
    _town("hasroun", "Hasroun", ("hasroun",), "bcharre", ("north",), "حصرون"),
    # South
    Place("saida", "Saida", ("saida", "sidon", "sayda", "saïda"), ("south",), arabic=("صيدا",)),
    Place("tyre", "Tyre", ("tyre", "sour", "tyr"), ("south",), arabic=("صور",)),
    Place("jezzine", "Jezzine", ("jezzine", "jezzin", "jizzin", "jazzine"), ("south",), arabic=("جزين",)),
    _town("naqoura", "Naqoura", ("naqoura", "naqura"), "tyre", ("south",), "الناقورة"),
    _town("qana", "Qana", ("qana", "cana"), "tyre", ("south",), "قانا"),
    _town("abbasieh", "Abbasieh", ("abbasieh", "abbasiyeh"), "tyre", ("south",), "العباسية"),
    _town("jouaiya", "Jouaiya", ("jouaiya", "jwaya"), "tyre", ("south",), "جويا"),
    _town("bazourieh", "Bazourieh", ("bazourieh",), "tyre", ("south",), "البازورية"),
    _town("ghazieh", "Ghazieh", ("ghazieh", "ghaziyeh", "ghaziye", "ghazie"), "saida", ("south",), "الغازية"),
    _town("sarafand", "Sarafand", ("sarafand", "sarfand"), "saida", ("south",), "الصرفند"),
    _town("maghdouche", "Maghdouche", ("maghdouche", "maghdoucheh"), "saida", ("south",), "مغدوشة"),
    _town("bkassine", "Bkassine", ("bkassine",), "jezzine", ("south",), "بكاسين"),
    # the Palestine refugee camps, each in its governorate and district (Shatila and Bourj el-Barajneh:
    # Beirut's southern suburbs, counted in Beirut and in Mount Lebanon)
    Place(
        "ein-el-hilweh",
        "Ein el-Hilweh",
        (
            "ein el-hilweh",
            "ein el hilweh",
            "ein al-hilweh",
            "ein al hilweh",
            "ain el-hilweh",
            "ain el hilweh",
            "ain al-hilweh",
            "ain al hilweh",
            "ain el-helweh",
            "ein el-helweh",
        ),
        ("south",),
        camp=True,
        district="saida",
        arabic=("عين الحلوة",),
    ),
    Place(
        "mieh-mieh",
        "Mieh Mieh",
        ("mieh mieh", "mieh-mieh", "miyeh miyeh"),
        ("south",),
        camp=True,
        district="saida",
        arabic=("المية ومية",),
    ),
    Place(
        "rashidieh",
        "Rashidieh",
        ("rashidieh", "rashidiyeh", "rachidieh", "rashidiya"),
        ("south",),
        camp=True,
        district="tyre",
        arabic=("الرشيدية",),
    ),
    Place(
        "burj-el-shemali",
        "Burj el-Shemali",
        ("burj el-shemali", "burj el shemali", "burj shemali", "bourj el-chemali", "burj al-shamali"),
        ("south",),
        camp=True,
        district="tyre",
        arabic=("برج الشمالي",),
    ),
    Place(
        "el-buss",
        "El Buss",
        ("el buss", "el-buss", "al buss", "al-buss"),
        ("south",),
        camp=True,
        district="tyre",
    ),
    Place(
        "nahr-el-bared",
        "Nahr el-Bared",
        ("nahr el-bared", "nahr el bared", "nahr al-bared", "nahr al bared"),
        ("north",),
        camp=True,
        district="minieh-danniyeh",
        arabic=("نهر البارد",),
    ),
    Place(
        "beddawi",
        "Beddawi",
        ("beddawi", "baddawi", "beddaoui"),
        ("north",),
        camp=True,
        district="minieh-danniyeh",
        arabic=("البداوي",),
    ),
    Place(
        "mar-elias",
        "Mar Elias",
        ("mar elias",),
        ("beirut",),
        camp=True,
        district="beirut",
        arabic=("مار الياس",),
    ),
    Place(
        "shatila",
        "Shatila",
        ("shatila", "chatila", "shateela"),
        ("beirut", "mount-lebanon"),
        camp=True,
        arabic=("شاتيلا",),
    ),
    Place(
        "bourj-el-barajneh",
        "Bourj el-Barajneh",
        (
            "bourj el-barajneh",
            "bourj el barajneh",
            "burj el-barajneh",
            "burj el barajneh",
            "burj al-barajneh",
            "burj barajneh",
            "bourj barajneh",
        ),
        ("mount-lebanon", "beirut"),
        camp=True,
        district="baabda",
        arabic=("برج البراجنة",),
    ),
    Place(
        "dbayeh",
        "Dbayeh",
        ("dbayeh", "dbaye"),
        ("mount-lebanon",),
        camp=True,
        district="metn",
        arabic=("ضبية",),
    ),
    Place("wavel", "Wavel", ("wavel", "jalil camp"), ("baalbek-hermel",), camp=True, district="baalbek"),
)
# what a question means by these words (the owner's rule): the South and Nabatieh governorates for the south
# ("South Lebanon", "the south", "Liban-Sud", as many reports use it); the North and Akkar for the north ("the
# north", "North Lebanon", "Liban-Nord", "Shamal", the northern governorates or region: Akkar was part of the
# North until 2014). "North governorate", "South governorate" and one "southern governorate" are that one
_SOUTH = (
    "south",
    "south lebanon",
    "southern lebanon",
    "south of lebanon",
    "southern governorates",
    "southern districts",
    "southern region",
    "southern regions",
    "south region",
    "southern areas",
    "southern parts",
    "southern part of lebanon",
    "southern border",
    "southern borders",
    "sud-liban",
    "sud liban",
    "sud du liban",
    "liban-sud",
    "liban sud",
    "janoub",
    "jnoub",
    "الجنوب",
    "جنوب لبنان",
    "لبنان الجنوبي",
)
_NORTH = (
    "north",
    "north lebanon",
    "northern lebanon",
    "north of lebanon",
    "northern governorates",
    "north governorates",
    "northern districts",
    "northern region",
    "northern regions",
    "north region",
    "northern areas",
    "northern parts",
    "northern part of lebanon",
    "liban-nord",
    "liban nord",
    "nord-liban",
    "nord liban",
    "nord du liban",
    "shamal",
    "chamal",
    "الشمال",
    "شمال لبنان",
    "لبنان الشمالي",
)
REGIONS: dict[str, tuple[str, ...]] = {
    **{spelling: ("south", "nabatieh") for spelling in _SOUTH},
    **{spelling: ("north", "akkar") for spelling in _NORTH},
}
REGION_NAMES = {
    ("south", "nabatieh"): "the South (South and Nabatieh governorates)",
    ("north", "akkar"): "the north (North and Akkar governorates)",
}
# a region's part of a place is that place ("the southern region of the Bekaa", "the southern areas of Mount
# Lebanon": the Bekaa, Mount Lebanon), never the region; any other word after "of", "in" or "within" keeps
# the region ("of Lebanon", "in light of the escalation", "in collective shelters": :func:`_of_a_place`)
_AREA = re.compile(r"(?:regions?|areas?|parts?|districts|governorates)$")
THE = frozenset({"north", "south"})  # said "the North", "the South"
GOVERNORATE_WORDS = (
    "governorates",
    "governorate",
    "govs",
    "gov",
    "mohafazat",
    "mohafazah",
    "mohafaza",
    "muhafazat",
    "muhafazah",
    "muhafaza",
    "gouvernorats",
    "gouvernorat",
    "المحافظات",
    "محافظات",
    "المحافظة",
    "محافظة",
)
# the words for a district (a caza), and the phrases that ask for a breakdown by district
DISTRICT_WORDS = (
    "districts",
    "district",
    "cazas",
    "caza",
    "kazas",
    "kaza",
    "qadaas",
    "qadaa",
    "qada",
    "aqdiya",
    "الاقضية",
    "اقضية",
    "القضاء",
    "قضاء",
)
# the governorate and district words as findings write them (not "gov": texts write it for "government"; not
# "district" alone: "district hospitals")
WRITTEN_GOVERNORATE_WORDS = ("governorate", "governorates", "mohafaza", "mohafazat", "districts", "cazas")
# all of Lebanon: the words of a phrase joined by spaces or hyphens ("lebanon-wide", "Lebanon wide"), accents
# and the Arabic letters folded (:func:`fold`: "a l'echelle nationale")
NATIONAL = (
    "all of lebanon",
    "all over lebanon",
    "all lebanon",
    "whole of lebanon",
    "whole lebanon",
    "entire lebanon",
    "across lebanon",
    "throughout lebanon",
    "lebanon as a whole",
    "lebanon wide",
    "the whole country",
    "the entire country",
    "across the country",
    "across the whole country",
    "across the entire country",
    "throughout the country",
    "all over the country",
    "the country as a whole",
    "country level",
    "national level",
    "nationwide",
    "nation wide",
    "countrywide",
    "country wide",
    "nationally",
    "national figures",
    "national figure",
    "national average",
    "national averages",
    "national rate",
    "national rates",
    "national data",
    "national scale",
    "tout le liban",
    "l'ensemble du liban",
    "au niveau national",
    "niveau national",
    "a l'echelle nationale",
    "echelle nationale",
    "كل لبنان",
    "لبنان كله",
    "جميع انحاء لبنان",
    "كافة انحاء لبنان",
    "كافة المناطق اللبنانية",
    "على مستوى لبنان",
    "على المستوى الوطني",
    "المستوى الوطني",
    "على الصعيد الوطني",
)
# before a place's name: "the North", "El Nabatieh", "al-Koura", and in French "la Békaa", "le Akkar", "du
# Hermel", "de la Békaa", "au Mont-Liban", "l'Akkar"
ARTICLE = r"(?:(?:(?:the|el|al|an|ash|ech|la|le|les|du|des|de(?:\s+la)?|au|aux)[\s-]+)|l['’])?"
# before a name in Arabic script: "و" (and) or "ف", then "ب", "ل" or "ك" (in, for, as), and the article "ال"
# with a name that has it ("بالبقاع", "للبقاع", "والشمال"), never with one that has none ("الصور" is no Tyre)
_ARABIC_THE = "(?:[وف]?(?:[بك]?ال|لل))"
_ARABIC_BEFORE = "(?:[وف]?[بلك]?)"
_ARABIC = re.compile(r"[؀-ۿ]")
# between the words of a place's name, as questions and texts write it: spaces or one hyphen or dash
# ("North Bekaa", "North-Bekaa", "Bint–Jbeil"), never a comma, a full stop or a line break ("the North, Bekaa
# and Akkar": the North, the Bekaa and Akkar). A pattern of Python and of PostgreSQL alike
JOIN = "(?:[ \t ]+|[-–—])"

BY_KEY: dict[str, Place] = {p.key: p for p in (*GOVERNORATES, *DISTRICTS)}


@cache
def _folded_letter(letter: str) -> str:
    base = "".join(c for c in unicodedata.normalize("NFKD", letter) if not unicodedata.combining(c))
    return base if len(base) == 1 else letter


def fold(text: str) -> str:
    """``text`` with its letters' accents and the hamza of an Arabic alef taken off, one letter for one
    ("Békaa": "Bekaa", "أ": "ا"), so that a place is found whatever its accents and a span of the folded text
    is the same span of ``text``."""
    return text if text.isascii() else "".join(_folded_letter(c) for c in text)


def arabic(spelling: str) -> bool:
    return bool(_ARABIC.search(spelling))


def _arabic_forms(spelling: str) -> list[str]:
    """An Arabic spelling and the same with a final "ة" written "ه" ("النبطية", "النبطيه"), folded."""
    folded = fold(spelling)
    plain = " ".join(w[:-1] + "ه" if w.endswith("ة") else w for w in folded.split())
    return list(dict.fromkeys([folded, plain]))


def _joined(spelling: str) -> str:
    """A pattern of ``spelling`` with :data:`JOIN` between its words ("north bekaa": "North Bekaa" and
    "North-Bekaa", never "North, Bekaa")."""
    return JOIN.join(re.escape(word) for word in re.split(r"[ -]+", spelling))


def _lead(spelling: str) -> str:
    """The pattern of ``spelling`` (folded) with what may come before it: an article ("the", "El", "la"),
    or in Arabic script the letters joined to its first word ("و", "ب", "ل", "ال")."""
    if not arabic(spelling):
        return ARTICLE + _joined(spelling)
    if spelling.startswith("ال"):
        return _ARABIC_THE + _joined(spelling[2:])
    return _ARABIC_BEFORE + _joined(spelling)


@cache
def _of_a_place() -> str:
    """After a region's spelling ("the southern region"): no place's spelling after "of", "in" or "within"
    ("the southern region of the Bekaa" is the Bekaa)."""
    spellings = sorted({s for p in (*GOVERNORATES, *DISTRICTS) for s in p.spellings}, key=len, reverse=True)
    names = "|".join(_joined(fold(s)) for s in dict.fromkeys(spellings))
    return rf"(?!\s+(?:of|in|within)\s+{ARTICLE}(?:{names})(?![\w'-]))"


@cache
def _aliases() -> list[tuple[re.Pattern, tuple[str, ...], str, bool]]:
    """Every spelling a question may use (folded: :func:`fold`), the longest first: its pattern, the places it
    means, the name the line under the answer gives it, and whether it is written in Arabic script."""
    found: dict[str, tuple[tuple[str, ...], str, str]] = {}
    for place in (*GOVERNORATES, *DISTRICTS):
        name = said(place.key) if not place.governorates else f"{place.name} ({_names(place.governorates)})"
        suffix = r"(?:\s+camps?|\s+(?:مخيم|المخيم))?" if place.camp else ""
        for spelling in place.spellings:
            found.setdefault(fold(spelling), ((place.key,), name, suffix))
        for spelling in place.arabic:
            for form in _arabic_forms(spelling):
                found.setdefault(form, ((place.key,), name, ""))
    for spelling, keys in REGIONS.items():
        for form in _arabic_forms(spelling) if arabic(spelling) else [fold(spelling)]:
            found[form] = (keys, REGION_NAMES[keys], _of_a_place() if _AREA.search(form) else "")
    return [
        (
            re.compile(rf"(?<![\w'-]){_lead(spelling)}{suffix}(?![\w'-])", re.IGNORECASE),
            keys,
            name,
            arabic(spelling),
        )
        for spelling, (keys, name, suffix) in sorted(
            found.items(), key=lambda item: len(item[0]), reverse=True
        )
    ]


def said(key: str) -> str:
    """A place as the line under an answer names it: "Akkar", "the North"."""
    return f"the {BY_KEY[key].name}" if key in THE else BY_KEY[key].name


def _names(keys: tuple[str, ...]) -> str:
    return " and ".join(said(k) for k in keys)


def _phrases(words: tuple[str, ...]) -> re.Pattern:
    """One pattern of ``words`` (folded), the words of a phrase joined by spaces or hyphens."""
    joined = [r"[\s-]+".join(re.escape(w) for w in re.split(r"[ -]+", fold(p))) for p in words]
    alternation = "|".join(sorted(joined, key=len, reverse=True))
    return re.compile(rf"(?<![\w'-])(?:{alternation})(?![\w'-])", re.IGNORECASE)


_DISTRICT_WORDS = _phrases(DISTRICT_WORDS)
_NATIONAL = _phrases(NATIONAL)
_NATIONAL_WORD = _phrases(("national",))
# the words before a governorate or district word that ask for a breakdown, taken out with it (never
# searched): "by", "per", "across", "each", "all", "in all", "within each", "across all the", "broken
# down by", "disaggregated by", "a breakdown by"; "at (the)" before the word and its level ("governorate
# level", "district-wise", "governorate by governorate")
_HOW_MANY = r"(?:\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)"
_BREAKDOWN_LEAD = (
    r"(?:(?:broken\s+down|break\s+down|breakdowns?|disaggregated|disaggregation|split|divided)\s+)?"
    r"(?:(?:by|each|every|all|per|across|different|various|"
    r"(?:for|in|of|across|at|within|on)\s+(?:each|every|all))(?:\s+of)?\s+(?:the\s+)?|"
    # "in the nine governorates"
    rf"(?:in|of|across|for|from|within)\s+the\s+(?={_HOW_MANY}\s))"
    # "across Lebanon's governorates", "in all eight governorates", "across the country's governorates"
    rf"(?:(?:lebanon['’]?s|lebanese|(?:the\s+)?country['’]?s|its|their|respective|{_HOW_MANY})\s+){{0,2}}"
)
_AT = r"(?:(?:at|on)\s+(?:the\s+)?)"
_LEVEL = r"(?:[\s-]+(?:levels?|wise|breakdowns?|by\s+(?:the\s+)?(?:{words})))"  # "governorate-level"
_DISTRICT = "|".join(re.escape(w) for w in DISTRICT_WORDS)
_GOVERNORATE = "|".join(re.escape(w) for w in GOVERNORATE_WORDS)
# a breakdown by district: "by district", "each caza", "all the districts", "per district", "at district
# level", "district-level", or the plural ("the districts"); "by governorate and district" and "by district
# and governorate" too (the finer of the two)
_AND = r"\s*(?:and|or|&|/|,)\s*(?:(?:by|per|each|every)\s+)?(?:the\s+)?"
_BY_DISTRICT = re.compile(
    rf"(?<![\w'-])(?:{_BREAKDOWN_LEAD}(?:(?:{_GOVERNORATE}){_AND})?(?:{_DISTRICT})"
    rf"(?:{_AND}(?:{_GOVERNORATE}))?{_LEVEL.format(words=_DISTRICT)}?|"
    rf"{_AT}?(?:{_DISTRICT}){_LEVEL.format(words=_DISTRICT)}|"
    rf"(?:districts|cazas|kazas|qadaas|aqdiya){_LEVEL.format(words=_DISTRICT)}?)(?![\w'-])",
    re.IGNORECASE,
)
# the governorate words, with the words that ask for the breakdown ("per gov", "across governorates", "at
# governorate level"), and "by region" ("region" alone is no breakdown: "the southern region")
_BY_GOVERNORATE = re.compile(
    rf"(?<![\w'-])(?:(?:{_BREAKDOWN_LEAD}|{_AT})?(?:{_GOVERNORATE}){_LEVEL.format(words=_GOVERNORATE)}?|"
    rf"(?:by|per|each|across)\s+(?:the\s+)?regions?)(?![\w'-])",
    re.IGNORECASE,
)


@dataclass
class Reading:
    """What a question says about places: its text without them, the places it names (their keys, and the
    names said under the answer), and whether it asks for all of Lebanon or a breakdown by governorate."""

    text: str
    keys: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    national: bool = False
    breakdown: bool = False
    by_district: bool = False  # the breakdown was asked by district ("by district", "each caza")
    national_word: bool = False  # "national" with no place named: left in the text, a word of the subject

    @property
    def restricted(self) -> bool:
        """Whether the findings must name one of the places (a place named, and not "all Lebanon")."""
        return bool(self.keys) and not self.national

    @property
    def label(self) -> str:
        """The places named: "Akkar", "Akkar or the North", "Akkar, Beirut or the North"."""
        if len(self.names) < 2:
            return "".join(self.names)
        return f"{', '.join(self.names[:-1])} or {self.names[-1]}"

    def alternatives(self) -> list[str]:
        """The spellings a finding's own words may use for any of the places named: one group."""
        return _pruned([s for key in self.keys for s in written(key)])

    def broader(self) -> list[str]:
        """The spellings of the governorates of the districts and towns named (a finding about the North
        when the question names Tripoli), for the ranking of a wider search."""
        governorates = [g for key in self.keys for g in BY_KEY[key].governorates]
        return _pruned([s for key in dict.fromkeys(governorates) for s in written(key)])


def written(key: str) -> list[str]:
    """How findings may write place ``key``: its spellings; for a governorate also its districts and towns,
    and the governorates it includes (Mount Lebanon: Keserwan-Jbeil, as older reports count it); for a
    district also its towns and camps (Zahle: Bar Elias, Tyre: Qana and Rashidieh). Never its Arabic
    spellings: the findings are in English."""
    place = BY_KEY[key]
    found = list(place.spellings)
    if not place.governorates:
        for district in DISTRICTS:
            if key in district.governorates:
                found += district.spellings
        for other in place.includes:
            found += written(other)
    else:  # a district: also its towns and camps
        for town in DISTRICTS:
            if town.district == key:
                found += town.spellings
    return list(dict.fromkeys(found))


@cache
def shadows(spelling: str) -> tuple[str, ...]:
    """The spellings of places of other governorates that hold ``spelling`` as one of their words ("jbeil"
    in "Bint Jbeil", "north" in "North Metn"): a finding that names one of them does not name ``spelling``'s
    place."""
    owner = [p for p in (*GOVERNORATES, *DISTRICTS) if spelling in p.spellings]
    if not owner or " " in spelling or "-" in spelling:
        return ()
    found = []
    for place in owner:
        mine = {place.key, *place.governorates, *place.includes}
        for other in (*GOVERNORATES, *DISTRICTS):
            theirs = {other.key, *other.governorates}
            if mine & theirs or any(key in BY_KEY[g].includes for key in theirs for g in mine if g in BY_KEY):
                continue
            found += [s for s in other.spellings if s != spelling and spelling in re.split(r"[\s-]+", s)]
    return tuple(dict.fromkeys(found))


@cache
def longer_names() -> frozenset[str]:
    """The spellings that hold a word another governorate's place is spelt with ("north bekaa", "south
    metn", "bint jbeil": :func:`shadows`): a text names them only as written, their words joined by
    :data:`JOIN` ("the North, Bekaa and Akkar" is no North Bekaa)."""
    every = (*GOVERNORATES, *DISTRICTS)
    return frozenset(longer for p in every for s in p.spellings for longer in shadows(s))


def _one_of(spellings) -> str:
    return "|".join(_joined(s) for s in sorted(set(spellings), key=lambda w: (-len(w), w)))


@cache
def as_written(spellings: tuple[str, ...]) -> str:
    """A PostgreSQL pattern of the names ``spellings`` (:func:`longer_names`) as a text writes them."""
    return rf"\m(?:{_one_of(spellings)})\M"


@cache
def standalone(spellings: tuple[str, ...]) -> tuple[str, str]:
    """PostgreSQL patterns that find ``spellings`` (words that :func:`shadows` other places' spellings)
    written on their own: the words, and the longer names that hold them (:func:`as_written`). A text names
    one when it still writes the word once those names are taken out: "the Bekaa and North Bekaa" names the
    Bekaa, "North Bekaa" and "North-Bekaa" alone do not, "the North, Bekaa and Akkar" and "the South. Bekaa
    ..." name the North or the South and the Bekaa. Read in two steps, the plain word first, so that only
    the texts that write it are read again (one pattern of lookarounds would be tried at every word of
    every text)."""
    longer = [name for s in spellings for name in shadows(s)]
    return rf"\m(?:{_one_of(spellings)})\M", as_written(tuple(longer))


def breakdown_alternatives() -> list[str]:
    """A breakdown by governorate: the governorate words and the spellings of every governorate, district and
    town (a district or a town names its governorate)."""
    return _pruned(
        [*WRITTEN_GOVERNORATE_WORDS, *(s for p in (*GOVERNORATES, *DISTRICTS) for s in p.spellings)]
    )


def governorate_alternatives() -> list[list[str]]:
    """Each governorate's spellings with those of its districts and towns (each counted in its first
    governorate, as :func:`compares` counts them): a finding that names more governorates ranks first in a
    breakdown."""
    return [
        _pruned([*g.spellings, *(s for d in DISTRICTS if d.governorates[0] == g.key for s in d.spellings)])
        for g in GOVERNORATES
    ]


def _pruned(spellings: list[str]) -> list[str]:
    """``spellings`` without those that hold another one as a whole word ("akkar governorate" when "akkar" is
    there: the search finds both by the word), but for a word that does not find every text it is in
    (:func:`shadows`: "bint jbeil" stays with "jbeil", "north lebanon" with "north": "In North Lebanon and
    North Bekaa" names the North)."""
    unique = list(dict.fromkeys(spellings))
    singles = {s for s in unique if re.fullmatch(r"[\w'.]+", s)}
    return [
        s
        for s in unique
        if s in singles or not any(w in singles and not shadows(w) for w in re.split(r"[\s-]+", s))
    ]


_LATIN = re.compile(r"[A-Za-z]")


def _claimed(rest: str) -> list[tuple[int, int, tuple[str, ...], str]]:
    """The places ``rest`` names: (start, end, their keys, the name said), in the order it names them, read
    in its folded letters (:func:`fold`: the same spans)."""
    folded = fold(rest)
    scripts = {True: arabic(folded), False: bool(_LATIN.search(folded))}
    claimed: list[tuple[int, int, tuple[str, ...], str]] = []
    for pattern, keys, name, script in _aliases():  # the longest spellings first: "north metn" before "north"
        if not scripts[script]:
            continue
        for match in pattern.finditer(folded):
            start, end = match.span()
            if not any(start < e and s < end for s, e, _k, _n in claimed):
                claimed.append((start, end, keys, name))
    claimed.sort()
    return claimed


def names_in(text: str) -> list[str]:
    """The places ``text`` names, as it writes them ("Beirut" in "the 2020 Beirut port explosion")."""
    rest = f" {text or ''} "
    return [rest[start:end].strip() for start, end, _k, _n in _claimed(rest)]


_PLURAL = re.compile(
    r"(?<![\w'-])(?:governorates|mohafazat|muhafazat|gouvernorats|districts|cazas)(?![\w'-])", re.IGNORECASE
)


def compares(text: str) -> bool:
    """Whether a finding's ``text`` gives figures by governorate: it names two governorates or more (a
    district or a town as its governorate; "South Lebanon" as one), or speaks of governorates ("in all
    governorates")."""
    if _PLURAL.search(text or ""):
        return True
    named = set()
    for _s, _e, keys, _n in _claimed(f" {text or ''} "):
        place = BY_KEY[keys[0]]
        named.add(place.governorates[0] if place.governorates else place.key)
    return len(named) >= 2


def _cut(rest: str, folded: str, pattern: re.Pattern) -> tuple[str, str, bool]:
    """``rest`` and its folded letters without what ``pattern`` finds in the folded ones (the same spans), and
    whether it found any."""
    spans = [m.span() for m in pattern.finditer(folded)]
    for start, end in reversed(spans):
        rest, folded = rest[:start] + " " + rest[end:], folded[:start] + " " + folded[end:]
    return rest, folded, bool(spans)


def read(text: str) -> Reading:
    """The places ``text`` names (see the module's notes), in the order it names them, and the text without
    them, without the governorate words and without the words for all of Lebanon. A place a region named
    covers ("Nabatiyeh" with "the south") is not named again under the answer."""
    rest = f" {text or ''} "
    claimed = _claimed(rest)
    regions = [set(k) for _s, _e, k, _n in claimed if len(k) > 1]
    keys: list[str] = []
    names: list[str] = []
    for _s, _e, found, name in claimed:
        keys += [k for k in found if k not in keys]
        covered = len(found) == 1 and any(set(found) <= region for region in regions)
        if not covered and name not in names:
            names.append(name)
    for start, end, _k, _n in sorted(claimed, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    folded = fold(rest)
    rest, folded, national = _cut(rest, folded, _NATIONAL)
    national_word = bool(_NATIONAL_WORD.search(folded))
    if national_word and keys:  # "the national rate and Akkar": every place, Akkar first
        national, national_word = True, False
        rest, folded, _found = _cut(rest, folded, _NATIONAL_WORD)
    by_district = not keys and bool(_BY_DISTRICT.search(folded))
    rest, folded, _found = _cut(rest, folded, _BY_DISTRICT)
    if keys:  # "Akkar district"
        rest, folded, _found = _cut(rest, folded, _DISTRICT_WORDS)
    rest, folded, governorate = _cut(rest, folded, _BY_GOVERNORATE)
    return Reading(
        text=" ".join(rest.split()),
        keys=keys,
        names=names,
        national=national,
        breakdown=(governorate or by_district) and not keys,
        by_district=by_district and not governorate,
        national_word=national_word,
    )
