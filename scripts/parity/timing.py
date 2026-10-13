"""Speed of a page group on the parity world (SPEC7 §3.6; review items 5d and 5e).

    python -m scripts.parity.timing pages <group> out.json [--checkout DIR] [--runs 3]
    python -m scripts.parity.timing load <group> out.json [--checkout DIR] [--users 8] [--rounds 2]
    python -m scripts.parity.timing first <username> <url> [--checkout DIR]     (one request, fresh process)

Run from the working tree's root, on a parity database (``DATABASE_URL``; see ``core``). Every command uses
``scripts.parity.settings``: ``DJANGO_ENV=local`` and ``DEBUG`` off, so the shared page figures are in the
database table, as in production, never in memory as in the tests. ``--checkout`` points at the base worktree
to time the old page the same way, in the same session. Requests ask for gzip, as a browser does.

**pages**, in one process (Django's test client), after the process's first requests (``/help/`` and one
page view per user: the targets leave a process's first request out). For each user and timed filter:

- the shell cold (every cache emptied: the per-process ones and the shared table) and warm (the same
  address again: its filter choices and freshness strip are stored, its figures are not), its queries, size;
- a page view: the shell, then its parts as ``parts.js`` asks for them (at most 3 at a time, 2 while a
  group's leader loads; the leader alone in its group; high first, then the visible normal and low ones,
  then the rest one at a time; strict parts only when opened), with a pool of 3 threads: the time to the
  first figure (the group's ``FIRST_FIGURE`` part arriving) and to the last part (strict ones left out);
- the warm page: the shell once its figures are stored (drawn inline: one request);
- the strict parts opened on demand, and the size of each value in the shared cache table.

"Visible" assumes a 1366 x 768 desktop: a placeholder whose top (``HEADER`` px of frame, then the reserved
heights before it) is within the viewport plus parts.js's 800 px margin. On a checkout without parts (the
base commit) the page is its own first figure, and what it loads by itself (``hx-trigger="load"`` in
``<main>``) are its other parts, asked for at once as htmx does.

**load** (review item 5d): gunicorn with the checkout's ``config/gunicorn.conf.py`` (3 workers x 4
threads), unchanged, plus a hook (``count_builds``) that logs each figure build a worker computes and keeps
GET requests from starting jobs; a free port; the in-app scheduler off. N signed-in users (half the
administrator, half the viewer) open the group's first page at the same moment, each as a browser does.
Rounds: cold with fresh workers, cold with workers that served ``/help/`` first, then warm. The shared cache
is emptied before each cold round. Reported: the shell's p50/p95, the first figure, the last part, the
builds, the 202 answers ("still being computed") and any error. Every server is stopped on the way out.
"""

from __future__ import annotations

import argparse
import atexit
import concurrent.futures as futures
import contextlib
import gzip
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.parity import core

SETTINGS = "scripts.parity.settings"
MAX_PARALLEL, LEADER_MAX = (
    3,
    2,
)  # parts.js: <meta name="nd-parts-parallel" content="3">, one less for a leader
PENDING_FIRST, PENDING_MAX, BUDGET = 1.5, 6.0, 60.0  # parts.js: a 202 is asked again after 1.5 s, backing off
VIEWPORT, NEAR, HEADER, REM, VH = 768, 800, 320, 16.0, 7.68  # a 1366 x 768 desktop
TARGETS = {"shell cold": 150.0, "shell warm": 80.0, "load shell p95": 300.0}
HEIGHT = re.compile(r"--part-h:\s*(?P<n>\d+(?:\.\d+)?)(?P<unit>px|rem|vh)")
ACCEPT = {"HTTP_ACCEPT_ENCODING": "gzip, deflate, br"}

_servers: list[subprocess.Popen] = []


# ------------------------------------------------------------------------------------------ a page view
@dataclass
class Answer:
    status: int
    ms: float
    size: int  # bytes sent (compressed)
    body: str
    queries: int = 0
    pending: int = 0


