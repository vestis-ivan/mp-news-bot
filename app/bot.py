"""MP News Bot — главный модуль.

Запускает два конкурентных воркера:
  1) Telethon user-client — слушает каналы-источники и заливает в БД-очереди.
  2) aiogram bot — админ-панель в Telegram.

И два таймера:
  3) Дайджест ozon_novosti (каждый час 09-21 МСК).
  4) Дайджест уикэнд-постов (пн 09:00 МСК).

Источник правды — SQLite (data/state.db). config.yaml только для первичного посева.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, Message

from app.ai_client import make_openai_client
from app import admin_bot, state as st
from app.common import CFG, chunk_text, clean_text, is_ad
from app.proxy import aiogram_session_from_env, proxy_label, telethon_proxy_from_env

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------- Конфиг и логирование ----------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Telethon очень болтлив на INFO
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("mp-news-bot")

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_PHONE = os.environ["TG_PHONE"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

DB_PATH = ROOT / "data" / "state.db"
SESSION_PATH = ROOT / "data" / "user"

MSK = ZoneInfo("Europe/Moscow")

# ---------- Фильтры -----------------------------------------------------------



# ---------- AI-саммари --------------------------------------------------------

ai: AsyncOpenAI | None = None
if CFG["ai"]["enabled"] and OPENAI_API_KEY:
    ai = make_openai_client(OPENAI_API_KEY)


async def summarize(text: str) -> str | None:
    if not ai or len(text) < CFG["ai"]["summarize_if_longer_than"]:
        return None
    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {"role": "system", "content": CFG["ai"]["system_prompt"]},
                {"role": "user", "content": text},
            ],
            max_tokens=CFG["ai"]["max_tokens"],
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("OpenAI fail: %s", e)
        return None


async def ai_is_ad(text: str) -> bool:
    ai_filter = CFG.get("filter", {}).get("ai_ad_filter", {})
    if not ai or not ai_filter.get("enabled", False):
        return False

    sample = text.strip()
    if not sample:
        return False

    max_chars = int(ai_filter.get("max_chars", 2500))
    sample = sample[:max_chars]

    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify a Telegram post for a Russian marketplace news digest. "
                        "Return only AD or OK. "
                        "AD means the post is mainly paid promotion, self-promotion, "
                        "selling a service/course/webinar, referral/coupon pitch, or a call "
                        "to buy, subscribe, register, or contact the author. "
                        "OK means factual marketplace news, platform updates, policy changes, "
                        "analytics, seller tips, or useful observations. "
                        "If unsure, choose OK."
                    ),
                },
                {"role": "user", "content": sample},
            ],
            max_tokens=3,
            temperature=0,
        )
        verdict = (resp.choices[0].message.content or "").strip().upper()
        return verdict.startswith("AD")
    except Exception as e:
        log.warning("OpenAI ad filter fail: %s", e)
        return False


async def ai_classify_post(text: str) -> str:
    ai_filter = CFG.get("filter", {}).get("ai_ad_filter", {})
    if not ai or not ai_filter.get("enabled", False):
        return "KEEP"

    sample = text.strip()
    if not sample:
        return "NOISE"

    max_chars = int(ai_filter.get("max_chars", 2500))
    sample = sample[:max_chars]

    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify a Russian Telegram post for a marketplace seller digest. "
                        "Return only one token: KEEP, AD, or NOISE. "
                        "KEEP: factual marketplace news, rule changes, deadlines, fees, logistics, "
                        "ads platform changes, useful cases with numbers, practical tips. "
                        "AD: paid promotion, self-promotion, course/webinar/service sales, referral links, "
                        "giveaways, livestream announcements, calls to register/contact/buy/subscribe. "
                        "NOISE: opinions without facts, polls, jokes, personal stories, vague motivation, "
                        "minor cases without useful lesson, repeated discussion prompts. "
                        "If unsure between KEEP and NOISE, choose KEEP only when there is a concrete fact "
                        "or applicable seller insight."
                    ),
                },
                {"role": "user", "content": sample},
            ],
            max_tokens=4,
            temperature=0,
        )
        verdict = (resp.choices[0].message.content or "").strip().upper()
        if verdict.startswith("AD"):
            return "AD"
        if verdict.startswith("NOISE"):
            return "NOISE"
        return "KEEP"
    except Exception as e:
        log.warning("OpenAI post classifier fail: %s", e)
        return "KEEP"


async def check_openai_status() -> tuple[bool, str]:
    if not CFG.get("ai", {}).get("enabled", False):
        return False, "disabled in config.yaml"
    if not ai:
        return False, "OPENAI_API_KEY is empty"
    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {"role": "system", "content": "Reply with OK."},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=3,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        return True, text or "OK"
    except Exception as e:
        return False, str(e)


# ---------- Форматирование ----------------------------------------------------

def format_msg(*, source: str, title: str, msg_id: int, body: str, summary: str | None) -> str:
    link = f"https://t.me/{source}/{msg_id}"
    header = f"📰 <b>{title}</b>"
    if summary:
        return (
            f"{header}\n\n<i>{summary}</i>\n\n———\n{body}\n\n"
            f'<a href="{link}">Открыть оригинал</a>'
        )
    return f'{header}\n\n{body}\n\n<a href="{link}">Открыть оригинал</a>'


# ---------- Отправка ----------------------------------------------------------

async def send_to_target(
    out_bot: Bot, client: TelegramClient, conn, category: str, *,
    text: str, media: Any = None,
) -> bool:
    """Шлёт в целевой чат/тему ОТ ИМЕНИ БОТА (admin-bot должен быть админом канала).

    Если есть медиа — скачиваем bytes у user-клиента и отправляем через aiogram.
    Это надёжнее, чем форвард, потому что бот может писать в любую группу/тему
    где он админ, в отличие от user-аккаунта.
    """
    t = st.get_target(conn, category)
    if not t:
        log.warning("Нет таргета для %s — /here ещё не вызывали", category)
        return False
    chat_id, thread_id = t
    try:
        if media:
            buf = await client.download_media(media, file=bytes)
            if isinstance(buf, (bytes, bytearray)):
                from aiogram.types import BufferedInputFile
                file = BufferedInputFile(bytes(buf), filename="media.bin")
                # Различаем фото/видео примитивно: фото если небольшое и jpeg-подобное
                # aiogram сам решит как отрисовать через sendDocument-fallback ниже,
                # но для красоты пробуем photo сначала
                try:
                    await out_bot.send_photo(
                        chat_id, file, caption=text[:1024], message_thread_id=thread_id,
                    )
                    return True
                except Exception:
                    await out_bot.send_document(
                        chat_id, file, caption=text[:1024], message_thread_id=thread_id,
                    )
                    return True
        await out_bot.send_message(
            chat_id, text, message_thread_id=thread_id, disable_web_page_preview=False,
        )
        return True
    except Exception as e:
        log.exception("send_to_target fail (%s): %s", category, e)
        return False


# ---------- Обработка нового сообщения ----------------------------------------

async def send_to_admins(out_bot: Bot, conn, text: str) -> int:
    sent = 0
    for admin in st.list_admins(conn):
        try:
            for chunk in chunk_text(text, 4000):
                await out_bot.send_message(
                    admin["user_id"],
                    chunk,
                    disable_web_page_preview=False,
                )
            sent += 1
        except Exception as e:
            log.warning("admin digest copy fail (%s): %s", admin["user_id"], e)
    return sent


def chat_username(chat: Any) -> str | None:
    if isinstance(chat, (Channel, Chat)) and getattr(chat, "username", None):
        return chat.username.lower()
    return None


def is_weekend_msk() -> bool:
    now = datetime.now(MSK)
    return now.weekday() >= 5  # 5=сб, 6=вс


def message_day_msk(msg: Message) -> str:
    dt = getattr(msg, "date", None)
    if dt is None:
        return datetime.now(MSK).date().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).date().isoformat()


def daily_bucket(day: str, category: str) -> str:
    return f"daily:{day}:{category}"


def quarantine_bucket(day: str, category: str) -> str:
    return f"quarantine:{day}:{category}"


def filtered_ad_count(day_stats, category: str) -> int:
    return day_stats.get((category, "blocked_ad"), 0) + day_stats.get((category, "blocked_ai_ad"), 0)


def daily_digest_done_key(day: str, category: str, send_hour: int) -> str:
    return f"last_daily_digest:{day}:{category}:{send_hour}"


def daily_digest_fail_notice_key(day: str, category: str, send_hour: int) -> str:
    return f"daily_digest_fail_notice:{day}:{category}:{send_hour}"


async def handle_message(out_bot: Bot, client: TelegramClient, conn, event: events.NewMessage.Event) -> None:
    msg: Message = event.message
    chat = await event.get_chat()
    await handle_message_from_chat(out_bot, client, conn, msg, chat)


async def handle_message_from_chat(out_bot: Bot, client: TelegramClient, conn, msg: Message, chat: Any) -> None:
    username = chat_username(chat)
    if not username:
        return
    ch = st.get_channel(conn, username)
    if not ch:
        return
    if ch.paused:
        return

    if st.is_seen(conn, msg.chat_id, msg.id):
        return
    st.mark_seen(conn, msg.chat_id, msg.id)

    text = (msg.text or msg.message or "").strip()
    if not text and not msg.media:
        return

    day = message_day_msk(msg)

    if text and is_ad(text):
        log.info("[%s/%d] ad → skip", username, msg.id)
        st.bump_stat(conn, day, ch.category, "blocked_ad")
        st.bump_source_stat(conn, day, username, ch.category, "blocked_ad")
        st.enqueue_digest(conn, quarantine_bucket(day, ch.category), username, msg.chat_id, msg.id, "[AD_REGEX]\n" + text[:2000])
        return

    cleaned = clean_text(text) if text else ""
    if not cleaned:
        st.bump_stat(conn, day, ch.category, "skipped_empty")
        st.bump_source_stat(conn, day, username, ch.category, "skipped_empty")
        return

    classification = await ai_classify_post(cleaned)
    if classification == "AD":
        log.info("[%s/%d] ai-ad -> skip", username, msg.id)
        st.bump_stat(conn, day, ch.category, "blocked_ai_ad")
        st.bump_source_stat(conn, day, username, ch.category, "blocked_ai_ad")
        st.enqueue_digest(conn, quarantine_bucket(day, ch.category), username, msg.chat_id, msg.id, "[AD_AI]\n" + cleaned)
        return
    if classification == "NOISE":
        log.info("[%s/%d] ai-noise -> quarantine", username, msg.id)
        st.bump_stat(conn, day, ch.category, "blocked_noise")
        st.bump_source_stat(conn, day, username, ch.category, "blocked_noise")
        st.enqueue_digest(conn, quarantine_bucket(day, ch.category), username, msg.chat_id, msg.id, "[NOISE]\n" + cleaned)
        return

    bucket = daily_bucket(day, ch.category)
    st.enqueue_digest(conn, bucket, username, msg.chat_id, msg.id, cleaned)
    st.bump_stat(conn, day, ch.category, "queued_daily")
    st.bump_source_stat(conn, day, username, ch.category, "queued_daily")
    log.info("[%s/%d] -> daily queue %s", username, msg.id, bucket)
    return

    # Спец-режим: digest по расписанию (ozon_novosti)
    if ch.mode == "digest":
        st.enqueue_digest(conn, "ozon_novosti", username, msg.chat_id, msg.id, cleaned)
        log.info("[%s/%d] → ozon-digest queue", username, msg.id)
        return

    # Уикэнд → копим до понедельника
    if is_weekend_msk():
        st.enqueue_digest(conn, f"weekend:{ch.category}", username, msg.chat_id, msg.id, cleaned)
        st.bump_stat(conn, today, ch.category, "queued_weekend")
        log.info("[%s/%d] → weekend queue (%s)", username, msg.id, ch.category)
        return

    # Live-режим
    summary = await summarize(cleaned) if cleaned else None
    title = getattr(chat, "title", username)
    caption = format_msg(source=username, title=title, msg_id=msg.id, body=cleaned, summary=summary)

    ok = await send_to_target(out_bot, client, conn, ch.category, text=caption, media=msg.media)
    if ok:
        st.bump_stat(conn, today, ch.category, "sent")
        log.info("[%s/%d] → %s ✓", username, msg.id, ch.category)


# ---------- Воркеры дайджестов ------------------------------------------------

DAILY_CATEGORY_LABEL = {
    "mp_news": "МП",
    "marketing": "Маркетинг",
}


def _daily_rows_payload(rows: list[Any], max_chars: int) -> tuple[str, int]:
    parts: list[str] = []
    used = 0
    total = 0
    for i, r in enumerate(rows, 1):
        text = str(r["text"]).strip()
        if not text:
            continue
        source = str(r["source"]).strip().lstrip("@")
        entry = f"[{i}] @{source} https://t.me/{source}/{r['msg_id']}\n{text}\n"
        if total and total + len(entry) > max_chars:
            break
        if len(entry) > max_chars:
            entry = entry[:max_chars].rsplit(" ", 1)[0].strip()
        parts.append(entry)
        total += len(entry)
        used += 1
    return "\n---\n".join(parts), used


async def summarize_daily(category: str, day: str, rows: list[Any], filtered_ads: int) -> str | None:
    if not ai or not rows:
        return None

    daily_cfg = CFG.get("daily_digest", {})
    max_chars = int(daily_cfg.get("ai_max_chars", 16000))
    payload, used = _daily_rows_payload(rows, max_chars)
    if not payload:
        return None

    category_label = DAILY_CATEGORY_LABEL.get(category, category)
    try:
        display_day = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        display_day = day
    prompt = f"""
