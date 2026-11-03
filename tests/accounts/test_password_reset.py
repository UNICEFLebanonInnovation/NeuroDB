"""The "Forgot password?" page when NeuroDB cannot send email (``EMAIL_URL`` empty).

Without ``EMAIL_URL`` Django sends mail through its default SMTP server on this machine, which does not exist:
the reset form failed with a server error (the connection refused) at the first address typed. The page now
says that resetting by email is not set up and whom to ask, with no form, and a form sent anyway is refused
with the same words rather than a server error.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

from neurodb.accounts.models import User

pytestmark = pytest.mark.django_db

SMTP = "django.core.mail.backends.smtp.EmailBackend"  # what EMAIL_URL empty leaves (the test runner swaps it)
NOT_SET_UP = "Resetting a password by email is not set up in NeuroDB."


@pytest.fixture
def no_email(settings):
    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST, settings.EMAIL_PORT = "127.0.0.1", 9  # nothing listens there
    settings.PASSWORD_RESET_BY_EMAIL = False
    return settings


def test_the_flag_follows_email_url():
    from django.conf import settings

    assert settings.PASSWORD_RESET_BY_EMAIL is bool(settings.EMAIL_URL)


def test_without_email_the_reset_page_says_whom_to_ask(client, no_email):
    response = client.get(reverse("account_reset_password"))
    assert response.status_code == 200
    text = response.text
    assert NOT_SET_UP in text
    assert "Ask a NeuroDB administrator for a new password." in text
    assert "If you have a donor account, ask your UNICEF focal point for a new temporary password." in text
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
