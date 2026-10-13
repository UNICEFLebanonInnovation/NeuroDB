"""The parity harness: record a page group on the base commit, check it on the working tree (SPEC7 §3.5).

    python -m scripts.parity.core record <group> out.json [--checkout DIR] [--at MOMENT]
    python -m scripts.parity.core check <group> out.json [--checkout DIR] [--report diff.json]

Run it from the working tree's root. ``--checkout`` (default: this repository) is the code that answers: the
base worktree for ``record``, the working tree for ``check``. The database is the one ``DATABASE_URL`` names; it
must start with ``neurodb_parity_`` (never ``neurodb_v3`` or ``test_neurodb_v3``), because some GET requests
write (the sessions of the signed-in test users, among others): restore the snapshot before each command.

**What one observation holds**, for each user and each filter of the group's matrix:

- ``figures``: the context values the page draws, at the group's context paths, made plain (a model
  instance becomes its label, primary key and field values; dates become ISO text), so they compare as JSON;
- ``json``: every ``<script type="application/json" id=...>`` payload, parsed;
- ``blocks``: the visible text and the links (``href``, form ``action``) of ``<main>``, cut at each heading
  and keyed by it;
- ``ids``: the element ids in ``<main>`` (anchors other pages link to).

``record`` reads the old page as one response (its ``response.context``). ``check`` asks for the shell, then
for every placeholder's part (``[data-part]``) with the HTMX headers the browser sends, puts each answer in
its place (again for placeholders that arrive inside a part, such as a lazy list), and merges the part
contexts. On both sides the elements ``<main>`` loads by itself (``hx-trigger="load"``, like the old NeuroDB
Watch slot) are asked for and put in place too, so both pages are compared as a person sees them once
everything has arrived. ``check`` runs the matrix twice: with every cache emptied before each page (each
part asked for), and warm (the same page again, when the shell draws what is already stored inline).

**The clock** is frozen (``--at``, default ``FROZEN_AT``) for the whole run, as ``freezegun`` would:
``datetime.date.today()``, ``datetime.datetime.now()`` and ``django.utils.timezone.now()`` answer the frozen
moment in every module, while the ``datetime`` module itself stays as it is (pickling keeps working). The
database's own clock is not frozen; the overview does not read it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import decimal
import enum
import importlib
import importlib.util
import json
import math
import os
import pkgutil
import re
import subprocess
import sys
import time
import types
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DATABASE_PREFIX = "neurodb_parity_"
RESERVED_DATABASES = frozenset({"neurodb_v3", "test_neurodb_v3", "postgres", "template0", "template1"})
FROZEN_AT = "2026-10-12T12:30:00+00:00"  # the parity world's day (15:30 in Beirut), after its last sync
SERVER = "http://testserver"
PENDING_RETRIES = 40  # a part answering 202 ("still computing") is asked again, as parts.js does
PENDING_DELAY = 1.5
RUN_ENV = ("DJANGO_ENV", "DJANGO_SETTINGS_MODULE", "DJANGO_DEBUG", "FMM_ENABLED")  # kept with each run


# ------------------------------------------------------------------------------------------------ the world
def check_database(name: str) -> None:
    """Refuse any database that is not a parity copy."""
    if name in RESERVED_DATABASES or not name.startswith(DATABASE_PREFIX):
        raise SystemExit(
            f"refusing the database {name!r}: parity runs only on a copy named {DATABASE_PREFIX}<stage>"
        )


def git_state(checkout: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False
        )
        return done.stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "changed_files": len(git("status", "--porcelain").splitlines()),
    }


def setup(checkout: Path | str | None = None, *, settings_module: str | None = None, contexts: bool = True):
    """Start Django with ``checkout``'s code (default: this repository) on the parity database.

    ``contexts``: keep each response's template contexts (``setup_test_environment``), which the parity needs
    and the timing must not pay for. A GET never starts a background job here."""
    checkout = Path(checkout or REPO).resolve()
    if str(REPO) not in sys.path:  # this package, whatever the working directory becomes
        sys.path.append(str(REPO))
    sys.path.insert(0, str(checkout))
    os.chdir(checkout)
    if settings_module:
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.db import connection

    import neurodb

    code = Path(neurodb.__file__).resolve()
    if checkout not in code.parents:
        raise SystemExit(f"the code comes from {code}, not from the checkout {checkout}: check PYTHONPATH")
    check_database(connection.settings_dict["NAME"])
    if contexts:
        from django.test.utils import setup_test_environment

        setup_test_environment()
    else:  # the test client's host, without the rest of the test environment
        from django.conf import settings

        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]
    from neurodb.integrations import background

    background.start_command = lambda *args, **kwargs: 0
    return {
        "checkout": str(checkout),
        **git_state(checkout),
        "database": connection.settings_dict["NAME"],
        # the settings the run used, so a check can run under the same ones as its recording
        "env": {name: os.environ.get(name) for name in RUN_ENV},
    }


def load_group(name: str) -> types.ModuleType:
    if not re.fullmatch(r"[a-z_]+", name):
        raise SystemExit(f"no group named {name!r}")
    return importlib.import_module(f"scripts.parity.{name}")


def clear_caches() -> None:
    """Every cache empty, as after a deployment: the per-process ones and the shared page figures (and the
    request memo of ``neurodb.core.compute``, where the checkout has it)."""
    from django.conf import settings
    from django.core.cache import caches

    for alias in settings.CACHES:
        caches[alias].clear()
    if importlib.util.find_spec("neurodb.core.compute") is not None:
        compute = importlib.import_module("neurodb.core.compute")
        compute.reset_memo()


def signed_in_client(user):
    from django.test import Client

    client = Client(raise_request_exception=False)
    client.force_login(user)
    return client


# ------------------------------------------------------------------------------------------------ the clock
_REAL_MODULE = datetime
_REAL_DATE = datetime.date
_REAL_DATETIME = datetime.datetime
_frozen: _REAL_DATETIME | None = None
_scanned: set[str] = set()
_NEVER_PATCHED = {
    "datetime",
    "_datetime",
    "_pydatetime",
    "pickle",
    "_pickle",
    "copyreg",
    "__main__",
    __name__,
}


def _moment() -> _REAL_DATETIME:
    if _frozen is None:
        raise RuntimeError("the clock is not frozen")
    return _frozen


class _DateType(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _REAL_DATE)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATE)


class _DateTimeType(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATETIME)


class FrozenDate(_REAL_DATE, metaclass=_DateType):
    """``datetime.date`` whose ``today()`` is the frozen moment's local date. It only ever makes real dates."""

    def __new__(cls, *args: Any, **kwargs: Any):
        return _REAL_DATE(*args, **kwargs)

    @classmethod
    def today(cls):
        return _moment().astimezone().date()