Сделай редакторский дайджест новостей для владельца/менеджера маркетплейсов.

Нужен формат как у живого новостного редактора: не таблица, не список всех постов,
а короткая лента самых важных событий с поясняющими подпунктами.

Оцени каждый пост:
3 — важно/срочно: правила, сроки, штрафы, блокировки, комиссии, поставки, реклама, деньги, новые инструменты, крупные изменения.
2 — полезно: применимый кейс, цифры, тарифы, наблюдение или лайфхак для селлера.
1 — слабый сигнал: мнение, частная история, обсуждение без нового факта.
0 — шум: повторы, самопиар, анонсы эфиров/курсов, опросы, эмоции, нерелевантное.

В итог включай только приоритет 2-3. Приоритет 0-1 не расписывай.
Если важных новостей мало — сделай короткий дайджест, не добивай объемом.

Строгий формат ответа:

📋 Дайджест новостей за {display_day}:

🖇Короткий заголовок новости (https://source/link)

-Что изменилось или произошло.

-Что важно знать селлеру: сроки, суммы, условия, ограничения, последствия.

-Что стоит сделать или проверить, если из новости следует действие.

🖇Следующая важная новость (https://source/link)

-...

Правила оформления:
- Без markdown, без жирного текста, без HTML.
- Один важный инфоповод = один блок.
- Заголовок начинай с 🖇, а особо денежные/практичные штуки можно начать с 👍.
- Ссылку ставь прямо в заголовок в скобках. Если ссылки нет в посте, используй ссылку Telegram-источника вида https://t.me/channel/123.
- Под каждым заголовком дай 1-4 подпункта через дефис, как в примере.
- Не пиши разделы "Главное", "Важно знать", "Гипотезы", "Не вошло".
- Не добавляй в дайджест рекламу, самопиар, опросы, анонсы вебинаров, мнения без фактов и мелкие истории без вывода.
- Не придумывай факты, цифры, сроки, причины и выводы. Если детали нет в постах — не добавляй ее.
- Сортируй новости по важности для селлера.
- Максимум 8-10 блоков, лучше меньше.

Категория: {category_label}
Дата сбора: {day}
Всего постов в очереди: {len(rows)}
Постов передано в анализ: {used}
Реклама/самопиар, отфильтрованные до анализа: {filtered_ads}

Посты:
{payload}
""".strip()

    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {"role": "system", "content": "Ты сильный редактор ежедневной сводки по маркетплейсам."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=int(daily_cfg.get("max_tokens", 1200)),
            temperature=0.25,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("OpenAI daily digest fail: %s", e)
        return None


def build_daily_digest_text(
    category: str,
    day: str,
    rows: list[Any],
    summary: str | None,
    filtered_ads: int,
) -> str:
    daily_cfg = CFG.get("daily_digest", {})
    max_links = int(daily_cfg.get("max_source_links", 25))
    try:
        display_day = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        display_day = day

    if summary:
        return html.escape(summary)
    else:
        parts = [
            f"📋 Дайджест новостей за {html.escape(display_day)}:",
        ]
        for i, r in enumerate(rows[:8], 1):
            source = str(r["source"]).strip().lstrip("@")
            link = f"https://t.me/{source}/{r['msg_id']}"
            snippet = str(r["text"]).replace("\n", " ").strip()
            if len(snippet) > 220:
                snippet = snippet[:220].rsplit(" ", 1)[0].strip() + "..."
            parts.append("")
            parts.append(f"🖇@{html.escape(source)} ({link})")
            parts.append(f"-{html.escape(snippet)}")
        parts.extend([
            "",
            f"Реклама/самопиар отфильтрованы до дайджеста: {filtered_ads}",
        ])
        source_lines = ["", "<b>🔗 Источники:</b>"]
        for r in rows[:max_links]:
            source = str(r["source"]).strip().lstrip("@")
            link = f"https://t.me/{source}/{r['msg_id']}"
            source_lines.append(f'— <a href="{link}">@{html.escape(source)}</a>')
        if len(rows) > max_links:
            source_lines.append(f"— еще {len(rows) - max_links} постов сохранены в архив")

    return "\n".join(parts + source_lines)


def _daily_pending_count(conn, day: str) -> int:
    return sum(st.queue_size(conn, daily_bucket(day, cat)) for cat in ("mp_news", "marketing"))


def resolve_runjob_day(conn, raw: str | None) -> str:
    today = datetime.now(MSK).date()
    if raw:
        value = raw.strip().lower()
        if value in ("today", "сегодня"):
            return today.isoformat()
        if value in ("yesterday", "вчера"):
            return (today - timedelta(days=1)).isoformat()
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError as exc:
            raise ValueError("Укажи дату как YYYY-MM-DD, today/сегодня или yesterday/вчера") from exc

    return today.isoformat()


async def run_daily_digest_once(
    out_bot: Bot,
    client: TelegramClient,
    conn,
    *,
    day: str | None = None,
    force: bool = False,
    mark_schedule: bool = False,
    consume: bool = True,
    send_target: bool = True,
    send_admins_copy: bool = True,
) -> str:
    daily_cfg = CFG.get("daily_digest", {})
    if not daily_cfg.get("enabled", True) and not force:
        return "⏸ Daily digest выключен в config.yaml."

    send_hour = int(daily_cfg.get("send_hour_msk", 9))
    if day is None:
        day = resolve_runjob_day(conn, None)

    setting_key = f"{day}:{send_hour}"
    if mark_schedule and not force and st.setting_get(conn, "last_daily_digest") == setting_key:
        return f"ℹ️ Сводка за {day} уже была отправлена."

    day_stats = st.stats_for_day(conn, day)
    any_pending = False
    all_sent = True
    mode = "отправка" if send_target else "предпросмотр"
    result_lines = [f"✅ Ручной запуск сводки за {day}: {mode}"]
    if not consume:
        result_lines.append("Очередь не очищается: утренний дайджест в 09:00 МСК соберет полный день.")
    if not send_target:
        result_lines.append("В целевой канал не отправляю: это preview для админов.")

    for cat in ("mp_news", "marketing"):
        bucket = daily_bucket(day, cat)
        rows = st.list_bucket(conn, bucket)
        label = DAILY_CATEGORY_LABEL.get(cat, cat)
        if not rows:
            result_lines.append(f"{label}: очередь пустая")
            continue

        any_pending = True
        filtered_ads = filtered_ad_count(day_stats, cat)
        summary = await summarize_daily(cat, day, rows, filtered_ads)
        if not summary and not daily_cfg.get("send_without_ai", False):
            all_sent = False
            result_lines.append(f"{label}: AI-сводка не сформировалась, очередь сохранена")
            log.warning("daily_digest %s %s skipped: AI summary unavailable", cat, day)
            continue
        text = build_daily_digest_text(cat, day, rows, summary, filtered_ads)

        target_sent = True
        if send_target:
            for chunk in chunk_text(text, 4000):
                if not await send_to_target(out_bot, client, conn, cat, text=chunk):
                    target_sent = False
                    break

        admin_copies = await send_to_admins(out_bot, conn, text) if send_admins_copy else 0

        if target_sent:
            if consume:
                st.delete_bucket(conn, bucket)
                st.bump_stat(conn, day, cat, "sent_daily_digest")
                result_lines.append(f"{label}: отправлено {len(rows)} постов, очередь очищена, копий админам {admin_copies}")
            elif not send_target:
                st.bump_stat(conn, day, cat, "preview_digest")
                result_lines.append(f"{label}: preview собран по {len(rows)} постам, очередь сохранена, копий админам {admin_copies}")
            else:
                st.bump_stat(conn, day, cat, "sent_manual_digest")
                result_lines.append(f"{label}: отправлено {len(rows)} постов, очередь сохранена, копий админам {admin_copies}")
            log.info("daily_digest %s %s: %d posts, consume=%s", cat, day, len(rows), consume)
        else:
            all_sent = False
            result_lines.append(f"{label}: не удалось отправить в целевой чат, очередь сохранена; копий админам {admin_copies}")

    if mark_schedule and (not any_pending or all_sent):
        st.setting_set(conn, "last_daily_digest", setting_key)

    if not any_pending:
        return f"ℹ️ Очередь за {day} пустая, отправлять нечего."

    return "\n".join(result_lines)


async def daily_digest_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    """Send yesterday's AI digest once a day after configured Moscow hour."""
    while True:
        await asyncio.sleep(60)
        try:
            daily_cfg = CFG.get("daily_digest", {})
            if not daily_cfg.get("enabled", True):
                continue

            now = datetime.now(MSK)
            send_hour = int(daily_cfg.get("send_hour_msk", 9))
            send_window_minutes = int(daily_cfg.get("send_window_minutes", 10))
            if now.hour != send_hour or now.minute >= send_window_minutes:
                continue

            day = (now.date() - timedelta(days=1)).isoformat()
            setting_key = f"{day}:{send_hour}"
            if st.setting_get(conn, "last_daily_digest") == setting_key:
                continue

            day_stats = st.stats_for_day(conn, day)
            any_pending = False
            all_sent = True

            for cat in ("mp_news", "marketing"):
                bucket = daily_bucket(day, cat)
                category_done_key = daily_digest_done_key(day, cat, send_hour)
                if st.setting_get(conn, category_done_key) == setting_key:
                    continue

                rows = st.list_bucket(conn, bucket)
                if not rows:
                    continue

                any_pending = True
                filtered_ads = filtered_ad_count(day_stats, cat)
                summary = await summarize_daily(cat, day, rows, filtered_ads)
                if not summary and not daily_cfg.get("send_without_ai", False):
                    all_sent = False
                    log.warning("daily_digest %s %s skipped: AI summary unavailable", cat, day)
                    continue
                text = build_daily_digest_text(cat, day, rows, summary, filtered_ads)

                sent = True
                for chunk in chunk_text(text, 4000):
                    if not await send_to_target(out_bot, client, conn, cat, text=chunk):
                        sent = False
                        break

                if sent:
                    admin_copies = await send_to_admins(out_bot, conn, text)
                    st.delete_bucket(conn, bucket)
                    st.bump_stat(conn, day, cat, "sent_daily_digest")
                    st.setting_set(conn, category_done_key, setting_key)
                    log.info("daily_digest %s %s: %d posts, admin copies: %d", cat, day, len(rows), admin_copies)
                else:
                    all_sent = False
                    fail_key = daily_digest_fail_notice_key(day, cat, send_hour)
                    if st.setting_get(conn, fail_key) != setting_key:
                        label = DAILY_CATEGORY_LABEL.get(cat, cat)
                        await send_to_admins(
                            out_bot,
                            conn,
                            (
                                f"⚠️ Утренний дайджест за {day} по категории {label} не отправился "
                                "в целевой чат/тему. Очередь сохранена, полный дайджест админам повторно "
                                "не рассылаю, чтобы не спамить. Проверь /here, права бота и /health."
                            ),
                        )
                        st.setting_set(conn, fail_key, setting_key)

            if not any_pending or all_sent:
                st.setting_set(conn, "last_daily_digest", setting_key)
        except Exception as e:
            log.exception("daily_digest fail: %s", e)


async def hourly_digest_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    """ozon_novosti — раз в час 09-21 МСК."""
    last_hour: int | None = None
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(MSK)
            if now.hour < 9 or now.hour > 21:
                continue
            key = f"{now.date()}-{now.hour}"
            if st.setting_get(conn, "last_ozon_digest") == key:
                continue
            rows = st.pop_bucket(conn, "ozon_novosti")
            st.setting_set(conn, "last_ozon_digest", key)
            if not rows:
                continue
            header = f"🔔 <b>Обновления базы знаний Ozon</b> · {now.strftime('%H:%M %d.%m')}"
            parts = [header]
            for r in rows:
                snippet = r["text"][:300] + ("…" if len(r["text"]) > 300 else "")
                parts.append(
                    f"• {snippet}\n"
                    f'  <a href="https://t.me/{r["source"]}/{r["msg_id"]}">→ открыть</a>'
                )
            for chunk in chunk_text("\n\n".join(parts), 4000):
                await send_to_target(out_bot, client, conn, "mp_news", text=chunk)
            log.info("ozon_digest sent: %d posts", len(rows))
        except Exception as e:
            log.exception("hourly_digest fail: %s", e)


async def weekend_digest_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    """Понедельник 09:00 МСК — выгружает накопленные за сб/вс посты по двум категориям."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(MSK)
            if now.weekday() != 0 or now.hour != 9:
                continue
            key = f"{now.date()}"
            if st.setting_get(conn, "last_weekend_digest") == key:
                continue
            st.setting_set(conn, "last_weekend_digest", key)
            for cat in ("mp_news", "marketing"):
                rows = st.pop_bucket(conn, f"weekend:{cat}")
                if not rows:
                    continue
                label = "🛒 MP-новости" if cat == "mp_news" else "📣 Маркетинг"
                header = f"📅 <b>Дайджест выходных · {label}</b> · {now.strftime('%d.%m')}"
                parts = [header, f"За сб/вс накопилось {len(rows)} постов:\n"]
                for r in rows:
                    snippet = r["text"][:280] + ("…" if len(r["text"]) > 280 else "")
                    parts.append(
                        f"• <b>@{r['source']}</b>: {snippet}\n"
                        f'  <a href="https://t.me/{r["source"]}/{r["msg_id"]}">→ оригинал</a>'
                    )
                for chunk in chunk_text("\n\n".join(parts), 4000):
                    await send_to_target(out_bot, client, conn, cat, text=chunk)
                log.info("weekend_digest %s: %d posts", cat, len(rows))
        except Exception as e:
            log.exception("weekend_digest fail: %s", e)


def _message_datetime_utc(msg: Message) -> datetime:
    dt = getattr(msg, "date", None)
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def catchup_scan_once(out_bot: Bot, client: TelegramClient, conn) -> tuple[int, int, int]:
    cfg = CFG.get("catchup", {})
    limit = int(cfg.get("limit_per_channel", 5))
    max_age_hours = float(cfg.get("max_age_hours", 36))
    timeout = float(cfg.get("entity_timeout_seconds", 15))
    delay = float(cfg.get("per_channel_delay_seconds", 0.4))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    scanned = 0
    handled = 0
    errors = 0

    for ch in st.list_channels(conn):
        if ch.paused:
            continue
        try:
            entity = await asyncio.wait_for(client.get_entity(ch.username), timeout=timeout)
            messages: list[Message] = []
            async for msg in client.iter_messages(entity, limit=limit):
                if _message_datetime_utc(msg) < cutoff:
                    continue
                if getattr(msg, "chat_id", None) is None:
                    continue
                if st.is_seen(conn, msg.chat_id, msg.id):
                    continue
                messages.append(msg)

            scanned += 1
            for msg in reversed(messages):
                await handle_message_from_chat(out_bot, client, conn, msg, entity)
                handled += 1

            if delay > 0:
                await asyncio.sleep(delay)
        except Exception as e:
            errors += 1
            log.warning("catchup scan failed for @%s: %s", ch.username, e)

    return scanned, handled, errors


async def catchup_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    cfg = CFG.get("catchup", {})
    if not cfg.get("enabled", True):
        log.info("Catchup scanner disabled in config.yaml")
        return

    interval = int(cfg.get("interval_seconds", 300))
    await asyncio.sleep(15)
    while True:
        try:
            scanned, handled, errors = await catchup_scan_once(out_bot, client, conn)
            st.setting_set(conn, "last_catchup_at", datetime.now(MSK).isoformat(timespec="seconds"))
            st.setting_set(conn, "last_catchup_result", f"channels={scanned}, new_messages={handled}, errors={errors}")
            log.info("catchup scan done: channels=%d, new_messages=%d, errors=%d", scanned, handled, errors)
        except Exception as e:
            log.exception("catchup scan loop fail: %s", e)
        await asyncio.sleep(interval)


def backup_db_once(conn) -> Path:
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"state-{datetime.now(MSK).date().isoformat()}.db"
    dest = sqlite3.connect(out)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return out


async def backup_worker(conn) -> None:
    last_key: str | None = None
    while True:
        await asyncio.sleep(300)
        try:
            now = datetime.now(MSK)
            key = now.date().isoformat()
            if now.hour != 3 or now.minute >= 10 or last_key == key:
                continue
            path = backup_db_once(conn)
            st.setting_set(conn, "last_backup_at", now.isoformat(timespec="seconds"))
            st.setting_set(conn, "last_backup_path", str(path))
            last_key = key
            log.info("DB backup saved: %s", path)
        except Exception as e:
            log.exception("DB backup fail: %s", e)


# ---------- Точка входа -------------------------------------------------------

async def resolve_channels(client: TelegramClient, conn) -> list[Any]:
    chats = []
    for ch in st.list_channels(conn):
        try:
            entity = await asyncio.wait_for(client.get_entity(ch.username), timeout=15)
            chats.append(entity)
        except asyncio.TimeoutError:
            log.warning("Channel @%s resolve timeout, skipped for this run", ch.username)
        except Exception as e:
            log.warning("Канал @%s недоступен: %s", ch.username, e)
    return chats


async def main() -> None:
    conn = st.connect(DB_PATH)

    # Засев из yaml при первом запуске
    if not st.list_channels(conn):
        n = st.seed_from_yaml(conn, CFG)
        log.info("Посеяно из yaml: %d каналов", n)
    saved_catchup_age = st.setting_get(conn, "catchup_max_age_hours")
    if saved_catchup_age:
        try:
            CFG.setdefault("catchup", {})["max_age_hours"] = float(saved_catchup_age)
        except ValueError:
            log.warning("Bad saved catchup_max_age_hours ignored: %s", saved_catchup_age)

    # Bot для отправки + админ-панели (один инстанс на оба)
    bot_token = os.environ["TG_BOT_TOKEN"]
    bot_kwargs = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
    bot_session = aiogram_session_from_env()
    if bot_session:
        bot_kwargs["session"] = bot_session
        log.info("Telegram Bot API proxy: %s", proxy_label("bot"))
    out_bot = Bot(token=bot_token, **bot_kwargs)
    bot_me = await out_bot.get_me()
    log.info("Бот для постинга: @%s", bot_me.username)

    log.info("Подключение Telethon: %s", TG_PHONE)
    client = TelegramClient(str(SESSION_PATH), TG_API_ID, TG_API_HASH, proxy=telethon_proxy_from_env())
    await client.start(phone=TG_PHONE)
    me = await client.get_me()
    log.info("Залогинены: %s (%s)", me.first_name, me.username or me.id)

    log.info("В базе %d каналов. Слушаем входящие апдейты и сверяемся с БД динамически.", len(st.list_channels(conn)))

    log.info("Dynamic channel mode enabled: /add and /addlist changes are picked up without restart")
    openai_ok, openai_message = await check_openai_status()
    st.setting_set(conn, "openai_status", "OK" if openai_ok else "FAIL")
    st.setting_set(conn, "openai_last_check_at", datetime.now(MSK).isoformat(timespec="seconds"))
    st.setting_set(conn, "openai_last_message", openai_message[:500])
    startup_lines = [
        "✅ Бот запущен.",
        f"Каналов: {len(st.list_channels(conn))}",
        f"OpenAI: {'OK' if openai_ok else 'FAIL'}",
    ]
    if not openai_ok:
        startup_lines.append(f"Причина: {openai_message[:800]}")
    await send_to_admins(out_bot, conn, "\n".join(startup_lines))

    @client.on(events.NewMessage())
    async def _on_new(event):
        try:
            await handle_message(out_bot, client, conn, event)
        except Exception as e:
            log.exception("handler fail: %s", e)

    # Конкурентные таски
    async def _runjob(raw_day: str | None = None) -> str:
        raw = (raw_day or "").strip()
        parts = raw.split()
        send_target = False
        day_arg: str | None = raw or None
        if parts and parts[0].lower() in ("send", "отправить"):
            send_target = True
            day_arg = " ".join(parts[1:]) or None
        elif parts and parts[0].lower() in ("preview", "превью"):
            send_target = False
            day_arg = " ".join(parts[1:]) or None

        day = resolve_runjob_day(conn, day_arg)
        return await run_daily_digest_once(
            out_bot,
            client,
            conn,
            day=day,
            force=True,
            mark_schedule=False,
            consume=False,
            send_target=send_target,
            send_admins_copy=True,
        )

    async def _health_check() -> str:
        today = datetime.now(MSK).date().isoformat()
        yesterday = (datetime.now(MSK).date() - timedelta(days=1)).isoformat()
        openai_ok_now, openai_msg_now = await check_openai_status()
        st.setting_set(conn, "openai_status", "OK" if openai_ok_now else "FAIL")
        st.setting_set(conn, "openai_last_check_at", datetime.now(MSK).isoformat(timespec="seconds"))
        st.setting_set(conn, "openai_last_message", openai_msg_now[:500])
        telethon_ok = await client.is_user_authorized()
        return "\n".join([
            "🩺 <b>Health</b>",
            f"Telethon: {'OK' if telethon_ok else 'FAIL'}",
            f"OpenAI: {'OK' if openai_ok_now else 'FAIL'}",
            f"OpenAI msg: {html.escape(openai_msg_now[:300])}",
            f"Каналов: {len(st.list_channels(conn))}",
            f"Очередь сегодня MP: {st.queue_size(conn, daily_bucket(today, 'mp_news'))}",
            f"Очередь сегодня маркетинг: {st.queue_size(conn, daily_bucket(today, 'marketing'))}",
            f"Карантин сегодня MP: {st.queue_size(conn, quarantine_bucket(today, 'mp_news'))}",
            f"Карантин сегодня маркетинг: {st.queue_size(conn, quarantine_bucket(today, 'marketing'))}",
            f"Очередь вчера MP: {st.queue_size(conn, daily_bucket(yesterday, 'mp_news'))}",
            f"Очередь вчера маркетинг: {st.queue_size(conn, daily_bucket(yesterday, 'marketing'))}",
            f"Last catchup: {html.escape(st.setting_get(conn, 'last_catchup_at', 'never') or 'never')}",
            f"Catchup result: {html.escape(st.setting_get(conn, 'last_catchup_result', '-') or '-')}",
            f"Last backup: {html.escape(st.setting_get(conn, 'last_backup_at', 'never') or 'never')}",
        ])

    async def _check_channels() -> str:
        ok: list[str] = []
        bad: list[str] = []
        for ch in st.list_channels(conn):
            try:
                await asyncio.wait_for(client.get_entity(ch.username), timeout=12)
                ok.append(ch.username)
            except Exception as e:
                bad.append(f"@{ch.username}: {type(e).__name__}")
        lines = [
            "🔎 <b>Проверка каналов</b>",
            f"OK: {len(ok)}",
            f"Нет доступа/ошибка: {len(bad)}",
        ]
        if bad:
            lines.append("\nПроблемные:")
            lines.extend(bad[:40])
            if len(bad) > 40:
                lines.append(f"...и еще {len(bad) - 40}")
        return "\n".join(lines)

    tasks = [
        asyncio.create_task(catchup_worker(out_bot, client, conn), name="catchup-scan"),
        asyncio.create_task(daily_digest_worker(out_bot, client, conn), name="daily-digest"),
        asyncio.create_task(backup_worker(conn), name="db-backup"),
        asyncio.create_task(admin_bot.run_admin_bot(
            out_bot,
            conn,
            run_job=_runjob,
            health_check=_health_check,
            check_channels=_check_channels,
        ), name="admin-bot"),
    ]

    log.info("Бот стартовал. Ждём апдейты…")

    # graceful shutdown
    stop = asyncio.Event()

    def _stop(*_):
        log.info("Получен сигнал остановки")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    telethon_task = asyncio.create_task(client.run_until_disconnected(), name="telethon")
    tasks.append(telethon_task)

    await stop.wait()
    log.info("Останавливаемся…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.disconnect()
    await out_bot.session.close()
    conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