@dataclass
class Item:
    name: str
    url: str
    element_id: str
    order: int
    tier: int
    group: str | None
    strict: bool
    visible: bool
    state: str = "queued"


class Queue:
    """``createQueue`` of parts.js, as far as one page view needs it (no bumps, retries or anchors)."""

    def __init__(self, items: list[Item], max_parallel: int = MAX_PARALLEL, leader_max: int = LEADER_MAX):
        self.items = items
        self.max, self.leader_max = max_parallel, leader_max
        self.opened = {item.group: False for item in items if item.group}
        self.loading: set[str] = set()

    def _members(self, group: str | None) -> list[Item]:
        return [item for item in self.items if item.group == group]

    def _open(self, item: Item) -> bool:
        return not item.group or self.opened[item.group]

    def _leader(self, item: Item) -> bool:
        return not self._open(item) and min(self._members(item.group), key=_place) is item

    def _wanted_self(self, item: Item) -> bool:
        return item.visible or (item.tier == 0 and not item.strict)

    def _wanted(self, item: Item) -> bool:
        if self._wanted_self(item):
            return True
        return self._leader(item) and any(self._wanted_self(m) for m in self._members(item.group))

    def _choose(self) -> Item | None:
        queued = sorted(
            (i for i in self.items if i.state == "queued" and (self._open(i) or self._leader(i))), key=_place
        )
        wanted = [item for item in queued if self._wanted(item)]
        if wanted:
            return wanted[0]
        if self.loading:
            return None
        for item in queued:  # prefetch, one at a time
            if not item.strict or (
                self._leader(item) and any(not m.strict for m in self._members(item.group))
            ):
                return item
        return None

    def _limit(self) -> int:
        leader_loading = any(not self._open(i) for i in self.items if i.name in self.loading)
        return self.leader_max if leader_loading else self.max

    def next(self) -> list[Item]:
        out = []
        while len(self.loading) < self._limit():
            item = self._choose()
            if item is None:
                break
            if self._leader(item) and len(self.loading) >= self.leader_max:
                break
            item.state = "loading"
            self.loading.add(item.name)
            out.append(item)
        return out

    def done(self, item: Item) -> None:
        if item.group:
            self.opened[item.group] = True
        self.loading.discard(item.name)
        item.state = "done"


def _place(item: Item) -> tuple[int, int]:
    return item.tier, item.order


def _px(style: str) -> float:
    found = HEIGHT.search(style or "")
    if not found:
        return 128.0  # .part's own min-height (8rem)
    n = float(found["n"])
    return n * {"px": 1.0, "rem": REM, "vh": VH}[found["unit"]]


def items_of(html: str) -> list[Item]:
    """The parts a shell asks for: its placeholders, then what ``<main>`` loads by itself (the old pages)."""
    out: list[Item] = []
    top = float(HEADER)
    for order, match in enumerate(core.PLACEHOLDER.finditer(html)):
        found = core.attributes(match.group(0))
        strict = found.get("data-part-lazy") == "strict"
        out.append(
            Item(
                name=match["name"],
                url=found.get("hx-get", ""),
                element_id=found.get("id") or f"part-{match['name']}",
                order=order,
                tier={"high": 0, "normal": 1, "low": 2}.get(found.get("data-part-priority", "normal"), 1),
                group=found.get("data-part-group") or None,
                strict=strict,
                visible=not strict and top < VIEWPORT + NEAR,
            )
        )
        top += _px(found.get("style", ""))
    main = core.MAIN.search(html)
    for order, match in enumerate(core.LOADS_ITSELF.finditer(main["body"] if main else ""), start=len(out)):
        found = core.attributes(match.group(0))
        out.append(
            Item(f"load-{order}", found.get("hx-get", ""), found.get("id", ""), order, 0, None, False, True)
        )
    return out