class FrozenDateTime(_REAL_DATETIME, metaclass=_DateTimeType):
    """``datetime.datetime`` whose ``now()``, ``today()`` and ``utcnow()`` are the frozen moment."""

    def __new__(cls, *args: Any, **kwargs: Any):
        return _REAL_DATETIME(*args, **kwargs)

    @classmethod
    def now(cls, tz=None):
        moment = _moment()
        return moment.astimezone(tz) if tz is not None else moment.astimezone().replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def utcnow(cls):
        return _moment().replace(tzinfo=None)


_FROZEN_MODULE = types.ModuleType("datetime", _REAL_MODULE.__doc__)
_FROZEN_MODULE.__dict__.update({k: v for k, v in vars(_REAL_MODULE).items() if not k.startswith("__")})
_FROZEN_MODULE.date = FrozenDate
_FROZEN_MODULE.datetime = FrozenDateTime


def _patch_modules() -> int:
    """Point every loaded module's ``datetime``, ``date`` and ``datetime.datetime`` names at the frozen ones.
    Modules already scanned are skipped; call it again after new imports."""
    swaps = {id(_REAL_MODULE): _FROZEN_MODULE, id(_REAL_DATE): FrozenDate, id(_REAL_DATETIME): FrozenDateTime}
    patched = 0
    for name, module in list(sys.modules.items()):
        if name in _scanned or name in _NEVER_PATCHED or module is None:
            continue
        _scanned.add(name)
        try:
            items = list(vars(module).items())
        except TypeError:
            continue
        for attribute, value in items:
            swap = swaps.get(id(value))
            if swap is not None and value in (_REAL_MODULE, _REAL_DATE, _REAL_DATETIME):
                try:
                    setattr(module, attribute, swap)
                except (AttributeError, TypeError):
                    continue
                patched += 1
    return patched


