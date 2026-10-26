"""The addresses Compare with Power BI writes (its lines' "Check in NeuroDB", its pages' links): the admin's
pages and the reports' pages, reversed against these two parts of the site's URLconf alone
(``powerbi_urls``, mounted where ``config.urls`` mounts them). The job's own process then never imports the
rest of the site: the AI assistant's SDKs alone, which the "For you" pages import, take some 50 MB of the
job's memory (its data limit is 250 MB at least). The addresses are the site's own
(``tests/datamart/test_powerbi_job.py`` checks each against the site's URLconf).
"""

from __future__ import annotations

from typing import Any

from django.urls import reverse as site_reverse

URLCONF = "neurodb.datamart.powerbi_urls"


def reverse(viewname: str, args: list[Any] | None = None) -> str:
    """``django.urls.reverse`` against :data:`URLCONF`."""
    return site_reverse(viewname, urlconf=URLCONF, args=args)
