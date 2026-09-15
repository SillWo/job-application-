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
