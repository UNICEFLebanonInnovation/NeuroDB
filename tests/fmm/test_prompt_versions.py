"""Prompt versions of Monitoring insights' AI (stage 6a): the seeded v1, drafts, publishing, rollbacks
and deleting drafts with their test runs; published versions never change; the admin's form checks and
warnings, its Preview (exactly what each call sends, no AI call, nothing saved) and who may do what."""

import re

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.assistant import agent
from neurodb.fmm.ai import profiles, prompts, sampling
from neurodb.fmm.models import Insight, ModelCapability, PromptProfile, PromptVersion
from neurodb.fmm.scope import Scope
from tests.assistant.test_assistant import ENABLED, FakeClient, reply, say

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def v1():
    return PromptVersion.objects.get(number=1)


def _test_run(version, n=1):
    for i in range(n):
        Insight.objects.create(
            scope_hash=f"h{i}",
            scope={},
            scope_label="1 Jan – 31 Dec 2026 · Lebanon",
            trigger=Insight.Trigger.TEST,
            version=version,
            rules_version=1,
            input_hash=f"i{i}",
            status=Insight.Status.OK,
        )


def _form(version, **changes):
    """The admin form's fields for ``version``'s content, with ``changes``."""
    data = {}
    for name in PromptVersion.CONTENT_FIELDS:
        value = getattr(version, name)
        if isinstance(value, bool):
            if value:
                data[name] = "on"
            continue
        data[name] = "" if value is None else str(value)
    data["note"] = "Shorter brief"
    data.update(changes)
    return data


# ------------------------------------------------------------------------------------------ the seed
def test_v1_is_seeded_published_with_the_defaults(v1):
    profile = PromptProfile.objects.get(key="lebanon")
    assert profile.label == "Lebanon" and profile.published_id == v1.pk
    assert v1.status == "published" and v1.profile == profile
    assert str(v1.temperature) == "0.30" and v1.top_p is None
    assert (v1.max_output_tokens, v1.chat_max_output_tokens) == (4000, 6000)
    assert (v1.narratives_sampled, v1.comparison_visits) == (20, 15)
    assert (v1.insights_per_user_per_day, v1.chat_per_user_per_day) == (5, 20)
    assert (v1.chat_max_rounds, v1.chat_time_limit, v1.effort, v1.model) == (4, 120, "low", "")
    assert v1.created_by_name == v1.published_by_name == "NeuroDB (default)" and v1.published_at
    assert v1.instructions.startswith("You write the field monitoring brief for UNICEF Lebanon")
    assert "priority_actions: 3 to 6 actions" in v1.instructions
    assert v1.chat_instructions.startswith("You answer questions from UNICEF Lebanon staff")
    assert v1.content_hash == v1.compute_hash()  # the migration's literal hash is the model's
    assert profiles.published() == v1
    assert profiles.model_of(v1) == settings.FMM_MODEL  # a blank model: the setting's
    assert profiles.warnings(v1) == []  # temperature only: the "both set" warning does not fire


def test_the_content_hash_changes_with_the_safety_text_version(v1, monkeypatch):
    before = v1.compute_hash()
    monkeypatch.setattr(prompts, "SAFETY_VERSION", prompts.SAFETY_VERSION + 1)
    assert v1.compute_hash() != before


# ------------------------------------------------------------------------------------------ the lifecycle
def test_published_and_retired_versions_never_change(v1, admin_user):
    v1.instructions = v1.instructions + " More."
    with pytest.raises(ValueError, match="cannot change"):
        v1.save()
    v1.refresh_from_db()
    v1.temperature = "0.3"  # the same value written another way is no change
    v1.save()
    v1.status = "draft"
    with pytest.raises(ValueError, match="cannot become a draft"):
        v1.save()
    draft = profiles.draft_from(PromptVersion.objects.get(pk=v1.pk), admin_user, "New")
    profiles.publish(draft, admin_user)
    retired = PromptVersion.objects.get(pk=v1.pk)
    assert retired.status == "retired"
    retired.effort = "high"
    with pytest.raises(ValueError):
        retired.save()


