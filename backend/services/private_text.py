"""Private resume value handling without importing site adapters."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse


class ResumeImportError(ValueError):
    """Safe user-facing import/validation error (never contains page text)."""


_MARKER = re.compile(
    r"(?:\{\{.*?\}\}|\{%.*?%\}|<%.*?%>|\$\{.*?\}|\[[^\]\r\n]{1,160}\])",
    re.DOTALL,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_PHONE = re.compile(r"(?<!\w)(?:\+?7|8)[\s().-]*(?:\d[\s().-]*){9,10}(?!\w)")
_DIRECT_URL = re.compile(
    r'(?i)(?:https?://|ftp://|mailto:|tel:|www\.)[^\s<>"\']+'
    r"|(?<![\w@])(?:github\.com|gitlab\.com|linkedin\.com|facebook\.com|"
    r'instagram\.com|behance\.net|dribbble\.com)(?:/[^\s<>"\']*)?'
)


def _unseal_private(value: str) -> dict:
    try:
        if value.startswith("dpapi:"):
            from backend.persistence.crypto import decrypt_secret

            payload = decrypt_secret(value[6:])
        elif value.startswith("sealed-test:"):
            payload = base64.urlsafe_b64decode(value[11:]).decode("utf-8")
        else:
            raise ValueError
        data = json.loads(payload)
    except Exception as exc:
        raise ResumeImportError("Повреждён защищённый снимок резюме") from exc
    if not isinstance(data, dict):
        raise ResumeImportError("Повреждён защищённый снимок резюме")
    return data


def _private_groups(private: Any) -> tuple[dict, dict]:
    if isinstance(private, str):
        private = _unseal_private(private)
    model_dump = getattr(private, "model_dump", None)
    data = model_dump(mode="json") if callable(model_dump) else dict(private or {})
    identity = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
    contacts = data.get("contacts", {}) if isinstance(data.get("contacts"), dict) else {}
    return identity, contacts


def _messenger_values(private: Any) -> list[str]:
    _, contacts = _private_groups(private)
    value = contacts.get("messengers", [])
    if isinstance(value, dict) and "value" in value and "availability" in value:
        if value.get("availability") != "present":
            return []
        value = value.get("value")
    elif isinstance(value, dict) and "value" in value:
        value = value.get("value")
    if not isinstance(value, list):
        value = [value] if value is not None else []
    return [str(item).strip() for item in value if str(item).strip()]


def _messenger_urls(private: Any) -> list[str]:
    """Return exact saved messenger URLs in source order, without service labels."""
    urls: list[str] = []
    prefixed_url = re.compile(
        r"(?i)(?:viber|telegram|whatsapp|setka|сетка)\s*:\s*((?:https?://|viber://)\S+)"
    )
    for value in _messenger_values(private):
        match = prefixed_url.fullmatch(value)
        url = match.group(1) if match else value
        if any(unicodedata.category(char).startswith("C") for char in url):
            continue
        if any(rendered.endswith(url) for rendered in _format_messengers([value])):
            urls.append(url)
    return urls


def _flat_private(private: Any) -> dict[str, str]:
    identity, contacts = _private_groups(private)
    result: dict[str, str] = {}
    for group in (identity, contacts):
        for key, value in group.items():
            if isinstance(value, dict) and "value" in value:
                value = value.get("value")
            if isinstance(value, list):
                if key.casefold() == "messengers":
                    value = "\n".join(str(item).strip() for item in value if str(item).strip())
                else:
                    value = ", ".join(str(item).strip() for item in value if str(item).strip())
            if value is not None and str(value).strip():
                result[key.casefold()] = str(value).strip()
    result.update({"fio": result.get("full_name", ""), "name": result.get("full_name", "")})
    return result


def redact_private_text(text: str, private: Any = None) -> str:
    """Remove private literals before arbitrary text enters a model payload."""
    values: list[str] = []
    if private is not None:
        values.extend(value for value in _flat_private(private).values() if len(value) >= 2)
        values.extend(value for value in _messenger_values(private) if len(value) >= 2)
    result = str(text or "")
    for value in sorted({item.casefold() for item in values}, key=len, reverse=True):
        result = re.sub(re.escape(value), "[private value omitted]", result, flags=re.I)
    result = _EMAIL.sub("[email omitted]", result)
    result = _PHONE.sub("[phone omitted]", result)
    return _DIRECT_URL.sub("[link omitted]", result)


def render_local_private(text: str, private: Any) -> str:
    """Render private placeholders locally and reject unresolved markers."""
    values = _flat_private(private)
    messenger_values = _messenger_values(private)
    values.update({f"messenger_{index}": url for index, url in enumerate(_messenger_urls(private))})
    messenger_lines = _format_messengers(messenger_values)
    aliases = {
        "full_name": "full_name", "фио": "full_name", "имя": "full_name",
        "phone": "phone", "телефон": "phone", "email": "email", "почта": "email",
        "messengers": "messengers", "мессенджеры": "messengers",
    }
    pattern = re.compile(r"\{\{\s*([\wа-яё.-]+)\s*\}\}|\[\s*([^\]\r\n]{1,100})\s*\]")

    def marker_key(match: re.Match[str]) -> str:
        key = (match.group(1) or match.group(2) or "").casefold().strip()
        return aliases.get(key, key)

    def is_contact_line(line: str) -> bool:
        plain = pattern.sub("", line).casefold()
        return bool(re.search(
            r"(?:\b(?:телефон|phone|мобильн\w*|email|e[- ]?mail|почт\w*|"
            r"мессенджер\w*|messenger\w*|telegram|whatsapp|телеграм)\b|"
            r"(?:^|\s)(?:фио|full\s+name|с\s+уважением)(?:\s|:|,|$))",
            plain,
        ))

    prepared_lines: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        markers = list(pattern.finditer(line))
        messenger_markers = [marker for marker in markers if marker_key(marker) == "messengers"]
        if messenger_markers:
            first_marker = messenger_markers[0]
            marker_text = line[first_marker.start():first_marker.end()]
            prefix = line[:first_marker.start()]
            suffix = line[first_marker.end():]
            if not messenger_lines and is_contact_line(line):
                continue
            if re.fullmatch(
                r"\s*(?:[-*•]\s*)?(?:viber|telegram|whatsapp|setka|сетка|мессенджеры|messengers)\s*:\s*",
                prefix,
                flags=re.I,
            ):
                prefix = ""
            # The messenger list owns its output lines, so surrounding spaces
            # must not become part of the first or last rendered contact.
            if not messenger_lines and not prefix.strip() and not suffix.strip():
                continue
            line = prefix.rstrip() + marker_text + suffix.lstrip()
            markers = list(pattern.finditer(line))
            messenger_markers = [marker for marker in markers if marker_key(marker) == "messengers"]
        if markers and (is_contact_line(line) or messenger_markers) and any(marker_key(marker) not in values for marker in markers):
            continue
        if not markers:
            line = _normalize_saved_messenger_line(line, messenger_values)
        prepared_lines.append(line)

    def replace(match: re.Match[str]) -> str:
        key = marker_key(match)
        if key == "messengers":
            if not messenger_lines:
                return ""
            line_start = match.string.rfind("\n", 0, match.start()) + 1
            line_end = match.string.find("\n", match.end())
            if line_end < 0:
                line_end = len(match.string)
            is_only_content = not match.string[line_start:match.start()].strip() and not match.string[match.end():line_end].strip()
            contacts = "\n".join(messenger_lines)
            return contacts if is_only_content else f"\n{contacts}\n"
        return values.get(key, "")

    rendered = pattern.sub(replace, "\n".join(prepared_lines))
    rendered = re.sub(r"\[+[^\]\r\n]*\]+", "", rendered)
    rendered = re.sub(r"\{+[^}\r\n]*\}+", "", rendered)
    rendered = re.sub(r"\{\{[^{}\r\n]*(?:\}\}|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"\[\[[^\[\]\r\n]*(?:\]\]|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"<%[^<>\r\n]*(?:%>|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"\$\{[^{}\r\n]*(?:\}|$)", "", rendered, flags=re.MULTILINE)
    rendered = _MARKER.sub("", rendered)
    rendered = _CONTROL.sub("", rendered)
    rendered = "".join(char for char in rendered if unicodedata.category(char) != "Cf" or char in "\n\t")
    rendered = re.sub(r"(?m)^\s*[-*•]\s*$\n?", "", rendered)
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    rendered = re.sub(r"[ \t]+([,:;])", r"\1", rendered)
    rendered = rendered.replace("[", "").replace("]", "")
    rendered = "\n".join(line.rstrip() for line in rendered.splitlines()).strip()
    if _MARKER.search(rendered) or any(token in rendered for token in ("{{", "}}", "[[", "]]")):
        raise ResumeImportError("Не удалось безопасно собрать текст письма или анкеты")
    return rendered


def _format_messengers(values: list[str]) -> list[str]:
    """Format only URL-backed messenger contacts, preserving source order."""
    formatted: list[str] = []
    explicit_handles = {
        "telegram": "Telegram", "телеграм": "Telegram",
        "whatsapp": "WhatsApp", "вайбер": "Viber", "viber": "Viber",
        "setka": "Сетка", "сетка": "Сетка",
    }
    for value in values:
        # Legacy templates sometimes saved the old service label as part of
        # the value. Resolve known URL contacts from the URL itself.
        prefixed_url = re.fullmatch(
            r"(?i)(?:viber|telegram|whatsapp|setka|сетка)\s*:\s*((?:https?://|viber://)\S+)",
            value,
        )
        if prefixed_url:
            value = prefixed_url.group(1)
        # Keep only single-line values; controls can smuggle extra contacts or
        # alter the visual label in the rendered contact block.
        if any(unicodedata.category(char).startswith("C") for char in value):
            continue
        handle = re.fullmatch(r"([^:]{1,32}):\s*(@[A-Za-z0-9_]{1,64})", value)
        if handle and handle.group(1).casefold() in explicit_handles:
            formatted.append(f"{explicit_handles[handle.group(1).casefold()]}: {handle.group(2)}")
            continue
        if any(char.isspace() for char in value):
            continue
        try:
            parsed = urlparse(value)
            scheme = parsed.scheme.casefold()
            # Never display URLs that embed credentials.
            if parsed.username is not None or parsed.password is not None:
                continue
            _ = parsed.port  # Accessing it validates the port syntax and range.
            hostname = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            # urlparse raises on malformed bracketed hosts and invalid ports.
            continue
        if scheme in {"http", "https"} and hostname:
            if hostname in {"wa.me", "whatsapp.com", "www.whatsapp.com", "api.whatsapp.com"}:
                label = "WhatsApp"
            elif hostname in {"t.me", "telegram.me", "www.telegram.me"}:
                label = "Telegram"
            elif hostname in {"set.ki", "www.set.ki"}:
                label = "Сетка"
            elif hostname in {"viber.com", "www.viber.com", "account.viber.com"}:
                label = "Viber"
            else:
                label = hostname
        elif scheme == "viber" and (parsed.netloc or parsed.path):
            label = "Viber"
        else:
            # Bare service names and malformed/non-URL values are not contacts.
            continue
        formatted.append(f"{label}: {value}")
    return formatted


def _normalize_saved_messenger_line(line: str, messenger_values: list[str]) -> str:
    """Correct a standalone contact line only when its URL exactly matches saved data."""
    candidate = re.fullmatch(
        r"\s*(?:[-*•]\s*)?(?:(?:viber|telegram|whatsapp|setka|сетка)\s*:\s*)?"
        r"((?:https?://|viber://)\S+)\s*",
        line,
        flags=re.I,
    )
    if not candidate:
        return line
    url = candidate.group(1)
    for saved_value in messenger_values:
        normalized = re.fullmatch(
            r"(?i)(?:viber|telegram|whatsapp|setka|сетка)\s*:\s*((?:https?://|viber://)\S+)",
            saved_value,
        )
        saved_url = normalized.group(1) if normalized else saved_value
        if url == saved_url:
            formatted = _format_messengers([saved_value])
            if formatted:
                return formatted[0]
    return line


render_private_placeholders = render_local_private
