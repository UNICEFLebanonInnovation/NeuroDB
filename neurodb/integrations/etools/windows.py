"""A large eTools Datamart read in windows, each proven whole before the sync removes anything.

A read is windowed only when a cut is suspected: its first page announces 5,000 records or more and a round
count, or the dataset's previous finished run looked cut (``datamart_sync.cut_sign``). Every other read is
today's: the first page, then every ``next`` link. (The partner reports read on 20 and 21 Oct 2026 were whole:
777,535 and 780,905 announced, every one read, every country's rows; no limit near 10,000.) :class:`Reading`
reads a dataset exactly as :meth:`DatamartClient.list` does then, and a plain read of 5,000 records or more
asks the first and the last record by id before the rest of its pages (the *ends test*: one missing means the
answer was cut with its count). A read suspected cut is read in windows: ranges of reporting period start
(``date_key``, the partner reports) or of id, each announcing fewer than :data:`WINDOW_RECORDS`, once a
request that should find nothing proves the Datamart applies the window's filter. Every record must fall in
its window, every window give what it announced, each record once, and the whole count is asked again at the
end: only then is the read :attr:`Reading.whole` and may the sync remove what it no longer returns. A dataset
whose window filters are not proven is read once, as ever, and is not whole when its count is round (the
partner reports, read country-blind, hold more records than :data:`MAX_WINDOWS` windows can: a cut sign there
buys the proof requests and a plain read with its ends test, nothing more). A page shorter than the first one
of its request, not the last, is a cut too (``pages_cut``): the read is not whole, and the next one is
windowed.

Records that come twice while the pages are read (``pages_shifted``: the Datamart's default order moved rows
between pages, and as many others did not come, or it holds them twice) leave the read not whole. The next
read first proves a stable order (:func:`prove_ordering`: two pages in order and the reverse on another, by
``id`` or by the dataset's record identity) and asks every page in it; records that still come twice then are
most likely held twice by the Datamart (``held_twice``). :meth:`Reading.details` keeps counts, dates and ids
only.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import parse_iso_date
from neurodb.integrations.http import IntegrationError

# Code, not settings: the administrators have no command line.
WINDOW_RECORDS = (
    5_000  # a read suspected cut is windowed from this many records, a window split while it has more
)
MAX_WINDOWS = 100  # 100 windows below 5,000 records hold up to 500,000
MAX_COUNTS = (
    250  # the one-record requests of one read (proofs, id top search, plan and splits), then it stops
)
FAR_FUTURE, FAR_PAST = "2999-01-01", "1900-01-01"  # dates no record has: a filter applied finds nothing
ID_TOP = 10**15  # an id no record has
ID_FIRST_TOP = 2**20  # the first top of the id windows, multiplied by 4 while records lie above it
WINDOWS_KEPT = 60  # the windows listed in a run's details
BOUNDARY = dt.timedelta(days=1)  # a date this close outside its window is a time zone, not a record outside
ROUND_COUNT = "round_count"  # why a read is windowed when its count is a limit (``windows_why``)
ORDER_PAGE = 50  # the records of each of the three pages a stable order is proven on (:func:`prove_ordering`)


class _OutOfCounts(Exception):
    """:data:`MAX_COUNTS` count requests were made: the plan stops."""


@dataclass
class Window:
    """One range of a windowed read, ``[start, end)``: of start dates (``days``; ``end`` None for the open
    last one), of ids (``ids``), or both (a one-day window split by id). ``planned``: what its count request
    announced; the rest is what its read found."""

    days: tuple[dt.date, dt.date | None] | None = None
    ids: tuple[int, int] | None = None
    planned: int | None = None
    announced: int | None = None  # the count its read's first page announced
    read: int = 0  # distinct records, first seen in this window
    repeated: int = 0  # records returned again (in this window or an earlier one)
    outside: int = 0  # records whose date or id is not in the window
    pages: int = 0
    last_page: int = 0
    short_pages: int = 0  # pages, not the last, holding fewer records than the first page of its read
    too_large: bool = False  # could not be split below WINDOW_RECORDS
    done: bool = False  # read to its last page

    @property
    def whole(self) -> bool:
        """Read whole: its count is planned and not a limit, its read's first page announced the same (no
        change between the two), every record announced came once, and none outside it. A window planned
        empty is not read: its count answered it."""
        if self.planned == 0 and not self.too_large:
            return True
        return (
            self.done
            and self.planned is not None
            and not self.too_large
            and self.announced == self.planned
            and self.outside == 0
            and sync.read_whole(self.announced, self.read, self.repeated)
        )

    def bounds(self) -> tuple[str, str]:
        """(from, to) in words for the details: dates as ISO text ("" for the open end), else ids."""
        if self.days:
            start, end = self.days
            return start.isoformat(), end.isoformat() if end else ""
        if self.ids:
            return str(self.ids[0]), str(self.ids[1])
        return "", ""

    def summary(self) -> dict[str, Any]:
        start, end = self.bounds()
        if self.days and self.ids:  # a one-day window split by id
            start, end = f"{start} id {self.ids[0]}", f"{start} id {self.ids[1]}"
        return {
            "from": start,
            "to": end,
            "announced": self.planned,
            "read": self.read,
            "pages": self.pages,
            "outside": self.outside,
            "whole": self.whole,
        }


def _page(length: int, first_length: int) -> int:
    """1 for a page followed by another that holds fewer records than the first page of its request (a page
    the Datamart cut), else 0. The last page is never one: it holds what is left."""
    return 1 if length < first_length else 0


def _suspicion(count: int | None, before: Callable[[], dict[str, Any]]) -> str:
    """Why a read whose first page announces ``count`` is windowed (``windows_why``): a round count (a limit
    of the answer), else, from :data:`WINDOW_RECORDS` records, the cut sign of the previous finished run
    (``before``, its details, read only then: ``datamart_sync.cut_sign``); "" for a plain read."""
    if count is not None and sync.capped(count):
        return ROUND_COUNT
    if count is None or count < WINDOW_RECORDS:
        return ""
    return sync.cut_sign(before())


def suspected(name: str, count: int | None) -> str:
    """Why the sync would window the read of ``name`` whose first page announces ``count`` tonight, as
    :class:`Reading` decides it ("" when it reads it plain): for the probe."""
    return _suspicion(count, lambda: sync.last_details(name))


def order_wanted(before: dict[str, Any]) -> bool:
    """Whether a read proves a stable order first: the previous finished run of its dataset (``before``, its
    details) had records twice (``duplicates_read``), or asked an order (``ordering.key``)."""
    ordering = before.get("ordering") if isinstance(before, dict) else None
    asked = isinstance(ordering, dict) and bool(ordering.get("key"))
    return bool(isinstance(before, dict) and before.get("duplicates_read")) or asked


def _order_value(value: Any) -> tuple[int, Any] | None:
    """A value as an order compares it: a number as a number, else its text; None when missing."""
    if value in (None, "") or isinstance(value, bool | dict | list):
        return None
    text = str(value).strip()
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def _order_proof(
    client: DatamartClient, dataset: str, params: dict[str, Any], country: bool, key: str
) -> dict[str, Any]:
    """One key's proof (:func:`prove_ordering`): its verdict and what its three pages showed, as counts."""
    asked = {**params, "ordering": key, "page_size": ORDER_PAGE}
    try:
        count, first = client.first_page(dataset, asked, country=country)
        if count is not None and count <= 2 * ORDER_PAGE:
            return {"key": key, "verdict": "too_small", "count": count}
        second = client.first_page(dataset, {**asked, "page": 2}, country=country)[1]
        reverse = client.first_page(dataset, {**asked, "ordering": f"-{key}"}, country=country)[1]
    except IntegrationError as exc:
        refused = exc.status is not None and 400 <= exc.status < 500
        return {"key": key, "verdict": "refused" if refused else "failed", "status": exc.status}
    pages = (first, second, reverse)
    seen = [{sync.record_key(item) for item in page} for page in pages]  # by id, else by content
    shared, reverse_shared = len(seen[0] & seen[1]), len(seen[0] & seen[2])
    values = [[_order_value(item.get(key)) for item in page] for page in pages]
    carried = all(v is not None for page in values for v in page)
    in_order = not carried or (
        values[0] == sorted(values[0])
        and values[1] == sorted(values[1])
        and values[2] == sorted(values[2], reverse=True)
        and (not values[0] or not values[1] or values[0][-1] <= values[1][0])
    )
    proven = all(pages) and not shared and not reverse_shared and in_order
    return {
        "key": key,
        "verdict": "proven" if proven else "ignored",
        "count": count,
        "pages": [len(page) for page in pages],
        "shared": shared,
        "reverse_shared": reverse_shared,
        "carried": carried,
        "in_order": in_order,
    }


def prove_ordering(
    client: DatamartClient,
    dataset: str,
    params: dict[str, Any],
    *,
    country: bool = True,
    keys: tuple[str, ...] = ("id",),
    known: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Whether the Datamart reads ``dataset`` (``params``, the read's own query) in a stable order, proven
    as the probe proves a filter, on one key after another (``keys``, the empty ones left out): page 1 and
    page 2 of ``ordering=<key>`` (:data:`ORDER_PAGE` records each) share no record and, when the records carry
    the key, each is in order and page 1 ends at or before page 2's start; page 1 of ``ordering=-<key>``
    shares no record with page 1 and, with the key, is in descending order (records with no id are told apart
    by their content). A dataset of ``2 × ORDER_PAGE`` records or fewer (``known``, the last read's count, or
    the first proof page's) is not asked: too small to prove, and to shift. A refused or failed request never
    fails the read: that key is not proven. Returns ``{"key": key}``, or ``{"key": "", "why": ..., "tried":
    keys}`` (``ignored``, ``refused``, ``failed`` or ``too_small``), and each key tried with what its pages
    showed (counts only)."""
    if isinstance(known, int) and not isinstance(known, bool) and known <= 2 * ORDER_PAGE:
        return {"key": "", "why": "too_small"}, []
    tried: list[dict[str, Any]] = []
    for key in dict.fromkeys(k for k in keys if k):
        found = _order_proof(client, dataset, params, country, key)
        tried.append(found)
        if found["verdict"] == "proven":
            return {"key": key}, tried
        if found["verdict"] == "too_small":
            return {"key": "", "why": "too_small"}, tried
    verdicts = [t["verdict"] for t in tried]
    why = "ignored" if "ignored" in verdicts or not verdicts else verdicts[0]
    return {"key": "", "why": why, "tried": [t["key"] for t in tried]}, tried


def _next_year(day: dt.date) -> dt.date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


class Reading:
    """The records of one dataset query (as :meth:`DatamartClient.list` yields them), read in windows when a
    cut is suspected (:attr:`why`), and whether the read is :attr:`whole`. ``name``, the dataset's sync
    target: its previous finished run says whether the last read looked cut, whether to ask a stable order
    first (:func:`order_wanted`, :attr:`ordering`) and the record identity its records without an id are told
    apart by (``record_identity``, ``datamart_sync.record_key``). Iterate it once; read :attr:`whole` and
    :meth:`details` after."""

    def __init__(
        self,
        client: DatamartClient,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        country: bool = True,
        date_key: str = "",
        today: dt.date | None = None,
        name: str = "",
    ) -> None:
        self.client, self.dataset, self.params = client, dataset, dict(params or {})
        self.country, self.date_key, self.name = country, date_key, name
        self.before: dict[str, Any] = {}  # the details of the previous finished run of ``name``, once read
        self.identity = ""  # the previous run's record identity: the records without an id counted by it
        self.ordering: dict[str, Any] = {}  # the stable order asked (``{"key": "id"}``), or why none
        self.order_tried: list[dict[str, Any]] = []
        self.why = ""  # why the read is windowed (``windows_why``): "" for a plain read
        self.today = today or timezone.localdate()
        self.announced: int | None = None  # C, the count of the first page
        self.keys: set[str] = set()
        self.repeated = 0
        self.repeated_same = 0  # records again that carry no id: identical to their first copy (their hash)
        self.windowed = False
        self.windows: list[Window] = []
        self.bound = ""  # the filter the windows are made of: date_key, "id" or "" (none proven)
        self.date_end = self.id_end = ""  # the end filter proven: "lt", or "lte" when the Datamart has no lt
        self.id_top: int | None = None
        self.count_requests = 0
        self.plan_complete = True
        self.short = False  # two halves announced fewer than their window
        self.cap_suspected = ""
        self.ends: list[str] = []  # the first and the last id, asked before the read (the ends test)
        self.ends_missing = 0
        self.whole_count_end: int | None = None
        self.end_check_failed = False
        self.done = False

    # ----------------------------------------------------------------------------------------- reading
    def __iter__(self) -> Iterator[dict[str, Any]]:
        self.before = sync.last_details(self.name) if self.name else {}
        self.identity = str(self.before.get("record_identity") or "")
        if order_wanted(self.before):  # the last read's records came twice: ask a stable order, once proven
            keys = ("id", self.identity)
            known = self.before.get("datamart_count")
            self.ordering, self.order_tried = prove_ordering(
                self.client, self.dataset, self.params, country=self.country, keys=keys, known=known
            )
            if self.ordering.get("key"):
                self.params["ordering"] = self.ordering["key"]
        pages = self.client.pages(self.dataset, self.params, country=self.country)
        count, first = next(pages)
        self.announced = count
        self.why = self._why(count)
        if not self.why:  # no cut suspected: today's read, with the ends test when it is large
            if count is not None and count >= WINDOW_RECORDS:
                self._ask_ends()
            yield from self._plain(count, first, pages)
            self._ends_test()
            return
        self.windowed = True
        try:
            plan = self._plan()
        except _OutOfCounts:  # spent on the proofs: no window to read
            plan, self.bound = None, ""
        if plan is None:  # no window filter proven: one read as ever, the first page's records included
            self._ask_ends()
            yield from self._plain(count, first, pages)
            if sync.capped(count):
                self.cap_suspected = self.cap_suspected or "windows_unproven"
            self._ends_test()
            return
        pages.close()
        self.windows = plan
        for window in plan:
            if window.planned == 0 and not window.too_large:
                continue  # its count already answered it
            yield from self._read(window)
        try:
            self.whole_count_end = self._count({})
        except (IntegrationError, _OutOfCounts):
            self.end_check_failed = True
        self.done = True

    def _why(self, count: int | None) -> str:
        """Why this read is windowed: :func:`_suspicion` over the previous run's details (:attr:`before`)."""
        return _suspicion(count, lambda: self.before)

    def _large(self, count: int | None) -> bool:
        """A window to split: it announces :data:`WINDOW_RECORDS` or more, or a round count."""
        return count is not None and (count >= WINDOW_RECORDS or sync.capped(count))

    def _take(self, window: Window, item: dict[str, Any]) -> None:
        key = sync.record_key(item, self.identity)
        if key in self.keys:
            window.repeated += 1
            self.repeated += 1
            self.repeated_same += item.get("id") in (None, "") and key == sync.content_key(item)
        else:
            self.keys.add(key)
            window.read += 1
        if not self._inside(window, item):
            window.outside += 1

    def _plain(self, count, first, pages) -> Iterator[dict[str, Any]]:
        """Today's read: the first page, then every ``next`` link."""
        window = Window(planned=count, announced=count, pages=1, last_page=len(first))
        self.windows = [window]
        for item in first:
            self._take(window, item)
            yield item
        for _, results in pages:
            if window.pages > 1:  # the page before this one is not the last: was it cut?
                window.short_pages += _page(window.last_page, len(first))
            window.pages, window.last_page = window.pages + 1, len(results)
            for item in results:
                self._take(window, item)
                yield item
        window.done = self.done = True
        if count is None and self.read >= sync.CAP_FLOOR:
            # no count to compare with, and the answer stops on a full page: it may be cut. A page longer than
            # the page size asked is an unpaginated list, with no page to be full: a round length then
            size, last = self.client.page_size, window.last_page
            if sync.capped(last) if last > size else last >= size:
                self.cap_suspected = "no_count"

    def _read(self, window: Window) -> Iterator[dict[str, Any]]:
        first = 0  # the records on the first page of the window's read
        for count, results in self.client.pages(
            self.dataset, {**self.params, **self._filters(window)}, country=self.country
        ):
            if not window.pages:
                window.announced, first = count, len(results)
            elif window.pages > 1:  # the page before this one is not the last: was it cut?
                window.short_pages += _page(window.last_page, first)
            window.pages, window.last_page = window.pages + 1, len(results)
            for item in results:
                self._take(window, item)
                yield item
        window.done = True

    def _inside(self, window: Window, item: dict[str, Any]) -> bool:
        if window.days:
            try:
                day = parse_iso_date(item.get(self.date_key))
            except ValueError:
                day = None
            start, end = window.days
            if day is None or day < start - BOUNDARY or (end is not None and day > end):
                return False
        if window.ids:
            pk = sync._int(item.get("id"))
            if pk is None or not window.ids[0] <= pk < window.ids[1]:
                return False
        return True

    def _ask_ends(self) -> None:
        """The ends test's requests, asked once the first page is read and before the rest: the first record
        by id and the last (one record each). Only records that already exist are asked, so a record added
        during the read is never taken for one missing. No verdict when the records carry no id or the
        Datamart ignores the ordering (both ends are then the same record), nor when a request fails."""
        try:
            ends = [
                self.client.first_page(
                    self.dataset, {**self.params, "ordering": order, "page_size": 1}, country=self.country
                )[1]
                for order in ("id", "-id")
            ]
        except IntegrationError:
            return
        firsts = [str(page[0].get("id")) for page in ends if page and page[0].get("id") not in (None, "")]
        if len(firsts) == 2 and firsts[0] != firsts[1]:
            self.ends = firsts

    def _ends_test(self) -> None:
        """Once read: when either end asked (:meth:`_ask_ends`) was not returned, the answer was cut."""
        self.ends_missing = sum(1 for pk in self.ends if pk not in self.keys)
        if self.ends_missing:
            self.cap_suspected = self.cap_suspected or "ends_missing"

    # ------------------------------------------------------------------------------------------ planning
    def _count(self, filters: dict[str, Any]) -> int | None:
        if self.count_requests >= MAX_COUNTS:
            raise _OutOfCounts
        self.count_requests += 1
        return self.client.count(self.dataset, {**self.params, **filters}, country=self.country)

    def _finds_nothing(self, filters: dict[str, Any]) -> bool:
        """A request that should find nothing finds nothing: the filter is applied. A refused or failing
        request counts as not proven; it never fails the run."""
        try:
            return self._count(filters) == 0
        except IntegrationError:
            return False

    def _prove(self, key: str, beyond: Any, before: Any, before_lte: Any) -> str:
        """The end filter of ``key`` the Datamart applies ("lt", else "lte"), once ``__gte`` is proven; ""."""
        if not self._finds_nothing({f"{key}__gte": beyond}):
            return ""
        if self._finds_nothing({f"{key}__lt": before}):
            return "lt"
        return "lte" if self._finds_nothing({f"{key}__lte": before_lte}) else ""

    def _ids_proven(self) -> bool:
        """The id filters proven (asked once, only when id windows are needed), and the top of the ids."""
        if self.id_top is None:
            self.id_end = self._prove("id", ID_TOP, 1, 0)
            top = ID_FIRST_TOP
            while self.id_end and top < ID_TOP and self._count({"id__gte": top}):
                top *= 4
            self.id_top = min(top, ID_TOP) if self.id_end else 0
        return bool(self.id_end)

    def _since(self) -> dt.date | None:
        try:
            return dt.date.fromisoformat(str(self.params.get(f"{self.date_key}__gte") or "")[:10])
        except ValueError:
            return None

    def _plan(self) -> list[Window] | None:
        """The windows, each announcing fewer than :data:`WINDOW_RECORDS` (or marked too large), in order and
        end to end; None when no window filter is proven."""
        since = self._since() if self.date_key else None
        if since is not None:
            self.date_end = self._prove(self.date_key, FAR_FUTURE, FAR_PAST, FAR_PAST)
        if self.date_end:
            self.bound = self.date_key
            starts = [since, *(dt.date(y, 1, 1) for y in range(since.year + 1, self.today.year + 2))]
            queue = [Window(days=(a, b)) for a, b in zip(starts, starts[1:], strict=False)]
            queue.append(Window(days=(starts[-1], None)))  # a report starting later is still read
        elif self._ids_proven():
            self.bound = "id"
            queue = [Window(ids=(1, self.id_top))]
        else:
            return None
        return self._split(queue)

    def _split(self, windows: list[Window]) -> list[Window]:
        done: list[Window] = []
        queue = deque(windows)
        while queue:
            window = queue.popleft()
            try:
                if window.planned is None:
                    window.planned = self._count(self._filters(window))
                if window.planned is None or not self._large(window.planned):
                    done.append(window)
                    continue
                halves = self._halves(window)
                if halves is None or len(done) + len(queue) + 2 > MAX_WINDOWS:
                    window.too_large = True
                    self.plan_complete = self.plan_complete and halves is None
                    done.append(window)
                    continue
                for half in halves:
                    half.planned = self._count(self._filters(half))
            except _OutOfCounts:  # the plan stops: the windows left are read as they are, and it is not whole
                self.plan_complete = False
                done.extend([window, *queue])
                break
            if sum(half.planned or 0 for half in halves) < window.planned:
                self.short = True  # records lost between the counts (more is a cut count): not whole
            queue.extendleft(reversed(halves))
        return done

    def _halves(self, window: Window) -> list[Window] | None:
        """Two windows end to end in place of one, or None when it cannot be split."""
        if window.ids:
            start, end = window.ids
            if end - start <= 1:
                return None
            middle = (start + end) // 2
            return [
                Window(days=window.days, ids=(start, middle)),
                Window(days=window.days, ids=(middle, end)),
            ]
        start, end = window.days
        if end is None:  # the open window: one year, then the open rest
            middle = _next_year(start)
            return [Window(days=(start, middle)), Window(days=(middle, None))]
        if (end - start).days <= 1:  # one day still too large: by id, when ids are proven
            if not self._ids_proven():
                return None
            return [Window(days=window.days, ids=(1, self.id_top))]
        middle = start + dt.timedelta(days=(end - start).days // 2)
        return [Window(days=(start, middle)), Window(days=(middle, end))]

    def _filters(self, window: Window) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if window.days:
            start, end = window.days
            found[f"{self.date_key}__gte"] = start.isoformat()
            if end is not None and self.date_end == "lt":
                found[f"{self.date_key}__lt"] = end.isoformat()
            elif end is not None:
                found[f"{self.date_key}__lte"] = (end - dt.timedelta(days=1)).isoformat()
        if window.ids:
            start, end = window.ids
            found["id__gte"] = start
            found["id__lt" if self.id_end == "lt" else "id__lte"] = end if self.id_end == "lt" else end - 1
        return found

    # ------------------------------------------------------------------------------------------ verdict
    @property
    def read(self) -> int:
        """The distinct records read, in every window."""
        return len(self.keys)

    def _end_to_end(self) -> bool:
        """The windows cover the query once, in order: dates from the first to the open end, the ids of a
        one-day window (or of the id windows) from 1 to the top."""
        days: list[tuple[dt.date, dt.date | None]] = []
        ids: dict[Any, list[tuple[int, int]]] = {}
        for window in self.windows:
            if window.days and (not days or days[-1] != window.days):
                days.append(window.days)
            if window.ids:
                ids.setdefault(window.days, []).append(window.ids)
        if days and (
            days[-1][1] is not None or any(a[1] != b[0] for a, b in zip(days, days[1:], strict=False))
        ):
            return False
        for ranges in ids.values():
            if ranges[0][0] != 1 or ranges[-1][1] != self.id_top:
                return False
            if any(a[1] != b[0] for a, b in zip(ranges, ranges[1:], strict=False)):
                return False
        return bool(days or ids)

    @property
    def changed(self) -> bool:
        """The Datamart's whole count changed during the read (its ETL ran). When the first count is a limit
        (round), the end count is the same limit whatever happened: the windows' own counts tell then."""
        return self.whole_count_end is not None and self.whole_count_end != self.announced

    @property
    def pages_cut(self) -> int:
        """The pages, not the last of their request, that held fewer records than its first page."""
        return sum(w.short_pages for w in self.windows)

    @property
    def windows_cut(self) -> int:
        """The windows read to their end that held fewer records than they announced (each record once, or
        again)."""
        if not self.bound:
            return 0
        return sum(
            1
            for w in self.windows
            if w.done and w.announced is not None and w.read + w.repeated < w.announced
        )

    @property
    def windows_short(self) -> bool:
        """The windows' counts add up to less than the whole count, or two halves to less than their own."""
        planned = sum(w.planned or 0 for w in self.windows)
        return self.short or (self.announced is not None and planned < self.announced)

    @property
    def whole(self) -> bool:
        """Whether the sync may remove what the read did not return: never after a cut page
        (:attr:`pages_cut`), a record lost whatever the counts say."""
        if not self.done or self.cap_suspected or self.pages_cut:
            return False
        if not self.windowed or not self.bound:
            return sync.read_whole(self.announced, self.read, self.repeated)
        return (
            self.plan_complete
            and not self.end_check_failed
            and not self.changed
            and not self.windows_short
            and not self.repeated
            and all(w.whole for w in self.windows)
            and self._end_to_end()
        )

    def whole_from(self, held_from: dt.date | None) -> dt.date | None:
        """Date windows only: the first period start held whole once this read is done: its lower bound when
        every window is whole, else the end of the latest window not whole. A window of reports starting after
        today (the open last one, split or not) holds reports not due yet: one not read whole means recent
        reports missing, no history question, so it never hides an older gap. A window not whole removed
        nothing, so the rows an earlier whole read kept for it are still held: it counts as whole when
        ``held_from`` (the previous run's) is at or before its start. A read not whole as a whole (a changed
        count, a plan cut short) keeps ``held_from``; with none (the first windowed night, after runs that
        cover nothing), its own windows decide, and with no gap among them nothing is held whole (None)."""
        if self.bound != self.date_key or not self.date_key:
            return None
        since = self.windows[0].days[0] if self.windows and self.windows[0].days else None

        def held(window: Window) -> bool:
            return window.whole or (held_from is not None and window.days and held_from <= window.days[0])

        failed = not self.plan_complete or self.end_check_failed or self.changed or self.windows_short
        if failed and held_from is not None:
            return held_from
        gaps = [w for w in self.windows if not held(w) and w.days and w.days[0] <= self.today]
        if gaps:
            return max(gaps, key=lambda w: w.days[0]).days[1]
        return None if failed else since

    def details(self) -> dict[str, Any]:
        """For the run: ``datamart_count`` (the first page's count, C), ``pages_cut`` when a page was cut, the
        stable order asked (``ordering``) and, for records that came twice, ``pages_shifted`` (``twice``, and
        ``missing``: announced, not read) or, under a stable order, ``held_twice`` (``twice_identical`` when
        every one carries no id and came identical); when windowed, why (``windows_why``), the windows (the
        first :data:`WINDOWS_KEPT`), what was read and announced, the filter proven, the checks and their
        outcome (``windows_cut``: windows read to their end holding fewer than they announced);
        ``cap_suspected`` when the answer looks cut. Counts, dates and ids only."""
        found: dict[str, Any] = {}
        if self.announced is not None:
            found["datamart_count"] = self.announced
        if self.cap_suspected:
            found["cap_suspected"] = self.cap_suspected
        if self.ends_missing:
            found["ends_missing"] = self.ends_missing
        if self.pages_cut:
            found["pages_cut"] = self.pages_cut
        if self.ordering:
            found["ordering"] = dict(self.ordering)
        if self.repeated and self.ordering.get("key"):  # twice in a stable order: likely held twice
            found["held_twice"] = self.repeated
        elif self.repeated:
            missing = max((self.announced or 0) - self.read, 0)
            found["pages_shifted"] = {"twice": self.repeated, "missing": missing}
        if self.repeated and self.repeated_same == self.repeated:
            found["twice_identical"] = True
        if not self.windowed:
            return found
        found.update(
            read=self.read, window_bound=self.bound, count_requests=self.count_requests, windows_why=self.why
        )
        if not self.bound:
            return found
        not_whole = [w.summary() for w in self.windows if not w.whole]
        planned = sum(w.planned or 0 for w in self.windows)
        found.update(
            windows=[w.summary() for w in self.windows[:WINDOWS_KEPT]],
            windows_read=len(self.windows),
            windows_not_whole=len(not_whole),
            windows_not_whole_first=not_whole[:3],
            windows_announced=planned,
            outside_window=sum(w.outside for w in self.windows),
            whole_count_end=self.whole_count_end,
            # the checks of a change during the read: every window's own count, and the whole count again
            # unless the first one is a limit (it would be the same limit whatever happened)
            change_checks=["window_counts"] + ([] if sync.capped(self.announced) else ["whole_count"]),
        )
        if self.announced is not None and planned > self.announced and not found["outside_window"]:
            found["whole_count_capped"] = True  # the Datamart's own count was cut; the windows hold more
        if self.windows_cut:
            found["windows_cut"] = self.windows_cut
        for flag, on in (
            ("windows_short", self.windows_short),
            ("changed", self.changed),
            ("plan_incomplete", not self.plan_complete),
            ("end_check_failed", self.end_check_failed),
            ("duplicates_read", self.repeated),
        ):
            if on:
                found[flag] = on
        return found