def test_a_draft_is_numbered_after_the_last_and_can_change(v1, admin_user):
    draft = profiles.draft_from(v1, admin_user, "Narratives off", narratives_sampled=0)
    assert (draft.number, draft.status, draft.based_on, draft.narratives_sampled) == (2, "draft", v1, 0)
    assert draft.created_by == admin_user and draft.created_by_name == "admin"
    assert draft.instructions == v1.instructions and draft.content_hash != v1.content_hash
    draft.narratives_sampled = 5
    draft.save()
    draft.refresh_from_db()
    assert draft.narratives_sampled == 5 and draft.content_hash == draft.compute_hash()
    with pytest.raises(ValueError, match="not prompt content"):
        profiles.draft_from(v1, admin_user, "x", status="published")


def test_publish_retires_the_published_one_and_only_one_is_published(v1, admin_user):
    draft = profiles.draft_from(v1, admin_user, "v2")
    profiles.publish(draft, admin_user)
    draft.refresh_from_db()
    v1.refresh_from_db()
    assert (v1.status, draft.status) == ("retired", "published")
    assert draft.published_by == admin_user and draft.published_at
    assert PromptProfile.objects.get(key="lebanon").published == draft == profiles.published()
    with pytest.raises(ValueError, match="only a draft"):
        profiles.publish(draft, admin_user)
    other = profiles.draft_from(v1, admin_user, "v3")
    with pytest.raises(IntegrityError), transaction.atomic():
        PromptVersion.objects.filter(pk=other.pk).update(status="published")


def test_a_rollback_publishes_a_new_copy_and_the_history_stays_linear(v1, admin_user):
    v2 = profiles.draft_from(v1, admin_user, "Warmer", temperature="0.70")
    profiles.publish(v2, admin_user)
    v3 = profiles.roll_back(PromptVersion.objects.get(pk=v1.pk), admin_user, "too creative")
    assert v3.number == 3 and v3.status == "published" and v3.restored_from_id == v1.pk
    assert v3.note == "Rolled back to v1: too creative"
    assert v3.content() == PromptVersion.objects.get(pk=v1.pk).content()
    assert list(PromptVersion.objects.order_by("number").values_list("status", flat=True)) == [
        "retired",
        "retired",
        "published",
    ]
    with pytest.raises(ValueError, match="a draft"):
        profiles.roll_back(profiles.draft_from(v1, admin_user, "d"), admin_user, "x")


def test_a_test_run_of_a_draft_does_not_publish_it(v1, admin_user):
    draft = profiles.draft_from(v1, admin_user, "Try")
    _test_run(draft)
    draft.refresh_from_db()
    assert draft.status == "draft" and profiles.published() == v1


def test_a_draft_with_test_runs_is_deleted_with_them(v1, admin_user):
    draft = profiles.draft_from(v1, admin_user, "Try")
    _test_run(draft, 2)
    assert profiles.test_runs(draft) == 2
    assert profiles.delete_draft(draft, admin_user) == 2
    assert not PromptVersion.objects.filter(pk=draft.pk).exists() and not Insight.objects.exists()
    with pytest.raises(ValueError, match="only drafts"):
        profiles.delete_draft(v1, admin_user)


# ------------------------------------------------------------------------------------------ prompts sent
def test_compose_puts_the_fixed_text_last(v1):
    brief = profiles.compose(v1, "insights")
    assert (
        brief
        == f"{v1.instructions.strip()}\n\n---\n{prompts.DATA_GUIDE_INSIGHTS}\n{prompts.SAFETY_COMMON}\n{prompts.SAFETY_INSIGHTS}"
    )
    chat = profiles.compose(v1, "chat")
    assert chat.startswith(v1.chat_instructions.strip()) and chat.endswith(prompts.SAFETY_CHAT)
    assert prompts.DATA_GUIDE_CHAT in chat and prompts.SAFETY_INSIGHTS not in chat


def test_the_chat_instructions_are_the_agents_effective_instructions(v1, reporting_year):
    scope = Scope.from_params({"section": ""})
    text = profiles.chat_instructions(v1, scope)
    options = agent.RunOptions(
        base_prompt=profiles.compose(v1, "chat"), instructions=profiles.scope_line(scope)
    )
    assert text == agent.effective_instructions(options)
    assert text.startswith(v1.chat_instructions.strip()) and text.endswith(
        f"The visits in scope: {scope.label()}."
    )
    assert agent.SYSTEM_PROMPT[:80] not in text and "list_databases" not in text and "make_chart" not in text


