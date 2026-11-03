"""A donor who signed in with a temporary password can leave the password page.

Until the temporary password is changed every page leads to the password change page. That page has a Sign out
button of its own (so anyone can sign in with another account), says that the current password is the temporary
one, and, instead of a "Forgot your password?" link the donor could not use, says to sign out and ask the UNICEF
focal point for a new temporary password. A reset link from an email still works, and clears the flag.
"""

import re
from pathlib import Path

import pytest
from django.core import mail
from django.test import Client
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


def csrf_token(text: str) -> str:
    return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text).group(1)


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
    assert response.status_code == 302 and response["Location"] == reverse("reports:overview")
    assert forced.session["_auth_user_id"] == str(viewer.pk)
    home = forced.get(response["Location"])
    assert home.status_code == 200  # Home itself, not the password page or the donor page


def test_sign_out_after_the_session_ended_then_staff_sign_in_lands_on_home(db, viewer):
    """The password page is open in one browser; meanwhile the administrator gives the donor a new temporary
    password (or the session runs out), which ends the session. Sign out then still leads straight to the sign-in
    page, not to a sign-in that ends on the sign-out confirmation."""
    browser = Client(enforce_csrf_checks=True)
    account = make_donor("fresh-donor", must_change_password=True)
    browser.force_login(account.user)
    token = csrf_token(sign_out_form(browser.get(reverse("account_change_password")).text))
    account.user.set_password("another-temporary-pass-1")  # as "New temporary password" does
    account.user.save()
    response = browser.post(reverse("account_logout"), {"csrfmiddlewaretoken": token})
    assert response.status_code == 302 and response["Location"] == reverse("account_login")
    page = browser.get(response["Location"])
    assert "Sign in to open this page." not in page.text
    response = browser.post(
        reverse("account_login"),
        {"login": "viewer", "password": "viewer-pass-123456", "csrfmiddlewaretoken": csrf_token(page.text)},
    )
    assert response.status_code == 302 and response["Location"] == reverse("reports:overview")
    assert browser.get(response["Location"]).status_code == 200


def test_the_sign_out_confirmation_opens_for_a_forced_donor(forced):
    response = forced.get(reverse("account_logout"))
    assert response.status_code == 200
    assert sign_out_form(response.text) is not None


def test_the_logo_of_the_password_page_is_not_a_dead_end(forced):
    """For a donor still to change the password, every address leads back to the password page: the logo is no
    link then (it is one for everyone else)."""
    text = forced.get(reverse("account_change_password")).text
    assert '<a class="brand"' not in text and 'class="brand"' in text


@pytest.mark.parametrize("name", ["account_reset_password_done", "account_logout", "account_reauthenticate"])
def test_on_the_other_sign_in_pages_the_logo_leads_to_the_password_page(forced, settings, name):
    """Those pages have no Sign out of their own (or only allauth's): their logo leads to the password page."""
    settings.PASSWORD_RESET_BY_EMAIL = True
    response = forced.get(reverse(name))
    assert response.status_code == 200
    assert f'<a class="brand" href="{reverse("account_change_password")}">' in response.text


def test_too_many_tries_on_the_password_page_keeps_a_way_out(forced):
    """allauth allows 5 tries a minute on the password page; the next one gets NeuroDB's page, not a bare one."""
    wrong = {"oldpassword": "not-the-password", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    responses = [forced.post(reverse("account_change_password"), wrong) for _ in range(6)]
    assert [response.status_code for response in responses] == [200] * 5 + [429]
    text = responses[-1].text
    assert 'class="panel auth-card"' in text
    assert "Too many tries" in text and "Wait a minute, then try again." in text
    assert f'href="{reverse("account_change_password")}"' in text
    assert sign_out_form(text) is not None
    assert forced.get(reverse("account_change_password")).status_code == 200


def test_the_too_many_tries_page_of_a_visitor_leads_to_sign_in(rf):
    from allauth.core.internal.ratelimit import handler429
    from django.contrib.auth.models import AnonymousUser

    request = rf.get(reverse("account_login"))
    request.user = AnonymousUser()
    response = handler429(request)
    assert response.status_code == 429
    text = response.content.decode()
    assert "Wait a minute, then try again." in text
    assert f'href="{reverse("account_login")}"' in text
    assert sign_out_form(text) is None


def test_a_long_sign_in_name_wraps_on_the_password_page(client, db, settings):
    """A donor added as a new sign-in has the email address as username: on a phone it must wrap, not push the
    page wider than the screen."""
    account = make_donor("fresh-donor", must_change_password=True)
    account.user.username = "programme.reporting@delegation.example.org"
    account.user.save()
    client.force_login(account.user)
    text = client.get(reverse("account_change_password")).text
    match = re.search(
        r'<span class="([^"]*)"[^>]*>Signed in as programme\.reporting@delegation\.example\.org', text
    )
    assert match and "text-break" in match.group(1).split()
    # and allauth's own pages ("You are already logged in as ..."): the card never grows wider than the screen
    css = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
    card = re.search(r"^\.auth-card \{([^}]*)\}", css, re.M).group(1)
    assert "min-width: 0" in card and "overflow-wrap: break-word" in card


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


def test_field_errors_are_tied_to_their_inputs(forced):
    """A screen reader on a field with an error hears the error: it is named in aria-describedby, and the field
    is marked aria-invalid."""
    url = reverse("account_change_password")
    short = forced.post(url, {"oldpassword": "", "password1": "short", "password2": "short"}).text
    mismatch = forced.post(
        url, {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": "other"}
    ).text
    for field, text in (("oldpassword", short), ("password1", short), ("password2", mismatch)):
        tag = re.search(rf'<input [^>]*name="{field}"[^>]*>', text).group(0)
        assert 'aria-invalid="true"' in tag, field
        described = re.search(r'aria-describedby="([^"]*)"', tag).group(1).split()
        assert described[0] == f"id_{field}_error", field
        assert re.search(rf'<div class="invalid-feedback" id="id_{field}_error"><div>\S', text), field
        if field != "password2":
            assert f"id_{field}_helptext" in described, field  # the help text is still read
    tag = re.search(r'<input [^>]*name="password2"[^>]*>', short).group(0)
    assert "aria-invalid" not in tag and "aria-describedby" not in tag  # no error on it there


def test_fields_without_errors_are_not_marked_invalid(forced):
    text = forced.get(reverse("account_change_password")).text
    assert "aria-invalid" not in text
    assert not any(f"id_{field}_error" in text for field in ("oldpassword", "password1", "password2"))


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
