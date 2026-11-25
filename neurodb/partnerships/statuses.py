"""Programme document statuses as eTools names them, and the groups NeuroDB reads them in: one
definition for every page, so "active" and "running" mean the same thing everywhere.

* **Active**: the eTools status ``active`` and nothing else (the Programmes page's "Active" scope, the
  partner pages' active count, the Data quality tile "Active PDs"), and :data:`PAGE_DEFAULT`, what every PD
  status filter opens on (the owner's decision of 25 Nov 2026: Partner monitoring and its map, Progress
  reports since 21 Nov 2026, Funds and the HACT visit plan).
* **Running**: active, signed or suspended: the programme documents still under way, whose partners
  report (the overview, the daily review, the watch, the eTools reporting checks, Home's figures and the
  partner page's indicators: each writes them out in its links, so the page it opens shows its figure).
* **Ended**: ended, closed, terminated or expired (the Data quality tile "Ended PDs").
"""

from __future__ import annotations

ACTIVE = ("active",)
RUNNING = ("active", "signed", "suspended")
ENDED = ("ended", "closed", "terminated", "expired")
# never listed on the eTools reporting pages: not (or no longer) a partnership
NOT_A_PARTNERSHIP = ("draft", "cancelled")
# what every PD status filter opens on, with no PD status in the page's address: the active programme
# documents alone (the owner's decision of 25 Nov 2026; the running ones before, on Partner monitoring)
PAGE_DEFAULT = ACTIVE
# every status, as a link writes it: the PD status parameter with nothing chosen ("pd_status=", "status="),
# which every page reads as every status, never as its default; a tuple of one empty value, so a query
# built with ``urlencode(..., doseq=True)`` (review.checks.link) keeps the parameter
EVERY = ("",)


def label(status: str | None) -> str:
    """``"active"`` -> ``"Active"``: an eTools status as staff read it."""
    text = str(status or "").strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else "Unknown"