def page_view(
    get_shell: Callable[[], Answer],
    get_part: Callable[[Item], Answer],
    pool: futures.ThreadPoolExecutor,
    first_figure: str | None,
) -> dict[str, Any]:
    """One page view: the shell, then its parts through the queue. Times in ms from the start."""
    started = time.perf_counter()

    def now() -> float:
        return (time.perf_counter() - started) * 1000

    shell = get_shell()
    shell_at = now()
    items = items_of(shell.body) if shell.status == 200 else []
    inline = sorted(set(re.findall(r'data-part-loaded="([a-z][a-z0-9-]*)"', shell.body)))
    queue = Queue(items)
    arrivals: dict[str, float] = {}
    answers: dict[str, Answer] = {}
    pending = 0
    running: dict[futures.Future, tuple[Item, float, int]] = {}

    def ask(item: Item, delay: float) -> Answer:
        if delay:
            time.sleep(delay)
        return get_part(item)

    def submit(item: Item, delay: float = 0.0, waits: int = 0, fired: float | None = None) -> None:
        running[pool.submit(ask, item, delay)] = (item, now() if fired is None else fired, waits)

    for item in queue.next():
        submit(item)
    while running:
        done, _ = futures.wait(running, return_when=futures.FIRST_COMPLETED)
        for future in done:
            item, fired, waits = running.pop(future)
            answer = future.result()
            if answer.status == 202:
                pending += 1
                delay = min(PENDING_FIRST * 1.5**waits, PENDING_MAX)
                if (now() - fired) / 1000 + delay <= BUDGET:
                    submit(item, delay, waits + 1, fired)  # the slot stays taken, as in parts.js
                    continue
            arrivals[item.name] = now()
            answers[item.name] = answer
            queue.done(item)
        for item in queue.next():
            submit(item)
    if first_figure is None or not any(i.name == first_figure for i in items):
        first = shell_at  # the old page (one response), or the figure drawn inline
    else:
        first = arrivals.get(first_figure, float("nan"))
    return {
        "shell": {
            "status": shell.status,
            "ms": round(shell.ms, 1),
            "size": shell.size,
            "queries": shell.queries,
        },
        "shell_at": round(shell_at, 1),
        "first_figure": round(first, 1),
        "last_part": round(max(arrivals.values(), default=shell_at), 1),
        "inline": inline,
        "parts": {
            name: {
                "status": a.status,
                "ms": round(a.ms, 1),
                "at": round(arrivals[name], 1),
                "size": a.size,
                "queries": a.queries,
            }
            for name, a in answers.items()
        },
        "strict": [i.name for i in items if i.strict],
        "pending": pending,
        "bodies": {name: a.body for name, a in answers.items()},
    }


# ----------------------------------------------------------------------------------------- in process
class _Clients(threading.local):
    client: Any = None


class InProcess:
    """Requests through Django's test client, one client per thread, the session cookie of the user."""

    def __init__(self) -> None:
        self.local = _Clients()
        self.cookies: Any = None

    def client(self):
        from django.test import Client

        if self.local.client is None:
            self.local.client = Client(raise_request_exception=False)
        self.local.client.cookies = self.cookies.__class__(self.cookies)
        return self.local.client

    def get(self, path: str, *, target: str | None = None, page: str = "/") -> Answer:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        extra = dict(ACCEPT)
        if target is not None:
            extra.update(
                HTTP_HX_REQUEST="true",
                HTTP_HX_CURRENT_URL=core.SERVER + page,
                HTTP_HX_TARGET=target,
                HTTP_HX_TRIGGER=target,
            )
        client = self.client()
        with CaptureQueriesContext(connection) as queries:
            started = time.perf_counter()
            response = client.get(path, **extra)
            ms = (time.perf_counter() - started) * 1000
        raw = response.content
        body = gzip.decompress(raw) if response.get("Content-Encoding") == "gzip" else raw
        return Answer(response.status_code, ms, len(raw), body.decode("utf-8", "replace"), len(queries))


