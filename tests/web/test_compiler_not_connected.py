"""The Compiler pages before Compiler is connected (R1 a11y_words-W-6). The sidebar's greyed *Set up* rows send
administrators to these pages, which say what to do in the application: administrators have no shell and
never set a setting, so no page names one; the setting names stay in docs/OPERATIONS.md."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

PAGES = [
    ("education:makani", "Read the education figures from Compiler"),
    ("education:dirasa", "Read the education figures from Compiler"),
    ("youth:dashboard", "Read the youth figures from Compiler"),
    ("wellbeing:summaries", "Read the Makani wellbeing flags from Compiler"),
]


@pytest.mark.parametrize(("name", "job"), PAGES)
def test_a_page_without_compiler_says_what_to_do_in_the_application(client, admin_user, settings, name, job):
    settings.COMPILER_API_URL = ""
    settings.COMPILER_API_TOKEN = ""
    client.force_login(admin_user)  # the administrator the Set up row sent here
    response = client.get(reverse(name))
    assert response.status_code == 200
    html = response.content.decode()
    main = html.split("<main", 1)[1].split("</main>", 1)[0]
    assert "Compiler is not connected" in main
    assert (
        "The NeuroDB team connects it on the server; an administrator then runs the job "
        f"“{job}” (admin → Import and sync runs → Run a job)."
    ) in main
    for word in ("COMPILER_API", "Key Vault", "compiler-api-token"):
        assert word not in html, word
