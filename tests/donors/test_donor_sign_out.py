"""A donor who signed in with a temporary password can leave the password page.

Until the temporary password is changed every page leads to the password change page. That page has a Sign out
button of its own (so anyone can sign in with another account), says that the current password is the temporary
one, and, instead of a "Forgot your password?" link the donor could not use, says to sign out and ask the UNICEF
focal point for a new temporary password. A reset link from an email still works, and clears the flag.
"""

import re

import pytest
from django.core import mail
from django.urls import reverse

from neurodb.donors.models import DonorAccount
from tests.donors.test_donor_access import PASSWORD, make_donor

NEW_PASSWORD = "a-new-long-pass-9876"
RESET_PAGES = [
    "account_reset_password",
    "account_reset_password_done",
    "account_reset_password_from_key_done",
]


@pytest.fixture
def forced(client, db):
    """A client signed in as a donor whose temporary password is still to change."""
    account = make_donor("fresh-donor", must_change_password=True)
    client.force_login(account.user)
    client.account = account
    return client


def sign_out_form(text: str) -> str | None:
    """The page's own sign-out form (a POST to account_logout with its CSRF token), or None."""
    action = reverse("account_logout")
    match = re.search(rf'<form\s+method="post"\s+action="{action}">(.*?)</form>', text, re.S)
    if match and 'name="csrfmiddlewaretoken"' in match.group(1) and 'type="submit"' in match.group(1):
        return match.group(1)
    return None


def forms_are_not_nested(text: str) -> bool:
    depth = 0
    for tag in re.findall(r"<(/?)form\b", text):
        depth += -1 if tag else 1
        if depth not in (0, 1):
            return False
    return depth == 0


# ------------------------------------------------------------------------------ the password page
def test_the_password_page_has_sign_out_and_says_the_password_is_the_temporary_one(forced):
    response = forced.get(reverse("account_change_password"))
    assert response.status_code == 200
    text = response.text
    assert sign_out_form(text) is not None
    assert forms_are_not_nested(text)
    assert 'name="oldpassword"' in text and 'name="password1"' in text and 'name="password2"' in text
    assert "This is the temporary password you signed in with." in text
    assert (
        "If you no longer have it, sign out and ask your UNICEF focal point for a new temporary password."
        in text
    )
    # no link that cannot work: no reset link, no way back to a page the donor cannot open yet
    assert reverse("account_reset_password") not in text
    assert "Forgot your password?" not in text
    assert 'href="/"' not in text
    assert reverse("donors:page") not in text
    assert "at least 12 characters" in text  # the password rules, from the new password's help text


def test_sign_out_from_the_password_page_then_another_user_signs_in(forced, viewer):
    response = forced.post(reverse("account_logout"))
    assert response.status_code == 302 and response["Location"] == reverse("account_login")
    assert "_auth_user_id" not in forced.session
    assert forced.get(reverse("account_login")).status_code == 200
    response = forced.post(reverse("account_login"), {"login": "viewer", "password": "viewer-pass-123456"})
    assert response.status_code == 302
    assert forced.session["_auth_user_id"] == str(viewer.pk)
    assert forced.get(reverse("account_change_password")).status_code == 200  # no longer confined


def test_the_sign_out_confirmation_opens_for_a_forced_donor(forced):
    response = forced.get(reverse("account_logout"))
    assert response.status_code == 200
    assert sign_out_form(response.text) is not None


def test_the_logo_of_the_password_page_is_not_a_dead_end(forced):
    """For a donor still to change the password, every address leads back to the password page: the logo is no
    link then (it is one for everyone else)."""
    text = forced.get(reverse("account_change_password")).text
    assert '<a class="brand"' not in text and 'class="brand"' in text


# ------------------------------------------------------------------------------ the reset pages
@pytest.mark.parametrize("name", RESET_PAGES)
def test_a_forced_donor_reaches_the_reset_pages(forced, settings, name):
    settings.PASSWORD_RESET_BY_EMAIL = True
    response = forced.get(reverse(name))
    assert response.status_code == 200, response.get("Location")


def test_a_reset_link_that_no_longer_works_opens_too(forced):
    response = forced.get(reverse("account_reset_password_from_key", kwargs={"uidb36": "1", "key": "spent"}))
    assert response.status_code == 200
    assert response.context["token_fail"]


def test_a_forced_donor_still_reaches_nothing_else(forced):
    change = reverse("account_change_password")
    assert forced.get(reverse("donors:page"))["Location"] == change
    assert forced.get(reverse("reports:overview"))["Location"] == change
    assert forced.get(reverse("admin:index"))["Location"] == change
    assert forced.get(reverse("account_login"))["Location"] == change
    assert forced.get(reverse("api:donors")).status_code == 403
    assert forced.get(reverse("reports:overview"), HTTP_HX_REQUEST="true").status_code == 403


