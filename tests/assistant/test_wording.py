"""What the pages call the AI: the provider in use, read from the configuration (no template names one),
with today's words while OpenAI is in use; and the set-up hint, which names both keys, for staff only."""

import re

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

pytestmark = pytest.mark.django_db

CLAUDE_ON = {"AI_ASSISTANT_ENABLED": True, "AI_PROVIDER": "claude", "ANTHROPIC_API_KEY": "test-key-not-real"}
PROVIDER = re.compile(r"\b(OpenAI|ChatGPT|Anthropic|Claude)\b")  # case-sensitive: OPENAI_API_KEY passes


def test_no_template_names_a_provider():
    found = []
    for path in sorted((settings.BASE_DIR / "neurodb").glob("**/templates/**/*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PROVIDER.search(line):
                found.append(f"{path.relative_to(settings.BASE_DIR)}:{number}")
    assert found == []


def test_the_pages_name_the_provider_in_use(client_viewer, admin_client):
    with override_settings(**CLAUDE_ON):
        ask = client_viewer.get(reverse("assistant:ask")).content.decode()
        add = admin_client.get(reverse("knowledge:add")).content.decode()
    assert "answered from NeuroDB&#x27;s data by Claude (Anthropic)" in ask
    assert (
        "Answers are written by Claude (Anthropic&#x27;s model)" in ask and "sent to the Anthropic API" in ask
    )
    assert "ChatGPT" not in ask and "OpenAI API" not in ask
    assert "sent to the Anthropic API to be summarised" in add
    with override_settings(AI_ASSISTANT_ENABLED=True):  # OpenAI: the words it always had
        ask = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert "answered from NeuroDB&#x27;s data by ChatGPT (OpenAI)" in ask
    assert (
        "Answers are written by ChatGPT (OpenAI&#x27;s GPT model)" in ask and "sent to the OpenAI API" in ask
    )


def test_the_set_up_hint_names_both_keys_for_administrators_only(client_viewer, admin_user):
    with override_settings(AI_ASSISTANT_ENABLED=False, OPENAI_API_KEY=""):
        page = client_viewer.get(reverse("assistant:ask")).content.decode()
        assert "not set up yet" in page and "ANTHROPIC_API_KEY" not in page and "AI settings" not in page
        client_viewer.force_login(admin_user)
        page = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert (
        "For administrators: enter an Anthropic (Claude) or OpenAI API key in the admin (AI settings), or ask "
        "the hosting team to set ANTHROPIC_API_KEY or OPENAI_API_KEY." in page
    )