def _median(values: list[float]) -> float:
    values = [v for v in values if v == v]
    return round(statistics.median(values), 1) if values else float("nan")


def stored_values() -> list[tuple[str, int]]:
    """(key, bytes) of each value in the shared cache table, when the checkout has one."""
    from django.conf import settings
    from django.db import connection

    shared = settings.CACHES.get("shared", {})
    if not shared.get("BACKEND", "").endswith("DatabaseCache"):
        return []
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT cache_key, length(value) FROM "{shared["LOCATION"]}" ORDER BY 2 DESC')  # noqa: S608
        return [(key, size) for key, size in cursor.fetchall()]


def time_pages(args: argparse.Namespace) -> int:
    info = core.setup(args.checkout, settings_module=SETTINGS, contexts=False)
    from django.conf import settings

    group = core.load_group(args.group)
    shared = settings.CACHES.get("shared", {}).get("BACKEND", "(none)")
    people = group.users()
    matrix = [(label, url) for label, url in group.matrix(people) if label in group.TIMED]
    print(
        f"timing {args.group} on {info['checkout']} ({info['commit'][:9]}, {info['changed_files']} changed files)"
    )
    print(
        f"database {info['database']}, DJANGO_ENV={settings.ENV}, DEBUG={settings.DEBUG}, shared cache {shared}"
    )
    browser = InProcess()
    pool = futures.ThreadPoolExecutor(MAX_PARALLEL, thread_name_prefix="part")
    cookies = {role: core.signed_in_client(user).cookies for role, user in people.items()}
    first = getattr(group, "FIRST_FIGURE", None)

    def view(url: str) -> dict[str, Any]:
        return page_view(
            lambda: browser.get(url),
            lambda item: browser.get(item.url, target=item.element_id, page=url),
            pool,
            first,
        )

    for role in people:  # the process's first requests, left out of the targets
        browser.cookies = cookies[role]
        browser.get("/help/")
        view(matrix[0][1])
    core.clear_caches()
    rows: dict[str, Any] = {}
    for role in people:
        browser.cookies = cookies[role]
        for label, url in matrix:
            row: dict[str, Any] = {"url": url}
            cold = []
            for _ in range(args.runs):
                core.clear_caches()
                cold.append(browser.get(url))
            warm = [browser.get(url) for _ in range(args.runs)]
            views = []
            for _ in range(args.runs):
                core.clear_caches()
                views.append(view(url))
            stored = stored_values()
            inline = [browser.get(url) for _ in range(args.runs)]
            inline_parts = sorted(set(re.findall(r'data-part-loaded="([a-z][a-z0-9-]*)"', inline[-1].body)))
            on_demand: dict[str, Any] = {}
            last = views[-1]
            for name, body in last["bodies"].items():
                for item in items_of(body):
                    if item.strict:
                        answers = [
                            browser.get(item.url, target=item.element_id, page=url) for _ in range(args.runs)
                        ]
                        on_demand[item.name] = {
                            "ms": _median([a.ms for a in answers]),
                            "queries": answers[-1].queries,
                            "size": answers[-1].size,
                            "inside": name,
                        }
            row.update(
                shell_cold_ms=_median([a.ms for a in cold]),
                shell_cold_runs=[round(a.ms) for a in cold],
                shell_cold_queries=cold[-1].queries,
                shell_warm_ms=_median([a.ms for a in warm]),
                shell_warm_queries=warm[-1].queries,
                shell_size=cold[-1].size,
                first_figure_ms=_median([v["first_figure"] for v in views]),
                last_part_ms=_median([v["last_part"] for v in views]),
                view_runs=[[round(v["first_figure"]), round(v["last_part"])] for v in views],
                parts={
                    name: {
                        **part,
                        "ms": _median([v["parts"][name]["ms"] for v in views if name in v["parts"]]),
                    }
                    for name, part in last["parts"].items()
                },
                warm_page_ms=_median([a.ms for a in inline]),
                warm_page_queries=inline[-1].queries,
                warm_page_size=inline[-1].size,
                warm_page_inline=inline_parts,
                on_demand=on_demand,
                pending=sum(v["pending"] for v in views),
                stored=stored,
            )
            rows[f"{role} {label}"] = row
            print(
                f"{role:6} {label:16} shell cold {row['shell_cold_ms']:6.0f} ms ({row['shell_cold_queries']} q, "
                f"{row['shell_size'] / 1024:.0f} KB) warm {row['shell_warm_ms']:5.0f} ms ({row['shell_warm_queries']} q)"
                f" | first figure {row['first_figure_ms']:6.0f} ms, last part {row['last_part_ms']:6.0f} ms"
                f" | warm page {row['warm_page_ms']:5.0f} ms ({row['warm_page_queries']} q, "
                f"inline {','.join(inline_parts) or '-'})",
                flush=True,
            )
            for name, part in row["parts"].items():
                print(
                    f"         part {name:12} {part['ms']:6.0f} ms  {part['queries']:3} q  {part['size'] / 1024:6.1f} KB"
                )
            for name, part in on_demand.items():
                print(
                    f"         on demand {name:7} {part['ms']:6.0f} ms  {part['queries']:3} q  {part['size'] / 1024:6.1f} KB"
                )
    pool.shutdown()
    first_requests = []
    for role, user in people.items():
        done = subprocess.run(  # noqa: S603 - this module, fixed arguments
            [
                sys.executable,
                "-m",
                "scripts.parity.timing",
                "first",
                user.username,
                matrix[0][1],
                "--checkout",
                info["checkout"],
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=core.REPO,
            env={**os.environ, "PYTHONPATH": str(core.REPO)},
        )
        lines = done.stdout.strip().splitlines()
        first_requests.append(
            json.loads(lines[-1]) if done.returncode == 0 and lines else {"error": done.stderr[-2000:]}
        )
        print(f"fresh process, {role}: {first_requests[-1]}")
    Path(args.out).write_text(
        json.dumps(
            {
                "timed": info,
                "settings": {"env": settings.ENV, "debug": settings.DEBUG, "shared": shared},
                "runs": args.runs,
                "targets": TARGETS,
                "pages": rows,
                "first_requests": first_requests,
            },
            indent=1,
            default=str,
        )
    )
    return 0


def first_request(args: argparse.Namespace) -> int:
    """One request in a fresh process (what a worker's first visitor waits for), then the same again."""
    started = time.perf_counter()
    core.setup(args.checkout, settings_module=SETTINGS, contexts=False)
    setup_ms = (time.perf_counter() - started) * 1000
    from neurodb.accounts.models import User

    browser = InProcess()
    browser.cookies = core.signed_in_client(User.objects.get(username=args.username)).cookies
    one, two = browser.get(args.url), browser.get(args.url)
    print(
        json.dumps(
            {
                "user": args.username,
                "url": args.url,
                "setup_ms": round(setup_ms),
                "first_ms": round(one.ms),
                "first_queries": one.queries,
                "status": one.status,
                "again_ms": round(two.ms),
                "again_queries": two.queries,
            }
        )
    )
    return 0


# ---------------------------------------------------------------------------------------------- the load check
WRAPPER = '''"""Written by scripts/parity/timing.py for one load check: the checkout's own gunicorn settings, unchanged,
plus a log of the figure builds each worker computes."""
import os as _os

_conf = _os.path.join(_os.getcwd(), "config", "gunicorn.conf.py")
with open(_conf) as _fh:
    exec(compile(_fh.read(), _conf, "exec"))  # noqa: S102
_checkout_post_worker_init = globals().get("post_worker_init")


def post_worker_init(worker):
    if _checkout_post_worker_init is not None:
        _checkout_post_worker_init(worker)
    from scripts.parity.timing import count_builds

    count_builds(_os.environ["PARITY_BUILD_LOG"])
'''


def count_builds(log_path: str) -> None:
    """In a gunicorn worker: log every overview build (and filter-choices and freshness computation, where the
    checkout has them) to ``log_path``, one JSON line each; and never start a background job from a GET."""
    from neurodb.integrations import background
    from neurodb.reports import overview

    background.start_command = lambda *args, **kwargs: 0
    guard = threading.Lock()

    def wrap(owner: Any, name: str, what: str, scope: Callable[[tuple], str]) -> None:
        real = getattr(owner, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return real(*args, **kwargs)
            finally:
                line = {
                    "pid": os.getpid(),
                    "what": what,
                    "scope": scope(args),
                    "at": time.time(),
                    "ms": round((time.perf_counter() - started) * 1000),
                }
                with guard, open(log_path, "a") as fh:
                    fh.write(json.dumps(line) + "\n")

        setattr(owner, name, counted)

    def build_scope(args: tuple) -> str:
        scope = args[0].scope
        return f"{scope.year}|{','.join(scope.sections) or '*'}|{scope.governorate or '*'}"

    wrap(overview._Builder, "build", "build", build_scope)
    # the frame's cached pieces (the working tree only): the filter bar's stored sections (_sections; _options
    # before the governorates left the stored value) and the freshness strip
    options = "_sections" if hasattr(overview, "_sections") else "_options"
    for name, what in ((options, "options"), ("_freshness", "freshness")):
        if hasattr(overview, name):
            wrap(overview, name, what, lambda args: "")
    # "missed": a shared value not found stored, the wait for its lock plus its computation (the working tree)
    with contextlib.suppress(ImportError):
        from neurodb.core import compute

        wrap(compute, "_compute_once", "missed", lambda args: str(args[0]).split(":")[1][:40])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(server: subprocess.Popen) -> None:
    if server.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(server.pid, signal.SIGTERM)
        try:
            server.wait(timeout=60)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(server.pid, signal.SIGKILL)
            server.wait(timeout=10)
    if server in _servers:
        _servers.remove(server)


@atexit.register
def _stop_all() -> None:
    for server in list(_servers):
        _stop(server)


class Http:
    """Requests to the gunicorn server, with a user's session cookie, asking for gzip as a browser does."""

    def __init__(self, base: str, cookie: str) -> None:
        self.base, self.cookie = base, cookie

    def get(self, path: str, *, target: str | None = None, page: str = "/") -> Answer:
        headers = {"Cookie": f"sessionid={self.cookie}", "Accept-Encoding": "gzip"}
        if target is not None:
            headers.update(
                {
                    "HX-Request": "true",
                    "HX-Current-URL": self.base + page,
                    "HX-Target": target,
                    "HX-Trigger": target,
                }
            )
        request = urllib.request.Request(self.base + path, headers=headers)  # noqa: S310 - the local server
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                raw, status, encoding = (
                    response.read(),
                    response.status,
                    response.headers.get("Content-Encoding"),
                )
        except urllib.error.HTTPError as error:
            raw, status, encoding = error.read(), error.code, error.headers.get("Content-Encoding")
        ms = (time.perf_counter() - started) * 1000
        body = gzip.decompress(raw) if encoding == "gzip" else raw
        return Answer(status, ms, len(raw), body.decode("utf-8", "replace"))


def _start_server(checkout: Path, port: int, wrapper: Path, log: Path, errors: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PORT": str(port),
        "SCHEDULER_ENABLED": "false",
        "ALLOWED_HOSTS": "127.0.0.1,localhost",
        "GUNICORN_LOG_LEVEL": "warning",
        "DJANGO_ENV": "local",
        "DJANGO_SETTINGS_MODULE": SETTINGS,
        "PYTHONPATH": str(core.REPO),
        "PARITY_BUILD_LOG": str(log),
    }
    with open(errors, "ab") as err:
        server = subprocess.Popen(  # noqa: S603 - fixed command
            [sys.executable, "-m", "gunicorn", "config.wsgi:application", "-c", str(wrapper)],
            cwd=checkout,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
    _servers.append(server)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"gunicorn stopped at start (exit {server.returncode}); see {errors}")
        try:
            urllib.request.urlopen(f"{base}/healthz/", timeout=2)  # noqa: S310
            break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.2)
    else:
        _stop(server)
        raise SystemExit(f"gunicorn did not answer within 90 s; see {errors}")
    time.sleep(3.0)  # every worker has loaded the application (post_worker_init ran)
    return server


def _p(values: list[float], share: float) -> float:
    ordered = sorted(v for v in values if v == v)
    if not ordered:
        return float("nan")
    return round(ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))], 1)


