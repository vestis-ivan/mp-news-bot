r"""Dry-run AI analysis for recent Telegram channel posts.

Examples:
  .venv\Scripts\python.exe test_ai_from_posts.py maxprowb --limit 10 --analyze 3
  .venv\Scripts\python.exe test_ai_from_posts.py ozonmarketplace marketpsy --limit 5

The script reads recent posts, skips ads by the bot filters, sends selected posts
to OpenAI, and prints summaries. It does not send anything to Telegram chats.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from app.ai_client import make_openai_client  # noqa: E402
from app.common import CFG, clean_text, is_ad  # noqa: E402
from app.proxy import proxy_label, telethon_proxy_from_env  # noqa: E402

SESSION_PATH = ROOT / "data" / "user"


def explain_exception(e: BaseException) -> str:
    parts = [f"{type(e).__name__}: {e}"]
    cur = e.__cause__ or e.__context__
    while cur:
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze recent Telegram posts with OpenAI without posting them.")
    p.add_argument("channels", nargs="*", default=["maxprowb"], help="Channel usernames, with or without @")
    p.add_argument("--limit", type=int, default=10, help="How many recent posts to scan per channel")
    p.add_argument("--analyze", type=int, default=3, help="How many non-ad text posts to analyze")
    p.add_argument("--min-chars", type=int, default=120, help="Skip shorter posts")
    return p.parse_args()


def username(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/")
    return value.lstrip("@").strip("/")


async def summarize(client, text: str) -> tuple[str, object | None]:
    resp = await client.chat.completions.create(
        model=CFG["ai"]["model"],
        messages=[
            {"role": "system", "content": CFG["ai"]["system_prompt"]},
            {"role": "user", "content": text},
        ],
        max_tokens=CFG["ai"]["max_tokens"],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip(), resp.usage


async def main() -> int:
    args = parse_args()

    required = ["TG_API_ID", "TG_API_HASH", "TG_PHONE", "OPENAI_API_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        return 1

    tg_api_id = int(os.environ["TG_API_ID"])
    tg_api_hash = os.environ["TG_API_HASH"]
    tg_phone = os.environ["TG_PHONE"]
    ai = make_openai_client(os.environ["OPENAI_API_KEY"])

    proxy = telethon_proxy_from_env()
    if proxy:
        print(f"Telethon proxy: {proxy_label('telethon')}")

    tg = TelegramClient(str(SESSION_PATH), tg_api_id, tg_api_hash, proxy=proxy)
    await tg.start(phone=tg_phone)

    analyzed = 0
    total_prompt = 0
    total_completion = 0

    try:
        for raw_channel in args.channels:
            ch = username(raw_channel)
            entity = await tg.get_entity(ch)
            title = getattr(entity, "title", ch)
            print()
            print("=" * 72)
            print(f"@{ch} - {title}")
            print("=" * 72)

            async for msg in tg.iter_messages(entity, limit=args.limit):
                if analyzed >= args.analyze:
                    break

                text = (msg.text or msg.message or "").strip()
                if not text:
                    continue
                if is_ad(text):
                    print(f"skip ad: https://t.me/{ch}/{msg.id}")
                    continue

                cleaned = clean_text(text)
                if len(cleaned) < args.min_chars:
                    continue

                analyzed += 1
                print()
                print(f"Post {analyzed}: https://t.me/{ch}/{msg.id}")
                print(f"Cleaned length: {len(cleaned)} chars")
                print("-" * 72)
                print(cleaned[:900] + ("..." if len(cleaned) > 900 else ""))
                print("-" * 72)

                try:
                    summary, usage = await summarize(ai, cleaned)
                except Exception as e:
                    print("OpenAI error:")
                    print(explain_exception(e))
                    print()
                    print("If Telegram works but OpenAI fails, set OPENAI_PROXY in .env, for example:")
                    print("OPENAI_PROXY=socks5://127.0.0.1:1080")
                    print("or use OPENAI_BASE_URL for an OpenAI-compatible provider.")
                    return 2

                print("AI summary:")
                print(summary)
                if usage:
                    total_prompt += usage.prompt_tokens
                    total_completion += usage.completion_tokens
                    print(f"Tokens: input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

            if analyzed >= args.analyze:
                break
    finally:
        await tg.disconnect()

    if analyzed == 0:
        print("No suitable text posts found. Try a larger --limit or another channel.")
        return 3

    if total_prompt or total_completion:
        price_in = 0.15 / 1_000_000
        price_out = 0.60 / 1_000_000
        cost = total_prompt * price_in + total_completion * price_out
        print()
        print("=" * 72)
        print(f"Analyzed posts: {analyzed}")
        print(f"Total tokens: input={total_prompt}, output={total_completion}")
        print(f"Estimated cost: ${cost:.6f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
