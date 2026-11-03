"""The "Forgot password?" page when NeuroDB cannot send email (``EMAIL_URL`` empty).

Without ``EMAIL_URL`` Django sends mail through its default SMTP server on this machine, which does not exist:
the reset form failed with a server error (the connection refused) at the first address typed. The page now
says that resetting by email is not set up and whom to ask, with no form, and a form sent anyway is refused
with the same words rather than a server error.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import environ
import pytest
from django.core import mail
from django.urls import reverse

from neurodb.accounts.models import User
from tests.donors.test_donor_access import make_donor

pytestmark = pytest.mark.django_db

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "config" / "settings.py"
SMTP = "django.core.mail.backends.smtp.EmailBackend"  # what EMAIL_URL empty leaves (the test runner swaps it)
NOT_SET_UP = "Resetting a password by email is not set up in NeuroDB."
ASK_ADMIN = "Ask a NeuroDB administrator for a new password."
ASK_FOCAL_POINT = "Ask your UNICEF focal point for a new temporary password."
DONOR_LINE = "If you have a donor account, ask your UNICEF focal point for a new temporary password."


@pytest.fixture
def no_email(settings):
    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST, settings.EMAIL_PORT = "127.0.0.1", 9  # nothing listens there
    settings.PASSWORD_RESET_BY_EMAIL = False
    return settings


def _settings_with(monkeypatch, email_url: str) -> dict:
    """The settings module run afresh with this ``EMAIL_URL`` (and not a local .env)."""
    monkeypatch.setattr(environ.Env, "read_env", classmethod(lambda cls, *args, **kwargs: None))
    monkeypatch.setenv("EMAIL_URL", email_url)
    return runpy.run_path(str(SETTINGS_FILE))


def test_reset_by_email_is_on_only_when_email_url_is_set(monkeypatch):
    on = _settings_with(monkeypatch, "smtp://mail.example.invalid:25")
    assert on["PASSWORD_RESET_BY_EMAIL"] is True
    assert on["EMAIL_HOST"] == "mail.example.invalid" and on["EMAIL_PORT"] == 25
    off = _settings_with(monkeypatch, "")
    assert off["PASSWORD_RESET_BY_EMAIL"] is False and "EMAIL_HOST" not in off


def test_without_email_the_reset_page_says_whom_to_ask(client, no_email):
    response = client.get(reverse("account_reset_password"))
    assert response.status_code == 200
    text = response.text
    assert NOT_SET_UP in text
    assert ASK_ADMIN in text
    assert DONOR_LINE in text
    assert 'name="email"' not in text
    assert f'href="{reverse("account_login")}"' in text  # back to sign in
    assert "@" not in text.split("<main", 1)[1]  # no email address on the page


def test_without_email_a_reset_form_sent_anyway_is_refused_not_a_server_error(client, no_email):
    User.objects.create_user(username="someone", email="someone@example.org", password="pass-123456-x")
    response = client.post(reverse("account_reset_password"), {"email": "someone@example.org"})
    assert response.status_code == 200
    assert NOT_SET_UP in response.text
    assert mail.outbox == []
    response = client.post(reverse("account_reset_password"), {"email": "nobody@example.invalid"})
    assert response.status_code == 200 and NOT_SET_UP in response.text


def test_with_email_the_reset_page_keeps_its_form(client, settings):
    settings.PASSWORD_RESET_BY_EMAIL = True
    User.objects.create_user(username="someone", email="someone@example.org", password="pass-123456-x")
    response = client.get(reverse("account_reset_password"))
    assert response.status_code == 200
    assert 'name="email"' in response.text and NOT_SET_UP not in response.text
    response = client.post(reverse("account_reset_password"), {"email": "someone@example.org"})
    assert response.status_code == 302 and response["Location"] == reverse("account_reset_password_done")
    assert len(mail.outbox) == 1 and "/accounts/password/reset/key/" in mail.outbox[0].body


def test_without_email_a_forced_donor_is_told_to_ask_the_focal_point(client, no_email):
    """A donor still on the temporary password: only the focal point line, and the way back is the password page
    (the sign-in page would only lead back to it)."""
    account = make_donor("fresh-donor", must_change_password=True)
    client.force_login(account.user)
    response = client.get(reverse("account_reset_password"))
    assert response.status_code == 200
    text = response.text
    assert NOT_SET_UP in text and ASK_FOCAL_POINT in text
    assert ASK_ADMIN not in text and DONOR_LINE not in text
    assert f'href="{reverse("account_change_password")}"' in text
    assert f'href="{reverse("account_login")}"' not in text


def test_without_email_signed_in_staff_are_not_given_the_donor_line(client_viewer, no_email):
    response = client_viewer.get(reverse("account_reset_password"))
    assert response.status_code == 200
    text = response.text
    assert NOT_SET_UP in text and ASK_ADMIN in text
    assert DONOR_LINE not in text  # they are signed in and have no donor account
    assert f'href="{reverse("account_change_password")}"' in text
