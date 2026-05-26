from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

from aiogram.client.session.aiohttp import AiohttpSession


def telegram_proxy_url(kind: str = "telegram") -> str | None:
    env_by_kind = {
        "bot": "TELEGRAM_BOT_PROXY",
        "telethon": "TELETHON_PROXY",
        "telegram": "TELEGRAM_PROXY",
    }
    specific = os.getenv(env_by_kind[kind], "").strip()
    if specific:
        return specific

    proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    return proxy or None


def proxy_label(kind: str = "telegram") -> str | None:
    proxy = telegram_proxy_url(kind)
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if parsed.hostname and parsed.port:
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return proxy


def aiogram_session_from_env() -> AiohttpSession | None:
    proxy = telegram_proxy_url("bot")
    if not proxy:
        return None
    return AiohttpSession(proxy=proxy)


def telethon_proxy_from_env() -> dict | None:
    proxy = telegram_proxy_url("telethon")
    if not proxy:
        return None

    parsed = urlparse(proxy)
    scheme = parsed.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"socks4", "socks5", "http"}:
        raise ValueError("Telegram proxy must start with socks4://, socks5://, socks5h://, or http://")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Telegram proxy must include host and port, for example socks5://127.0.0.1:1080")

    return {
        "proxy_type": scheme,
        "addr": parsed.hostname,
        "port": parsed.port,
        "rdns": True,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }
