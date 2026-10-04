import base64
import json

import pytest

from backend.intelligence.security import PromptInjectionDetected, assert_safe_outgoing_text
from backend.orchestrator.workflow import (
    _assert_safe_application_plan,
    _redact_private_string,
    _render_private_plan,
)
from backend.schemas.domain import ApplicationPlan
from backend.services.private_text import redact_private_text
from backend.services.resume_session import render_local_private


def _private(**values):
    identity = {}
    contacts = {}
    for key, value in values.items():
        target = identity if key == "full_name" else contacts
        target[key] = {"value": value, "availability": "present"}
    return {"identity": identity, "contacts": contacts}


def test_missing_contact_values_remove_only_contact_lines():
    result = render_local_private(
        "Профессиональный текст.\n"
        "Телефон: {{phone}}\n"
        "Почта: {{email}}\n"
        "- Мессенджеры: {{messengers}}\n"
        "С уважением, {{full_name}}\n"
        "Следующий абзац.",
        _private(full_name="Ada Lovelace"),
    )
    assert result == "Профессиональный текст.\nС уважением, Ada Lovelace\nСледующий абзац."


def test_missing_marker_does_not_delete_neighboring_prose():
    result = render_local_private(
        "Начало: {{unknown}} остаётся частью абзаца.\n"
        "Email: {{email}}\n"
        "Продолжение.",
        _private(),
    )
    assert result == "Начало: остаётся частью абзаца.\nПродолжение."


def test_crlf_and_malformed_markers_are_scrubbed_without_service_symbols():
    result = render_local_private(
        "До\r\nТелефон: {{phone}}\r\nПосле\r\n{{unknown\r\n[[email\r\n",
        _private(),
    )
    assert result == "До\nПосле"
    assert not any(marker in result for marker in ("{{", "}}", "[[", "]]", "<%", "%>"))


def test_legacy_messenger_values_render_each_url_with_its_own_label():
    private = _private(messengers=[
        "https://wa.me/1234567890",
        "Viber",
        "https://t.me/ada",
        "https://set.ki/ada",
    ])
    result = render_local_private("{{messengers}}", private)
    assert result == (
        "WhatsApp: https://wa.me/1234567890\n"
        "Telegram: https://t.me/ada\n"
        "Сетка: https://set.ki/ada"
    )


def test_messenger_marker_expands_inline_and_discards_wrong_service_prefix():
    private = _private(messengers=["https://t.me/ada", "https://wa.me/123"])
    assert render_local_private("Viber: {{messengers}}", private) == (
        "Telegram: https://t.me/ada\nWhatsApp: https://wa.me/123"
    )
    assert render_local_private("Начало {{messengers}} конец", private) == (
        "Начало\nTelegram: https://t.me/ada\nWhatsApp: https://wa.me/123\nконец"
    )


def test_bare_names_missing_and_empty_messengers_are_removed():
    assert render_local_private("Мессенджеры: {{messengers}}", _private(messengers=[])) == ""
    assert render_local_private("Viber: {{messengers}}", _private(messengers=["Viber", "Telegram"])) == ""
    assert render_local_private("До\n{{messengers}}\nПосле", _private()) == "До\nПосле"


def test_messenger_domains_are_matched_exactly_and_unknown_urls_are_labeled_by_host():
    private = _private(messengers=[
        "https://t.me.evil.test/user",
        "https://t.me/user",
        "https://chat.example.test/contact",
        "viber://chat?number=123",
    ])
    assert render_local_private("{{messengers}}", private) == (
        "t.me.evil.test: https://t.me.evil.test/user\n"
        "Telegram: https://t.me/user\n"
        "chat.example.test: https://chat.example.test/contact\n"
        "Viber: viber://chat?number=123"
    )


def test_sourcefield_wrapper_and_sealed_private_render_and_redact_each_messenger():
    data = {
        "identity": {},
        "contacts": {"messengers": {"value": ["https://t.me/synthetic-user", "Viber", "https://wa.me/555"], "availability": "present"}},
    }
    sealed = "sealed-test:" + base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    assert render_local_private("{{messengers}}", sealed) == (
        "Telegram: https://t.me/synthetic-user\nWhatsApp: https://wa.me/555"
    )
    redacted_plan = _redact_private_string(
        "Telegram: https://t.me/synthetic-user\nWhatsApp: https://wa.me/555",
        sealed,
    )
    assert redacted_plan == "Telegram: {{messenger_0}}\nWhatsApp: {{messenger_1}}"
    assert render_local_private(redacted_plan, sealed) == (
        "Telegram: https://t.me/synthetic-user\nWhatsApp: https://wa.me/555"
    )
    redacted = redact_private_text("About: https://t.me/synthetic-user and https://wa.me/555", sealed)
    assert "https://t.me/synthetic-user" not in redacted
    assert "https://wa.me/555" not in redacted


def test_malformed_multiline_and_credential_urls_are_omitted():
    private = _private(messengers=[
        "https://[broken",
        "https://t.me/user\nhttps://wa.me/123",
        "https://user:password@t.me/private",
        "https://t.me/public",
    ])
    assert render_local_private("{{messengers}}", private) == "Telegram: https://t.me/public"


def test_explicit_messenger_handle_is_kept_but_unlabelled_handle_and_prose_are_omitted():
    private = _private(messengers=[
        "Telegram: @synthetic",
        "@unlabelled",
        "Please contact me on Telegram: @invented",
        "Viber",
    ])
    assert render_local_private("{{messengers}}", private) == "Telegram: @synthetic"


def test_standalone_literal_contact_lines_use_saved_url_labels_only_on_exact_match():
    private = _private(messengers=[
        "https://t.me/synthetic-user",
        "https://set.ki/synthetic-user",
    ])
    assert render_local_private(
        "Viber: https://t.me/synthetic-user\n"
        "https://set.ki/synthetic-user\n"
        "Keep this prose: https://t.me/synthetic-user",
        private,
    ) == (
        "Telegram: https://t.me/synthetic-user\n"
        "Сетка: https://set.ki/synthetic-user\n"
        "Keep this prose: https://t.me/synthetic-user"
    )


def test_legacy_prefixed_url_is_classified_from_its_actual_url():
    assert render_local_private(
        "{{messengers}}",
        _private(messengers=["Viber: https://t.me/synthetic-user"]),
    ) == "Telegram: https://t.me/synthetic-user"


def test_plan_redaction_preserves_each_exact_messenger_url_for_local_rendering():
    private = _private(
        phone="+7 (999) 123-45-67",
        messengers=[
            "WhatsApp: https://wa.me/79991234567",
            "Telegram: https://t.me/synthetic%2Fuser?start=hello%20there",
            "https://set.ki/ada",
        ],
    )
    source = (
        "WhatsApp: https://wa.me/79991234567\n"
        "Telegram: https://t.me/synthetic%2Fuser?start=hello%20there\n"
        "Сетка: https://set.ki/ada"
    )
    redacted = _redact_private_string(source, private)
    assert redacted == (
        "WhatsApp: {{messenger_0}}\n"
        "Telegram: {{messenger_1}}\n"
        "Сетка: {{messenger_2}}"
    )
    assert _redact_private_string(redacted, private) == redacted
    assert render_local_private(redacted, private) == source


def test_redaction_does_not_trust_a_model_url_that_is_not_a_saved_contact():
    private = _private(messengers=["https://t.me/saved-user"])
    model_url = "Telegram: https://t.me/another-user"
    assert _redact_private_string(model_url, private) == model_url
    with pytest.raises(PromptInjectionDetected) as error:
        assert_safe_outgoing_text(model_url, {}, [private], context="test")
    assert error.value.reason_code == "untrusted_outbound_url"


def test_url_redaction_is_case_sensitive_and_masks_generic_private_patterns():
    private = _private(
        phone="+7 999 123-45-67",
        messengers=["https://t.me/TestX", "https://wa.me/79991234567"],
    )
    lowercased = "https://t.me/testx"
    arbitrary_phone_url = "https://evil.example/79991234567"
    assert _redact_private_string(lowercased, private) == lowercased
    assert _redact_private_string(arbitrary_phone_url, private) == arbitrary_phone_url
    with pytest.raises(PromptInjectionDetected) as error:
        assert_safe_outgoing_text(lowercased, {}, [private], context="cached_plan")
    assert error.value.reason_code == "untrusted_outbound_url"


def test_url_with_embedded_unknown_marker_cannot_render_as_trusted_contact():
    private = _private(
        phone="+7 999 123-45-67",
        messengers=["https://wa.me/79991234567", "https://t.me/TestX"],
    )
    malformed = "https://wa.me/79991234567{{unexpected}}"
    redacted = _redact_private_string(malformed, private)
    assert redacted == "{{messenger_0}}{{unexpected}}"
    for cached_text in (malformed, redacted):
        plan = ApplicationPlan(vacancy_id=1, resume_file="", cover_letter=cached_text)
        with pytest.raises(PromptInjectionDetected) as error:
            rendered = _render_private_plan(plan, private)
            _assert_safe_application_plan(
                rendered, {}, [private], context="cached_application_plan"
            )
        assert error.value.reason_code == "untrusted_outbound_url"