def _import_everything() -> None:
    """Import the application's modules, the URL configuration and the template libraries before freezing,
    so that a module imported lazily inside a request is rare (``refreeze`` catches those)."""
    from django.template import engines
    from django.urls import get_resolver

    import neurodb

    for info in pkgutil.walk_packages(neurodb.__path__, "neurodb."):
        if ".migrations" in info.name or ".management" in info.name or ".tests" in info.name:
            continue
        try:
            importlib.import_module(info.name)
        except Exception as error:  # noqa: BLE001 - an optional module (a missing extra) stays unfrozen
            print(f"  (not imported before freezing: {info.name}: {error})", file=sys.stderr)
    get_resolver().url_patterns  # noqa: B018 - imports every view
    for engine in engines.all():
        getattr(engine, "engine", engine)


def freeze(moment: str | _REAL_DATETIME = FROZEN_AT) -> _REAL_DATETIME:
    """Freeze the clock at ``moment`` (aware; ISO text accepted) for the rest of the process."""
    global _frozen
    if isinstance(moment, str):
        moment = _REAL_DATETIME.fromisoformat(moment)
    if moment.tzinfo is None:
        raise SystemExit("the frozen moment needs a time zone (for example 2026-10-12T12:30:00+00:00)")
    _import_everything()
    _frozen = moment.astimezone(datetime.UTC)
    _patch_modules()
    return _frozen


def refreeze() -> None:
    """Patch the modules imported since the last scan (a lazy import inside a request)."""
    if _frozen is not None:
        _patch_modules()


# ------------------------------------------------------------------------------------------- plain values
_SCALARS = (str, int, bool, type(None))


def plain(value: Any, _depth: int = 0) -> Any:
    """``value`` as JSON-comparable data. A model instance becomes ``{"model", "pk", "fields", "str"}`` with its
    concrete fields by attribute name (foreign keys as ids: no query), plus the plain values a query added to
    it (annotations); a QuerySet is evaluated; dates and Decimals become tagged text."""
    if _depth > 60:
        return {"__too_deep__": repr(value)[:200]}
    nested = _depth + 1
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"__float__": repr(value)}
    if isinstance(value, str):  # SafeString and other str subclasses
        return str(value)
    if isinstance(value, int):  # IntEnum and other int subclasses
        return int(value)
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, _REAL_DATETIME):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, _REAL_DATE):
        return {"__date__": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"__time__": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"__timedelta__": value.total_seconds()}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return plain(value.value, nested)
    if isinstance(value, bytes | bytearray):
        return {"__bytes__": len(value)}
    from django.db.models import Model
    from django.db.models.query import QuerySet
    from django.utils.functional import Promise

    if isinstance(value, Promise):
        return str(value)
    if isinstance(value, Model):
        return _model(value, nested)
    if isinstance(value, QuerySet):
        return [plain(item, nested) for item in value]
    if isinstance(value, Mapping):
        return {_key(key): plain(item, nested) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item, nested) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(
            (plain(item, nested) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": type(value).__name__,
            **{f.name: plain(getattr(value, f.name), nested) for f in dataclasses.fields(value)},
        }
    if hasattr(value, "__dict__") and not callable(value):
        return {
            "__object__": type(value).__name__,
            **{k: plain(v, nested) for k, v in vars(value).items() if not k.startswith("_")},
        }
    return {"__repr__": repr(value)[:500]}


def _key(key: Any) -> str:
    return key if isinstance(key, str) else json.dumps(plain(key), sort_keys=True)


def _model(obj: Any, depth: int) -> dict[str, Any]:
    fields = {f.attname: plain(getattr(obj, f.attname), depth) for f in obj._meta.concrete_fields}
    extra = {
        name: plain(value, depth)
        for name, value in vars(obj).items()
        if not name.startswith("_")
        and name not in fields
        and isinstance(value, (*_SCALARS, float, decimal.Decimal))
    }
    out = {"model": obj._meta.label, "pk": plain(obj.pk, depth), "fields": fields, "str": str(obj)}
    if extra:
        out["extra"] = extra
    return out


def flatten(tree: Any, prefix: str = "") -> dict[str, Any]:
    """``{"a.b[0].c": leaf}`` of a plain tree; an empty dict or list is a leaf of its own."""
    out: dict[str, Any] = {}
    if isinstance(tree, dict) and tree and not _tagged(tree):
        for key, value in tree.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(tree, list) and tree:
        for index, value in enumerate(tree):
            out.update(flatten(value, f"{prefix}[{index}]"))
    else:
        out[prefix] = tree
    return out


def _tagged(tree: dict) -> bool:
    return len(tree) == 1 and next(iter(tree)).startswith("__")


# ------------------------------------------------------------------------------------------ reading a page
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
NO_TEXT = frozenset({"script", "style", "noscript", "template"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SPACE = re.compile(r"\s+")


@dataclasses.dataclass
class _Frame:
    tag: str
    hidden: bool
    in_main: bool


class _Reader(HTMLParser):
    """The visible text and links of ``<main>`` cut at each heading, its element ids, and every JSON payload
    of the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = []
        self.blocks: list[dict[str, Any]] = [{"heading": "", "text": [], "links": []}]
        self.heading: list[str] | None = None
        self.heading_tag = ""
        self.ids: list[str] = []
        self.json: dict[str, str] = {}
        self.json_id: str | None = None
        self.json_text: list[str] = []

    def _top(self) -> _Frame:
        return self.stack[-1] if self.stack else _Frame("", False, False)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        top = self._top()
        in_main = top.in_main or tag == "main"
        if tag == "script" and values.get("type") == "application/json" and values.get("id"):
            self.json_id, self.json_text = values["id"], []
        if in_main:
            if values.get("id"):
                self.ids.append(values["id"])
            if tag == "a" and "href" in values:
                self.blocks[-1]["links"].append(values["href"])
            if tag == "form" and "action" in values:
                self.blocks[-1]["links"].append("form " + values["action"])
        if tag in VOID:
            return
        hidden = top.hidden or tag in NO_TEXT or "hidden" in values
        self.stack.append(_Frame(tag, hidden, in_main))
        if in_main and not hidden and tag in HEADINGS and self.heading is None:
            self.heading, self.heading_tag = [], tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if tag == "script" and self.json_id is not None:
            self.json[self.json_id] = "".join(self.json_text)
            self.json_id = None
        if self.heading is not None and tag == self.heading_tag:
            title = _SPACE.sub(" ", " ".join(self.heading)).strip()
            self.blocks.append({"heading": title, "text": [], "links": []})
            self.heading = None
        if not any(frame.tag == tag for frame in self.stack):
            return  # a stray end tag
        while self.stack:
            if self.stack.pop().tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.json_id is not None:
            self.json_text.append(data)
            return
        top = self._top()
        if not top.in_main or top.hidden:
            return
        text = _SPACE.sub(" ", data).strip()
        if not text:
            return
        (self.heading if self.heading is not None else self.blocks[-1]["text"]).append(text)


def read_html(html: str) -> dict[str, Any]:
    """``{"blocks": {heading: {"text", "links"}}, "order": [heading...], "ids": [...], "json": {id: data}}``.
    Repeated headings get " (2)", " (3)"... The text before the first heading is under ""."""
    reader = _Reader()
    reader.feed(html)
    reader.close()
    blocks: dict[str, dict[str, Any]] = {}
    for block in reader.blocks:
        key, n = block["heading"], 1
        while key in blocks:
            n += 1
            key = f"{block['heading']} ({n})"
        blocks[key] = {"text": " ".join(block["text"]), "links": block["links"]}
    payloads: dict[str, Any] = {}
    for element_id, text in reader.json.items():
        try:
            payloads[element_id] = json.loads(text)
        except ValueError:
            payloads[element_id] = {"__unparsed__": text}
    return {"blocks": blocks, "order": list(blocks), "ids": reader.ids, "json": payloads}


# ---------------------------------------------------------------------------------------- assembling a page
PLACEHOLDER = re.compile(r'<div\b[^>]*?\sdata-part="(?P<name>[a-z][a-z0-9-]*)"[^>]*>')
LOADS_ITSELF = re.compile(r'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*?\shx-trigger="load"[^>]*>')
MAIN = re.compile(r"<main\b[^>]*>(?P<body>.*)</main>", re.S)


@dataclasses.dataclass
class Page:
    """A page as the browser shows it once everything has arrived."""

    url: str
    shell: Any  # the response to the page's own request
    html: str
    parts: dict[str, Any] = dataclasses.field(default_factory=dict)  # part name -> its response
    loads: list[Any] = dataclasses.field(default_factory=list)  # hx-trigger="load" responses
    inline: list[str] = dataclasses.field(default_factory=list)  # parts the shell drew already loaded
    pending: int = 0  # 202 answers ("still computing") met on the way


def attributes(start_tag: str) -> dict[str, str]:
    """The attributes of one opening tag."""
    found: dict[str, str] = {}

    class _One(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if not found:
                found.update({key: value or "" for key, value in attrs})
                found["__tag__"] = tag

    parser = _One(convert_charrefs=True)
    parser.feed(start_tag)
    parser.close()
    return found


def element_end(html: str, start: int, tag: str = "div") -> int:
    """Where the element whose opening tag starts at ``start`` ends (after its closing tag)."""
    depth = 0
    for match in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(html, start):
        depth += -1 if match.group().startswith("</") else 1
        if depth == 0:
            return match.end()
    raise ValueError(f"an unclosed <{tag}> at {start}")


def hx_get(client, address: str, element_id: str, page_url: str):
    """A request as htmx sends it from the page at ``page_url``, aimed at ``element_id``. A 202 ("still
    computing") is asked again after a pause, as parts.js does."""
    headers = {
        "HTTP_HX_REQUEST": "true",
        "HTTP_HX_CURRENT_URL": SERVER + page_url,
        "HTTP_HX_TARGET": element_id,
        "HTTP_HX_TRIGGER": element_id,
    }
    pending = 0
    if not address:
        return _NoAnswer(), pending
    for _ in range(PENDING_RETRIES):
        refreeze()
        response = client.get(address, **headers)
        if response.status_code != 202:
            return response, pending
        pending += 1
        time.sleep(PENDING_DELAY)
    return response, pending


class _NoAnswer:
    """What a placeholder without an address gets: nothing (status 0)."""

    status_code = 0
    content = b""
    charset = "utf-8"
    context = None
    templates: list[Any] = []

    def has_header(self, name: str) -> bool:
        return False


def _swap(html: str, start: int, tag: str, swap: str, element_id: str, body: str) -> str:
    end = element_end(html, start, tag)
    if swap.startswith("innerHTML"):
        opening = html[start : html.index(">", start) + 1]
        return html[:start] + opening + body + f"</{tag}>" + html[end:]
    return html[:start] + body + html[end:]


def get_page(client, url: str) -> Page:
    """``url`` for the signed-in ``client``: the page, each placeholder filled with its part (parts arriving
    inside a part too), and each element of ``<main>`` that loads itself filled with its answer. An answer
    that is not a 200 leaves its placeholder; the observation's statuses show it."""
    refreeze()
    shell = client.get(url)
    page = Page(url=url, shell=shell, html=shell.content.decode(shell.charset or "utf-8"))
    if shell.status_code != 200:
        return page
    request = getattr(shell, "wsgi_request", None)
    registered = getattr(getattr(request, "_parts_page", None), "parts", None) or {}
    left: set[str] = set()
    while True:  # placeholders, until none is left
        match = next((m for m in PLACEHOLDER.finditer(page.html) if m["name"] not in left), None)
        if match is None:
            break
        name = match["name"]
        found = attributes(match.group(0))
        element_id = found.get("id") or f"part-{name}"
        response, pending = hx_get(client, found.get("hx-get", ""), element_id, url)
        page.pending += pending
        page.parts[name] = response
        if response.status_code != 200 or response.has_header("HX-Redirect"):
            left.add(name)
            continue
        body = response.content.decode(response.charset or "utf-8")
        page.html = _swap(
            page.html, match.start(), "div", found.get("hx-swap", "outerHTML"), element_id, body
        )
    page.inline = [
        name for name in registered if name not in page.parts and f'data-part-loaded="{name}"' in page.html
    ]
    done: set[int] = set()
    while True:  # elements of <main> that load themselves (the old NeuroDB Watch slot)
        main = MAIN.search(page.html)
        if main is None:
            break
        offset = main.start("body")
        match = next(
            (m for m in LOADS_ITSELF.finditer(main["body"]) if hash(m.group(0)) not in done),
            None,
        )
        if match is None:
            break
        done.add(hash(match.group(0)))
        found = attributes(match.group(0))
        response, pending = hx_get(client, found.get("hx-get", ""), found.get("id", ""), url)
        page.pending += pending
        page.loads.append(response)
        if response.status_code != 200:
            continue
        body = response.content.decode(response.charset or "utf-8")
        start = offset + match.start()
        page.html = _swap(page.html, start, found["__tag__"], found.get("hx-swap", "innerHTML"), "", body)
    return page


# ---------------------------------------------------------------------------------------------- observing
def observe(group: types.ModuleType, page: Page, *, role: str, label: str, mode: str) -> dict[str, Any]:
    reading = read_html(page.html)
    figures, problems = group.figures(page, mode)
    return {
        "role": role,
        "label": label,
        "url": page.url,
        "status": page.shell.status_code,
        "parts": {name: response.status_code for name, response in page.parts.items()},
        "inline": page.inline,
        "loads": [response.status_code for response in page.loads],
        "pending": page.pending,
        "problems": problems,
        "figures": figures,
        "json": reading["json"],
        "blocks": reading["blocks"],
        "order": reading["order"],
        "ids": reading["ids"],
    }


# --------------------------------------------------------------------------------------------- comparing
def _show(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} characters)"


def _text_difference(recorded: str, checked: str) -> tuple[str, str]:
    """The two texts around their first difference."""
    at = next((i for i, (a, b) in enumerate(zip(recorded, checked, strict=False)) if a != b), None)
    if at is None:
        at = min(len(recorded), len(checked))
    start = max(0, at - 80)
    return recorded[start : at + 160], checked[start : at + 160]


def compare(
    group: types.ModuleType, recorded: dict[str, Any], checked: dict[str, Any]
) -> list[dict[str, Any]]:
    """The differences between one recorded observation and its check, after the group's mapping. A group
    with several pages may bring both observations to one form first (``comparable(observation, side)``,
    ``side`` "record" or "check"), for a change made on purpose that a rename of ids cannot express."""
    comparable = getattr(group, "comparable", None)
    if comparable is not None:
        recorded, checked = comparable(recorded, "record"), comparable(checked, "check")
    where = {"role": recorded["role"], "label": recorded["label"], "url": recorded["url"]}
    out: list[dict[str, Any]] = []

    def add(kind: str, path: str, was: Any, now: Any) -> None:
        out.append({**where, "kind": kind, "path": path, "recorded": _show(was), "checked": _show(now)})

    if recorded["status"] != checked["status"]:
        add("status", "", recorded["status"], checked["status"])
    for name, status in checked["parts"].items():
        if status != 200:
            add("part status", name, 200, status)
    for status in checked["loads"]:
        if status != 200:
            add("load status", "", 200, status)
    for problem in checked["problems"]:
        add("consistency", problem, "", "")
    old, new = flatten(recorded["figures"]), flatten(checked["figures"])
    for path in sorted(old.keys() | new.keys()):
        if path not in new:
            add("figure missing", path, old[path], None)
        elif path not in old:
            add("figure added", path, None, new[path])
        elif old[path] != new[path]:
            add("figure", path, old[path], new[path])
    renamed = {group.JSON_IDS.get(key, key): value for key, value in recorded["json"].items()}
    for element_id in sorted(renamed.keys() | checked["json"].keys()):
        if element_id not in checked["json"]:
            add("json missing", element_id, renamed[element_id], None)
        elif element_id not in renamed:
            add("json added", element_id, None, checked["json"][element_id])
        else:
            was, now = flatten(renamed[element_id]), flatten(checked["json"][element_id])
            for path in sorted(was.keys() | now.keys()):
                if was.get(path, "<absent>") != now.get(path, "<absent>"):
                    add("json", f"{element_id}:{path}", was.get(path, "<absent>"), now.get(path, "<absent>"))
    if recorded["order"] != checked["order"]:
        add("headings", "order", recorded["order"], checked["order"])
    for heading in recorded["order"]:
        was = recorded["blocks"][heading]
        now = checked["blocks"].get(heading)
        if now is None:
            add("block missing", heading, was["text"], None)
            continue
        if was["text"] != now["text"]:
            add("text", heading, *_text_difference(was["text"], now["text"]))
        if was["links"] != now["links"]:
            gone = list((Counter(was["links"]) - Counter(now["links"])).elements())
            added = list((Counter(now["links"]) - Counter(was["links"])).elements())
            if gone or added:
                add("links", heading, gone, added)
            else:
                add("links order", heading, was["links"], now["links"])
    for heading in checked["order"]:
        if heading not in recorded["blocks"]:
            add("block added", heading, None, checked["blocks"][heading]["text"])
    old_ids = {group.JSON_IDS.get(i, i) for i in recorded["ids"]}
    new_ids = {i for i in checked["ids"] if not group.NEW_IDS.fullmatch(i)}
    for element_id in sorted(old_ids - new_ids):
        add("id missing", element_id, element_id, None)
    for element_id in sorted(new_ids - old_ids):
        add("id added", element_id, None, element_id)
    return out


# --------------------------------------------------------------------------------------------- commands
def _pages(group, users: Mapping[str, Any], matrix: Iterable[tuple[str, str]], *, mode: str, warm: bool):
    """Observe every page of the matrix for every user. Cold: every cache emptied before each page. Warm:
    the page is asked for once (its figures stored), then again, and the second one is observed."""
    for role, user in users.items():
        client = signed_in_client(user)
        for label, url in matrix:
            if warm:
                get_page(client, url)
            else:
                clear_caches()
            started = time.perf_counter()
            page = get_page(client, url)
            seconds = time.perf_counter() - started
            observation = observe(group, page, role=role, label=label, mode=mode)
            yield observation, seconds


def record(args: argparse.Namespace) -> int:
    info = setup(args.checkout)
    frozen = freeze(args.at)
    group = load_group(args.group)
    users = group.users()
    matrix = group.matrix(users)
    print(f"recording {args.group} on {info['checkout']} ({info['commit'][:9]}), database {info['database']}")
    print(
        f"clock frozen at {frozen.isoformat()}; users: {', '.join(f'{r}={u.username}' for r, u in users.items())}"
    )
    pages = []
    for observation, seconds in _pages(group, users, matrix, mode="record", warm=False):
        pages.append(observation)
        print(
            f"  {observation['role']:7} {observation['label']:44} {observation['status']} {seconds * 1000:6.0f} ms"
        )
    out = {
        "group": args.group,
        "recorded": info,
        "frozen_at": frozen.isoformat(),
        "users": {role: user.username for role, user in users.items()},
        "matrix": [list(row) for row in matrix],
        "pages": pages,
    }
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=False, default=str))
    print(f"{len(pages)} pages recorded in {args.out}")
    return 0


def check(args: argparse.Namespace) -> int:
    recording = json.loads(Path(args.out).read_text())
    if recording["group"] != args.group:
        raise SystemExit(f"{args.out} is a recording of {recording['group']!r}, not of {args.group!r}")
    info = setup(args.checkout)
    frozen = freeze(args.at or recording["frozen_at"])
    group = load_group(recording["group"])
    from neurodb.accounts.models import User

    users = {role: User.objects.get(username=username) for role, username in recording["users"].items()}
    matrix = [tuple(row) for row in recording["matrix"]]
    recorded = {(page["role"], page["label"]): page for page in recording["pages"]}
    print(
        f"checking {recording['group']} on {info['checkout']} ({info['commit'][:9]}, {info['changed_files']} "
        f"changed files), database {info['database']}, clock frozen at {frozen.isoformat()}"
    )
    differences: list[dict[str, Any]] = []
    passes = {}
    for warm in (False, True):
        name = "warm (stored figures drawn inline)" if warm else "cold (every part asked for)"
        found = 0
        for observation, seconds in _pages(group, users, matrix, mode="check", warm=warm):
            diffs = compare(group, recorded[(observation["role"], observation["label"])], observation)
            for diff in diffs:
                diff["pass"] = "warm" if warm else "cold"
            differences.extend(diffs)
            found += len(diffs)
            inline = ",".join(observation["inline"]) or "-"
            print(
                f"  {name[:4]} {observation['role']:7} {observation['label']:44} {observation['status']} "
                f"{seconds * 1000:6.0f} ms  parts {len(observation['parts'])}  inline {inline:14} "
                f"differences {len(diffs)}"
            )
        passes[name] = found
    pages = len(users) * len(matrix)
    for name, found in passes.items():
        print(
            f"{recording['group']}: {len(matrix)} filters x {len(users)} users, {name}: {found} differences"
        )
    line = (
        f"{recording['group']}: {len(matrix)} filters x {len(users)} users, {len(differences)} differences "
        f"({pages} pages, each checked cold and warm)"
    )
    print(line)
    for diff in differences[:60]:
        print(f"  - [{diff['pass']}] {diff['role']} {diff['label']}: {diff['kind']} {diff['path']}")
        print(f"      recorded: {diff['recorded']}")
        print(f"      checked:  {diff['checked']}")
    if len(differences) > 60:
        print(f"  ... and {len(differences) - 60} more")
    if args.report:
        Path(args.report).write_text(
            json.dumps({"line": line, "checked": info, "differences": differences}, indent=1, default=str)
        )
    return 1 if differences else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.parity.core", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("record", "record on the base commit"), ("check", "check on the working tree")):
        command = sub.add_parser(name, help=helptext)
        command.add_argument("group")
        command.add_argument("out", help="the recording (written by record, read by check)")
        command.add_argument(
            "--checkout", type=Path, default=None, help="the code to run (default: this tree)"
        )
        command.add_argument(
            "--at", default=FROZEN_AT if name == "record" else None, help="the frozen moment (ISO, aware)"
        )
        if name == "check":
            command.add_argument("--report", help="write the differences to this JSON file")
    args = parser.parse_args(argv)
    return record(args) if args.command == "record" else check(args)


if __name__ == "__main__":
    # run the package's own copy of this module, so the group modules and this one share the frozen clock
    from scripts.parity import core as _core

    raise SystemExit(_core.main())
