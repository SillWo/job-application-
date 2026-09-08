from __future__ import annotations

import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urlparse


def validate_base_url(value: str, allowed_local_urls: Collection[str] = ()) -> str:
    if len(value.strip()) > 2048:
        raise ValueError("Недопустимый адрес модели")
    parsed = urlparse(value.strip().rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Недопустимый адрес модели")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError("Не удалось проверить адрес модели") from exc
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips:
        raise ValueError("Не удалось проверить адрес модели")
    normalized = parsed.geturl()
    allowed_local = {url.strip().rstrip("/") for url in allowed_local_urls if url.strip()}
    try:
        explicit_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        explicit_loopback = parsed.hostname.rstrip(".").lower() == "localhost"
    # A fresh installation must be able to configure a local gateway from the UI.
    # Arbitrary DNS names still cannot opt into loopback/private destinations.
    if (explicit_loopback or normalized in allowed_local) and all(ip.is_loopback for ip in ips):
        return normalized
    if parsed.scheme != "https" or any(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        for ip in ips
    ):
        raise ValueError("Адрес модели указывает на закрытую сеть или использует небезопасный HTTP")
    return normalized