def test_a_reset_through_the_email_link_clears_the_flag(forced, settings):
    """The whole of allauth's reset: the form, the email, the link, the new password. The donor chose a password of
    their own, so the temporary one is no longer to change: signing in with the new one opens the donor page."""
    settings.PASSWORD_RESET_BY_EMAIL = True
    account = forced.account
    response = forced.post(reverse("account_reset_password"), {"email": account.user.email})
    assert response.status_code == 302 and response["Location"] == reverse("account_reset_password_done")
    assert len(mail.outbox) == 1
    link = re.search(r"https?://[^/\s]+(/accounts/password/reset/key/\S+/)", mail.outbox[0].body).group(1)
    response = forced.get(link)
    assert response.status_code == 302  # to the same address with the key kept in the session
    form_url = response["Location"]
    assert forced.get(form_url).status_code == 200
    response = forced.post(form_url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert response.status_code == 302 and response["Location"] == reverse(
        "account_reset_password_from_key_done"
    )
    account.refresh_from_db()
    assert account.must_change_password is False
    assert forced.get(reverse("account_reset_password_from_key_done")).status_code == 200
    forced.logout()
    response = forced.post(
        reverse("account_login"), {"login": account.user.username, "password": NEW_PASSWORD}
    )
    assert response["Location"] == reverse("donors:page")
    assert forced.get(reverse("donors:page"), {"year": "2026"}).status_code == 200


def test_the_password_reset_signal_clears_the_flag(db):
    from allauth.account.signals import password_reset

    account = make_donor("reset-donor", must_change_password=True)
    password_reset.send(sender=account.user.__class__, request=None, user=account.user)
    account.refresh_from_db()
    assert account.must_change_password is False


# ------------------------------------------------------------------------------ moving forward
def test_a_forced_donor_who_changes_the_password_lands_on_the_donor_page(forced):
    response = forced.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    forced.account.refresh_from_db()
    assert forced.account.must_change_password is False
    page = forced.get(reverse("donors:page"), {"year": "2026"})
    assert page.status_code == 200
    assert "Password successfully changed." in page.text


def test_a_next_address_after_the_change_still_ends_on_the_donor_page(forced):
    response = forced.post(
        reverse("account_change_password") + "?next=/programmes/",
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302
    assert forced.get(response["Location"])["Location"] == reverse("donors:page")


def test_a_wrong_temporary_password_keeps_the_page_and_its_way_out(forced):
    response = forced.post(
        reverse("account_change_password"),
        {"oldpassword": "not-the-password", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 200
    assert 'class="form-control is-invalid"' in response.text
    assert sign_out_form(response.text) is not None
    forced.account.refresh_from_db()
    assert forced.account.must_change_password is True


def test_a_forced_donor_without_a_usable_password_sets_one(client, db):
    """A sign-in whose password was switched off (signed in another way) is sent by allauth to the page that sets a
    password: it opens too, rather than leading back to the password change page."""
    account = make_donor("no-password-donor", must_change_password=True)
    account.user.set_unusable_password()
    account.user.save()
    client.force_login(account.user)
    response = client.get(reverse("account_change_password"))
    assert response["Location"] == reverse("account_set_password")
    page = client.get(reverse("account_set_password"))
    assert page.status_code == 200
    assert sign_out_form(page.text) is not None
    response = client.post(
        reverse("account_set_password"), {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    )
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    account.refresh_from_db()
    assert account.must_change_password is False


# ------------------------------------------------------------------------------ a valid donor, staff
def test_a_valid_donor_has_the_way_back_and_is_still_confined(client, db):
    account = make_donor("valid-donor")
    client.force_login(account.user)
    text = client.get(reverse("account_change_password")).text
    assert sign_out_form(text) is not None
    assert f'href="{reverse("donors:page")}"' in text
    assert reverse("account_reset_password") in text  # "Forgot your password?": the reset pages open
    for name in RESET_PAGES:
        assert client.get(reverse(name)).status_code == 200, name
    assert client.get(reverse("reports:overview"))["Location"] == reverse("donors:page")
    assert client.get(reverse("admin:index"))["Location"] == reverse("donors:page")
    assert client.get(reverse("api:donors")).status_code == 403
    assert DonorAccount.objects.get(pk=account.pk).must_change_password is False


def test_staff_see_sign_out_and_home_on_the_password_page(client_viewer):
    response = client_viewer.get(reverse("account_change_password"))
    assert response.status_code == 200
    text = response.text
    assert sign_out_form(text) is not None
    assert forms_are_not_nested(text)
    home = reverse("reports:overview")
    assert re.search(rf'<a [^>]*href="{home}"[^>]*>\s*Home\s*</a>', text)
    assert "temporary password" not in text
    assert f'<a href="{reverse("account_reset_password")}">Forgot your password?</a>' in text
    assert '<a class="brand" href="/">' in text


def test_staff_stay_on_the_password_page_after_the_change(client_viewer):
    response = client_viewer.post(
        reverse("account_change_password"),
        {"oldpassword": "viewer-pass-123456", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302 and response["Location"] == reverse("account_change_password")
    page = client_viewer.get(response["Location"])
    assert "Password successfully changed." in page.text
    assert sign_out_form(page.text) is not None