# ------------------------------------------------------------------------------------------ checks and warnings
def test_texts_with_an_email_a_link_or_a_known_name_are_refused():
    names = frozenset({"rania canary"})
    assert profiles.text_problems("Write for section chiefs.", names) == []
    assert "e-mail" in profiles.text_problems("Ask karim.canary@example.org.", names)[0]
    assert "link" in profiles.text_problems("See https://evil.example/x for more.", names)[0]
    assert "name of a person" in profiles.text_problems("Rania Canary leads it.", names)[0]


def test_the_warnings(v1, admin_user):
    draft = profiles.draft_from(v1, admin_user, "x", effort="medium", max_output_tokens=1500, top_p="0.9")
    found = profiles.warnings(draft)
    assert any("Effort medium with under 2,500 output tokens" in w for w in found)
    assert any("both set" in w for w in found)
    sampling.record(profiles.model_of(draft), "medium", "temperature", accepted=False)
    assert any(
        re.search(r"Temperature was refused by .+ at effort medium on .+: it will not be applied", w)
        for w in profiles.warnings(draft)
    )


# ------------------------------------------------------------------------------------------ the admin
def test_administrators_add_a_draft_prefilled_from_the_published_version(admin_client, admin_user, v1):
    page = admin_client.get(reverse("admin:fmm_promptversion_add"))
    html = page.content.decode()
    assert page.status_code == 200 and "prefilled from v1" in html
    assert escape(v1.chat_instructions.splitlines()[0]) in html
    response = admin_client.post(reverse("admin:fmm_promptversion_add"), _form(v1, narratives_sampled="10"))
    assert response.status_code == 302, response.content.decode()[:3000]
    draft = PromptVersion.objects.get(number=2)
    assert (draft.status, draft.based_on, draft.narratives_sampled, draft.note) == (
        "draft",
        v1,
        10,
        "Shorter brief",
    )
    assert draft.created_by == admin_user and draft.profile.key == "lebanon"
    # ?from=<pk> prefills from that version
    profiles.publish(draft, admin_user)
    html = admin_client.get(reverse("admin:fmm_promptversion_add") + f"?from={v1.pk}").content.decode()
    assert "prefilled from v1" in html


def test_the_form_blocks_people_links_and_unknown_efforts(admin_client, v1):
    for changes in (
        {"instructions": v1.instructions + " Write to karim.canary@example.org."},
        {"chat_instructions": v1.chat_instructions + " See https://evil.example/x."},
        {"effort": "extreme"},
        {"max_output_tokens": "500"},
        {"narratives_sampled": "51"},
        {"temperature": "2.5"},
        {"instructions": "Too short."},
        {"note": ""},
    ):
        response = admin_client.post(reverse("admin:fmm_promptversion_add"), _form(v1, **changes))
        assert response.status_code == 200, changes
    assert PromptVersion.objects.count() == 1


def test_saving_a_draft_shows_the_warnings(admin_client, admin_user, v1):
    draft = profiles.draft_from(v1, admin_user, "x")
    url = reverse("admin:fmm_promptversion_change", args=[draft.pk])
    page = admin_client.get(url).content.decode()  # the fixed part, read-only, under the editable text
    assert 'name="_save"' in page and "Rules that always apply" in page and "fmm_brief" not in page
    assert "additionalProperties" in page and "Preview" in page and "Publish" in page
    response = admin_client.post(
        url, _form(draft, effort="high", max_output_tokens="1500", top_p="0.9"), follow=True
    )
    html = response.content.decode()
    assert "under 2,500 output tokens" in html and "both set" in html
    draft.refresh_from_db()
    assert draft.effort == "high" and str(draft.top_p) == "0.90"


def test_published_versions_open_read_only_and_cannot_be_deleted(admin_client, v1):
    url = reverse("admin:fmm_promptversion_change", args=[v1.pk])
    html = admin_client.get(url).content.decode()
    assert 'name="_save"' not in html and "never changes" in html and "Roll back to this version" in html
    assert admin_client.post(url, _form(v1, effort="high")).status_code == 403
    assert (
        admin_client.post(
            reverse("admin:fmm_promptversion_delete", args=[v1.pk]), {"post": "yes"}
        ).status_code
        == 403
    )
    v1.refresh_from_db()
    assert v1.effort == "low"


def test_publish_and_roll_back_from_the_admin(admin_client, admin_user, v1):
    draft = profiles.draft_from(v1, admin_user, "v2")
    publish = reverse("admin:fmm_promptversion_publish", args=[draft.pk])
    html = admin_client.get(publish).content.decode()
    assert "Briefs written with v1 stay until tonight's run or a Regenerate" in html
    assert admin_client.post(publish).status_code == 302
    assert profiles.published() == PromptVersion.objects.get(pk=draft.pk)
    back = reverse("admin:fmm_promptversion_roll_back", args=[v1.pk])
    assert "Write why" in admin_client.post(back, {"note": ""}).content.decode()
    assert admin_client.post(back, {"note": "v2 was worse"}).status_code == 302
    v3 = profiles.published()
    assert v3.number == 3 and v3.restored_from_id == v1.pk


def test_a_draft_is_deleted_from_the_admin_with_its_test_runs(admin_client, admin_user, v1):
    draft = profiles.draft_from(v1, admin_user, "Try")
    _test_run(draft, 2)
    url = reverse("admin:fmm_promptversion_delete", args=[draft.pk])
    html = admin_client.get(url).content.decode()
    assert "this also deletes its 2 test runs" in html
    assert admin_client.post(url, {"post": "yes"}).status_code == 302
    assert not PromptVersion.objects.filter(pk=draft.pk).exists() and not Insight.objects.exists()


def test_the_preview_shows_what_is_sent_without_calling_the_ai(admin_client, v1, monkeypatch, reporting_year):
    def no_call():
        raise AssertionError("the preview called the AI")

    monkeypatch.setattr(agent, "client", no_call)
    before = (PromptVersion.objects.count(), Insight.objects.count(), ModelCapability.objects.count())
    response = admin_client.get(reverse("admin:fmm_promptversion_preview", args=[v1.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert escape(profiles.compose(v1, "insights")) in html
    scope = Scope.from_params({"section": ""})
    assert escape(profiles.chat_instructions(v1, scope)) in html
    assert (
        "Ask NeuroDB&#x27;s own prompt is not sent to this chat" in html
        or "Ask NeuroDB's own prompt is not sent" in html
    )
    assert escape(agent.SYSTEM_PROMPT.splitlines()[0]) not in html
    # a pasted page address gives its own scope line
    pasted = admin_client.get(
        reverse("admin:fmm_promptversion_preview", args=[v1.pk]), {"url": "/fmm/?year=2025&section="}
    )
    assert (
        escape(profiles.scope_line(Scope.from_params({"year": "2025", "section": ""})))
        in pasted.content.decode()
    )
    assert (PromptVersion.objects.count(), Insight.objects.count(), ModelCapability.objects.count()) == before


def test_what_the_preview_shows_is_what_the_chat_request_sends(admin_client, v1, monkeypatch, reporting_year):
    from django.test import override_settings

    html = admin_client.get(reverse("admin:fmm_promptversion_preview", args=[v1.pk])).content.decode()
    scope = Scope.from_params({"section": ""})
    options = agent.RunOptions(
        base_prompt=profiles.compose(v1, "chat"), instructions=profiles.scope_line(scope)
    )
    model = FakeClient([reply(say("Two visits."))])
    monkeypatch.setattr(agent, "client", lambda: model)
    with override_settings(**ENABLED):
        list(agent.answer("How many?", [], agent.Outcome(), options=options))
    sent = model.requests[0]["instructions"]
    assert escape(sent) in html and sent == profiles.chat_instructions(v1, scope)


def test_only_administrators_change_prompts(client, roles, v1, viewer):
    editor = User.objects.create_user(username="editor", password="editor-pass-123456", is_staff=True)
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    draft = profiles.draft_from(v1, None, "d")
    for user in (editor, viewer):
        client.force_login(user)
        for name, args in (
            ("admin:fmm_promptversion_changelist", []),
            ("admin:fmm_promptversion_add", []),
            ("admin:fmm_promptversion_change", [draft.pk]),
            ("admin:fmm_promptversion_delete", [draft.pk]),
            ("admin:fmm_modelcapability_changelist", []),
        ):
            assert client.get(reverse(name, args=args)).status_code in (302, 403), (user, name)
        for name in ("preview", "publish", "roll_back"):
            url = reverse(f"admin:fmm_promptversion_{name}", args=[draft.pk])
            assert client.get(url).status_code in (302, 404), (user, name)
            assert client.post(url, {"note": "x"}).status_code in (302, 404), (user, name)
    draft.refresh_from_db()
    assert draft.status == "draft" and profiles.published() == v1


def test_sampling_checks_are_listed_and_deleting_one_means_check_again(admin_client):
    sampling.record("gpt-5.5", "low", "temperature", accepted=False, detail="Unsupported parameter")
    row = ModelCapability.objects.get()
    html = admin_client.get(reverse("admin:fmm_modelcapability_changelist")).content.decode()
    assert "gpt-5.5" in html and "refused" in html
    assert admin_client.get(reverse("admin:fmm_modelcapability_add")).status_code == 403
    assert (
        admin_client.post(
            reverse("admin:fmm_modelcapability_delete", args=[row.pk]), {"post": "yes"}
        ).status_code
        == 302
    )
    assert not ModelCapability.objects.exists()


# ------------------------------------------------------------------------------------------ stage 6a check
def test_a_new_draft_is_based_on_the_version_it_was_prefilled_from(admin_client, admin_user, v1):
    page = admin_client.get(reverse("admin:fmm_promptversion_add")).content.decode()
    assert f'name="from_version" value="{v1.pk}"' in page
    # another version is published while the form is open: the draft stays based on v1, as shown
    profiles.publish(profiles.draft_from(v1, admin_user, "v2"), admin_user)
    data = {**_form(v1, narratives_sampled="12"), "from_version": str(v1.pk)}
    assert admin_client.post(reverse("admin:fmm_promptversion_add"), data).status_code == 302
    draft = PromptVersion.objects.get(number=3)
    assert draft.based_on == v1 and draft.status == "draft"


def test_publishing_a_version_published_meanwhile_says_so(admin_client, admin_user, v1, monkeypatch):
    draft = profiles.draft_from(v1, admin_user, "v2")
    url = reverse("admin:fmm_promptversion_publish", args=[draft.pk])
    assert admin_client.get(url).status_code == 200
    profiles.publish(draft, admin_user)  # someone else published it since the page was opened
    response = admin_client.post(url, follow=True)
    assert response.status_code == 200 and "Only a draft can be published." in response.content.decode()

    # ... or between the view's own check and the publish (profiles.publish re-checks under a lock)
    def published_meanwhile(version, user):
        raise ValueError(f"Prompt v{version.number} is published: only a draft can be published.")

    monkeypatch.setattr(profiles, "publish", published_meanwhile)
    other = profiles.draft_from(v1, admin_user, "v3")
    response = admin_client.post(reverse("admin:fmm_promptversion_publish", args=[other.pk]), follow=True)
    assert response.status_code == 200 and "Only a draft can be published." in response.content.decode()


def test_the_publish_confirmation_says_the_published_version_will_be_retired(admin_client, admin_user, v1):
    draft = profiles.draft_from(v1, admin_user, "v2")
    html = admin_client.get(reverse("admin:fmm_promptversion_publish", args=[draft.pk])).content.decode()
    assert "v1, published now, will be retired" in html


def test_bulk_delete_lists_published_versions_as_kept_and_deletes_drafts_only(admin_client, admin_user, v1):
    draft = profiles.draft_from(v1, admin_user, "Try")
    _test_run(draft)
    changelist = reverse("admin:fmm_promptversion_changelist")
    data = {"action": "delete_selected", "_selected_action": [v1.pk, draft.pk]}
    html = admin_client.post(changelist, data).content.decode()
    assert "Prompt v1 (published: kept, never deleted)" in html
    assert "Prompt v2 (this also deletes its test run)" in html
    response = admin_client.post(changelist, {**data, "post": "yes"}, follow=True)
    assert "never deleted: 1 kept" in response.content.decode()
    assert list(PromptVersion.objects.values_list("number", flat=True)) == [1]
    assert not Insight.objects.exists()
