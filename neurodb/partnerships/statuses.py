"""Programme document statuses as eTools names them, and the groups NeuroDB reads them in: one
definition for every page, so "active" and "running" mean the same thing everywhere.

* **Active**: the eTools status ``active`` and nothing else (the Programmes page's "Active" scope, the
  partner pages' active count, the Data quality tile "Active PDs").
* **Running**: active, signed or suspended: the programme documents still under way, whose partners
  report (the default of every partner monitoring and reporting PD status filter, the overview, the
  daily review, the watch).
* **Ended**: ended, closed, terminated or expired (the Data quality tile "Ended PDs").
"""

from __future__ import annotations

ACTIVE = ("active",)
RUNNING = ("active", "signed", "suspended")
ENDED = ("ended", "closed", "terminated", "expired")
# never listed on the eTools reporting pages: not (or no longer) a partnership
NOT_A_PARTNERSHIP = ("draft", "cancelled")


def label(status: str | None) -> str:
    """``"active"`` -> ``"Active"``: an eTools status as staff read it."""
    text = str(status or "").strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else "Unknown"
