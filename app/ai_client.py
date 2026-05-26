from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI


def _require_url_scheme(name: str, value: str, allowed: set[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in allowed:
        expected = " or ".join(f"{scheme}://" for scheme in sorted(allowed))
        raise ValueError(f"{name} must start with {expected}. Current value: {value!r}")


def make_openai_client(api_key: str | None = None) -> AsyncOpenAI:
    kwargs = {"api_key": api_key or os.getenv("OPENAI_API_KEY", "")}

    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        _require_url_scheme("OPENAI_BASE_URL", base_url, {"http", "https"})
    kwargs["base_url"] = base_url or "https://api.openai.com/v1"

    proxy = os.getenv("OPENAI_PROXY", "").strip()
    if proxy:
        _require_url_scheme("OPENAI_PROXY", proxy, {"http", "https", "socks4", "socks5", "socks5h"})
        kwargs["http_client"] = httpx.AsyncClient(proxies=proxy)

    return AsyncOpenAI(**kwargs)