def _round(base: str, cookies: dict[str, str], url: str, users: int, first_figure: str | None) -> list[dict]:
    barrier = threading.Barrier(users)
    rows: list[dict[str, Any]] = [{} for _ in range(users)]

    def user(index: int) -> None:
        role = "admin" if index % 2 == 0 else "viewer"
        http = Http(base, cookies[role])
        with futures.ThreadPoolExecutor(MAX_PARALLEL) as pool:
            barrier.wait()
            try:
                view = page_view(
                    lambda: http.get(url),
                    lambda item: http.get(item.url, target=item.element_id, page=url),
                    pool,
                    first_figure,
                )
            except Exception as error:  # noqa: BLE001 - reported in the round's errors
                rows[index] = {"role": role, "error": repr(error)}
                return
        view.pop("bodies")
        rows[index] = {"role": role, **view}

    threads = [threading.Thread(target=user, args=(i,)) for i in range(users)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return rows


def _summary(label: str, rows: list[dict], builds: list[dict]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    shells = [r["shell"]["ms"] for r in ok]
    first = [r["first_figure"] for r in ok]
    last = [r["last_part"] for r in ok]
    statuses = sorted(
        {r["shell"]["status"] for r in ok} | {p["status"] for r in ok for p in r["parts"].values()}
    )
    scopes: dict[str, int] = {}
    for line in builds:
        if line["what"] == "build":
            scopes[line["scope"]] = scopes.get(line["scope"], 0) + 1
    return {
        "round": label,
        "users": len(rows),
        "errors": [r["error"] for r in rows if "error" in r],
        "shell_p50": _p(shells, 0.5),
        "shell_p95": _p(shells, 0.95),
        "shell_max": _p(shells, 1.0),
        "first_figure_p50": _p(first, 0.5),
        "first_figure_p95": _p(first, 0.95),
        "last_part_p50": _p(last, 0.5),
        "last_part_p95": _p(last, 0.95),
        "statuses": statuses,
        "pending_202": sum(r["pending"] for r in ok),
        "builds": sum(scopes.values()),
        "builds_by_scope": scopes,
        "build_ms": [line["ms"] for line in builds if line["what"] == "build"],
        "options_computed": sum(1 for line in builds if line["what"] == "options"),
        "freshness_computed": sum(1 for line in builds if line["what"] == "freshness"),
        "computed": [[line["what"], line["scope"], line["ms"]] for line in builds],
        "rows": rows,
    }


def load(args: argparse.Namespace) -> int:
    info = core.setup(args.checkout, settings_module=SETTINGS, contexts=False)
    group = core.load_group(args.group)
    people = group.users()
    cookies = {role: core.signed_in_client(user).cookies["sessionid"].value for role, user in people.items()}
    url = group.matrix(people)[0][1]
    first = getattr(group, "FIRST_FIGURE", None)
    checkout = Path(info["checkout"])
    print(
        f"load check of {args.group} on {checkout} ({info['commit'][:9]}), database {info['database']}, "
        f"{args.users} users at once on {url}",
        flush=True,
    )
    with open("/proc/loadavg") as fh:
        before = fh.read().strip()
    results = []
    with tempfile.TemporaryDirectory(prefix="parity-load-") as tmp:
        wrapper = Path(tmp) / "gunicorn_parity.py"
        wrapper.write_text(WRAPPER)
        errors = Path(tmp) / "gunicorn.err"
        plan = [(False, n) for n in range(args.rounds)] + [(True, n) for n in range(args.rounds)]
        for warmed, n in plan:
            log = Path(tmp) / f"builds-{len(results)}.jsonl"
            with contextlib.suppress(Exception):
                core.clear_caches()  # the shared table, the same database the server reads
            port = _free_port()
            server = _start_server(checkout, port, wrapper, log, errors)
            base = f"http://127.0.0.1:{port}"
            try:
                if warmed:
                    helpers = Http(base, cookies[next(iter(cookies))])
                    for _ in range(2):
                        threads = [threading.Thread(target=helpers.get, args=("/help/",)) for _ in range(12)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join()
                    time.sleep(0.5)
                rounds = [f"cold {n + 1}, " + ("workers warmed on /help/" if warmed else "fresh workers")]
                if warmed and n == args.rounds - 1:
                    rounds.append("warm (the same again, nothing emptied)")
                for label in rounds:
                    seen = log.read_text().splitlines() if log.exists() else []
                    rows = _round(base, cookies, url, args.users, first)
                    lines = log.read_text().splitlines() if log.exists() else []
                    summary = _summary(label, rows, [json.loads(x) for x in lines[len(seen) :]])
                    results.append(summary)
                    print(
                        f"{label:40} shell p50 {summary['shell_p50']:6.0f} p95 {summary['shell_p95']:6.0f} | first "
                        f"figure p50 {summary['first_figure_p50']:6.0f} p95 {summary['first_figure_p95']:6.0f} | last "
                        f"part p50 {summary['last_part_p50']:6.0f} p95 {summary['last_part_p95']:6.0f} | builds "
                        f"{summary['builds']} {summary['builds_by_scope']} options {summary['options_computed']} "
                        f"freshness {summary['freshness_computed']} | 202s {summary['pending_202']} | statuses "
                        f"{summary['statuses']} {summary['errors'] or ''}",
                        flush=True,
                    )
            finally:
                _stop(server)
        server_errors = errors.read_text()[-4000:] if errors.exists() else ""
    with open("/proc/loadavg") as fh:
        after = fh.read().strip()
    print(f"load average before {before}, after {after}")
    Path(args.out).write_text(
        json.dumps(
            {
                "checked": info,
                "url": url,
                "users": args.users,
                "load_before": before,
                "load_after": after,
                "results": results,
                "server_errors": server_errors,
            },
            indent=1,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.parity.timing", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pages = sub.add_parser("pages", help="shell, first figure and last part, in one process")
    pages.add_argument("group")
    pages.add_argument("out")
    pages.add_argument("--runs", type=int, default=3)
    loads = sub.add_parser("load", help="N users at once against gunicorn")
    loads.add_argument("group")
    loads.add_argument("out")
    loads.add_argument("--users", type=int, default=8)
    loads.add_argument("--rounds", type=int, default=2)
    once = sub.add_parser("first", help="one request in a fresh process")
    once.add_argument("username")
    once.add_argument("url")
    for command in (pages, loads, once):
        command.add_argument("--checkout", type=Path, default=None)
    args = parser.parse_args(argv)
    os.environ["DJANGO_ENV"] = "local"  # production's caches: the shared figures in the database table
    os.environ.setdefault("ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    return {"pages": time_pages, "load": load, "first": first_request}[args.command](args)


if __name__ == "__main__":
    from scripts.parity import timing as _timing  # the package's copy (the gunicorn hook imports it too)

    raise SystemExit(_timing.main())
