from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent

with (ROOT / "config.yaml").open(encoding="utf-8") as f:
    CFG: dict[str, Any] = yaml.safe_load(f)

BLOCK_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in CFG["filter"]["block_patterns"]]
STRIP_RE = [re.compile(p, re.IGNORECASE) for p in CFG["filter"]["strip_lines"]]


def is_ad(text: str) -> bool:
    return any(r.search(text) for r in BLOCK_RE)


def clean_text(text: str) -> str:
    lines = [line for line in text.split("\n") if not any(r.search(line) for r in STRIP_RE)]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def chunk_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text] if text else []

    out: list[str] = []
    cur = ""

    def split_long_part(part: str) -> list[str]:
        chunks = []
        rest = part.strip()
        while len(rest) > max_len:
            cut = rest.rfind(" ", 0, max_len)
            if cut < max_len // 2:
                cut = max_len
            chunks.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            chunks.append(rest)
        return chunks

    for para in text.split("\n\n"):
        parts = split_long_part(para) if len(para) > max_len else [para]
        for part in parts:
            if not part:
                continue
            next_chunk = f"{cur}\n\n{part}" if cur else part
            if len(next_chunk) <= max_len:
                cur = next_chunk
                continue
            if cur:
                out.append(cur)
            cur = part

    if cur:
        out.append(cur)
    return out
