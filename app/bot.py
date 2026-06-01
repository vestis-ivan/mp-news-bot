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
import json
import logging
import os
import re
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
import httpx
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


async def ai_classify_post(text: str, category: str | None = None) -> str:
    ai_filter = CFG.get("filter", {}).get("ai_ad_filter", {})
    if not ai or not ai_filter.get("enabled", False):
        return "KEEP"

    sample = text.strip()
    if not sample:
        return "NOISE"

    max_chars = int(ai_filter.get("max_chars", 2500))
    sample = sample[:max_chars]

    category_rule = ""
    if category == "marketing":
        category_rule = (
            "For category MARKETING, KEEP only posts directly about marketplace marketing: "
            "ads, promotion tools, ad tariffs, campaign analytics, traffic, SEO/search ranking, "
            "content/creative, cards conversion, discounts/promos affecting demand, external traffic, "
            "brand communication, retention, marketplaces ad cabinets. "
            "Return NOISE for corporate finance, M&A, bank partnerships, investments, marketplace politics, "
            "generic WB/Ozon business news, logistics, warehouses, commissions, legal changes, or fintech news "
            "unless the post gives a direct marketing action for sellers. "
        )

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
                        f"{category_rule}"
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


async def send_text_to_target(
    out_bot: Bot,
    client: TelegramClient,
    conn,
    category: str,
    text: str,
) -> tuple[bool, int, int]:
    chunks = chunk_text(text, 3900)
    if not chunks:
        return True, 0, 0

    sent = 0
    for chunk in chunks:
        if not await send_to_target(out_bot, client, conn, category, text=chunk):
            return False, sent, len(chunks)
        sent += 1
    return True, sent, len(chunks)


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

    classification = await ai_classify_post(cleaned, ch.category)
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
    if category == "marketing":
        category_focus = """
Фокус категории "Маркетинг":
- Это НЕ общий дайджест маркетплейсов. Включай только то, что напрямую помогает продвигать товары и управлять спросом.
- Подходящие темы: реклама Ozon/WB, ставки и аукцион, продвижение, внешняя реклама, SEO/поиск, карточки и контент, CTR/CVR, отзывы как фактор конверсии, акции, промо, скидки, трафик, аналитика рекламных кампаний, креативы, инструменты маркетинга.
- Не включай: ВТБ покупает долю WB, ЦФА/инвестиции, банк/финтех WB, корпоративные сделки, логистика, склады, возвраты, комиссии, штрафы, законодательство, если там нет прямого маркетингового действия.
- Если в очереди нет сильных маркетинговых новостей, напиши коротко: "За период важных маркетинговых новостей не найдено." Не заменяй их общими новостями WB/Ozon.
""".strip()
    else:
        category_focus = """
Фокус категории "MP-новости":
- Включай важные новости Ozon/WB и других маркетплейсов: правила, комиссии, логистика, карточки, реклама, штрафы, кабинеты, сроки, инструменты и официальные изменения.
- Отдельные корпоративные/финансовые новости включай только если они могут повлиять на селлеров практически.
""".strip()
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

{category_focus}

Приоритет площадок:
- Ozon — главный фокус дайджеста. При прочих равных выбирай новости Ozon выше WB.
- Если за день есть достаточно важных новостей Ozon, дай им примерно 60-70% блоков.
- WB тоже включай, но только самые важные изменения: правила, тарифы, логистика, штрафы, карточки, реклама, официальные новости.
- Не выкидывай критически важный WB ради слабой новости Ozon. Важность факта важнее бренда, но при равной важности побеждает Ozon.
- Новости не про Ozon/WB включай только если они прямо важны селлерам маркетплейсов.

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
- Включай все важные новости приоритета 2-3, которые есть в очереди. Не ограничивайся одним сообщением: бот сам разобьет длинный дайджест на несколько сообщений.
- Если важных новостей много, максимум 20-25 блоков. Если важных мало, лучше коротко.

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


def strip_html_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_vc_initial_state(page: str) -> dict[str, Any]:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*({[\s\S]*?});\s*</script>", page)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception as e:
        log.warning("vc.ru initial state parse fail: %s", e)
        return {}


def vc_entry_text(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in entry.get("blocks") or []:
        data = block.get("data") or {}
        for key in ("text", "subline1", "subline2"):
            value = data.get(key)
            if isinstance(value, str):
                cleaned = strip_html_text(value)
                if cleaned:
                    parts.append(cleaned)
    return "\n".join(parts)


def vc_keyword_score(text: str) -> int:
    low = text.lower()
    ozon = any(x in low for x in ("ozon", "озон", "oзон"))
    wb = any(x in low for x in ("wildberries", "вайлдберриз", "wb ", " wb", "вб ", " вб"))
    if ozon and wb:
        return 3
    if ozon:
        return 2
    if wb:
        return 1
    return 0


async def fetch_vc_candidates(max_age_hours: float, max_items: int) -> list[dict[str, Any]]:
    cfg = CFG.get("vc_digest", {})
    pages = cfg.get("pages") or ["https://vc.ru/marketplace", "https://vc.ru/new", "https://vc.ru/popular"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    seen_ids: set[int] = set()
    out: list[dict[str, Any]] = []

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MPNewsBot/1.0; +https://vc.ru)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as http:
        for page_url in pages:
            try:
                resp = await http.get(page_url)
                resp.raise_for_status()
            except Exception as e:
                log.warning("vc.ru fetch fail %s: %s", page_url, e)
                continue

            state = extract_vc_initial_state(resp.text)
            for key, feed in state.items():
                if not key.startswith("feed@") or not isinstance(feed, dict):
                    continue
                for item in feed.get("items") or []:
                    if item.get("type") != "entry":
                        continue
                    data = item.get("data") or {}
                    item_id = int(data.get("id") or 0)
                    if not item_id or item_id in seen_ids:
                        continue
                    published = datetime.fromtimestamp(int(data.get("date") or 0), timezone.utc)
                    if published < cutoff:
                        continue
                    title = strip_html_text(str(data.get("title") or ""))
                    body = vc_entry_text(data)
                    score = vc_keyword_score(f"{title}\n{body}")
                    if not score:
                        continue
                    seen_ids.add(item_id)
                    url = data.get("url") or data.get("uri") or f"https://vc.ru/{item_id}"
                    if isinstance(url, str) and url.startswith("/"):
                        url = f"https://vc.ru{url}"
                    out.append({
                        "id": item_id,
                        "url": str(url),
                        "title": title,
                        "text": body,
                        "date": published,
                        "score": score,
                    })

    out.sort(key=lambda x: (x["score"], x["date"]), reverse=True)
    return out[:max_items]


def vc_payload(items: list[dict[str, Any]], max_chars: int = 18000) -> str:
    parts: list[str] = []
    total = 0
    for item in items:
        text = str(item["text"]).strip()
        if len(text) > 1800:
            text = text[:1800].rsplit(" ", 1)[0].strip() + "..."
        entry = (
            f"ID: {item['id']}\n"
            f"Дата: {item['date'].astimezone(MSK).strftime('%Y-%m-%d %H:%M')}\n"
            f"Ссылка: {item['url']}\n"
            f"Заголовок: {item['title']}\n"
            f"Текст: {text}"
        )
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)
    return "\n---\n".join(parts)


async def summarize_vc_digest(day: str, items: list[dict[str, Any]]) -> str | None:
    if not ai or not items:
        return None
    payload = vc_payload(items)
    if not payload:
        return None
    try:
        display_day = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        display_day = day

    prompt = f"""
Сделай отдельный дайджест по материалам vc.ru для селлера маркетплейсов.
Не пересказывай "о чём статья". Нужна суть: конкретные факты, последствия и что селлеру стоит проверить.

Главный фокус — Ozon. Wildberries можно добавить немного, только если новость реально важная.
Не включай пользовательские жалобы, бытовые кейсы покупателей, инвестиционные заметки, рекламу услуг,
общие рассуждения без фактов и материалы не для селлеров.

Если среди материалов нет важных новостей для селлера Ozon/WB, верни ровно: NO_IMPORTANT

Формат:

📋 VC.ru: важное про Ozon/WB за {display_day}

🖇Короткий заголовок (https://vc.ru/...)

- Тезис 1: главный факт или изменение из материала.

- Тезис 2: почему это важно для селлера Ozon/WB, какие риски или возможности появляются.

- Тезис 3: что проверить, сделать или какую гипотезу можно рассмотреть.

Правила:
- Ozon должен занимать примерно 70-80% дайджеста, если есть подходящие материалы.
- WB добавляй только после Ozon и только важное.
- Максимум 6-8 блоков.
- В каждом блоке должно быть ровно 3 содержательных тезиса. Не пиши общие фразы вроде "в статье рассказывается".
- Если в материале не хватает данных на 3 полезных тезиса для селлера, не включай этот материал.
- Не используй markdown и HTML.
- Не придумывай факты.

Материалы:
{payload}
""".strip()

    try:
        resp = await ai.chat.completions.create(
            model=CFG["ai"]["model"],
            messages=[
                {"role": "system", "content": "Ты редактор отдельной сводки по vc.ru для селлеров Ozon/WB."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=int(CFG.get("vc_digest", {}).get("max_tokens", 1600)),
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.upper().startswith("NO_IMPORTANT"):
            return None
        return text
    except Exception as e:
        log.warning("OpenAI vc digest fail: %s", e)
        return None


async def run_vc_digest_once(
    out_bot: Bot,
    client: TelegramClient,
    conn,
    *,
    force: bool = False,
    send_target: bool = True,
    send_admins_copy: bool = True,
) -> str:
    cfg = CFG.get("vc_digest", {})
    if not cfg.get("enabled", True) and not force:
        return "⏸ VC.ru digest выключен в config.yaml."

    now = datetime.now(MSK)
    day = now.date().isoformat()
    send_hour = int(cfg.get("send_hour_msk", 9))
    send_minute = int(cfg.get("send_minute_msk", 20))
    category = str(cfg.get("category", "mp_news"))
    if send_target and not st.get_target(conn, category):
        return f"❌ VC.ru-сводку не отправляю: нет целевого чата/темы для {category}. Задай /here {category}."

    setting_key = f"{day}:{send_hour}:{send_minute}"
    if not force and st.setting_get(conn, "last_vc_digest") == setting_key:
        return f"ℹ️ VC.ru-сводка за {day} уже была обработана."

    items = await fetch_vc_candidates(
        max_age_hours=float(cfg.get("max_age_hours", 72)),
        max_items=int(cfg.get("max_items", 30)),
    )
    if not items:
        if not force:
            st.setting_set(conn, "last_vc_digest", setting_key)
            st.setting_set(conn, "last_vc_digest_result", "no keyword candidates")
        return "ℹ️ VC.ru: за период не нашёл материалов про Ozon/WB."

    summary = await summarize_vc_digest(day, items)
    if not summary:
        if not force:
            st.setting_set(conn, "last_vc_digest", setting_key)
            st.setting_set(conn, "last_vc_digest_result", f"no important items, candidates={len(items)}")
        return f"ℹ️ VC.ru: кандидатов {len(items)}, но важных новостей для дайджеста AI не выбрал."

    sent = True
    chunks = 0
    total_chunks = 0
    if send_target:
        sent, chunks, total_chunks = await send_text_to_target(out_bot, client, conn, category, summary)
    admin_copies = await send_to_admins(out_bot, conn, summary) if send_admins_copy else 0

    if sent:
        if not force:
            st.setting_set(conn, "last_vc_digest", setting_key)
        st.setting_set(conn, "last_vc_digest_at", now.isoformat(timespec="seconds"))
        st.setting_set(conn, "last_vc_digest_result", f"sent, candidates={len(items)}, chunks={chunks}/{total_chunks}")
        return f"✅ VC.ru-сводка отправлена: кандидатов {len(items)}, частей {chunks}/{total_chunks}, копий админам {admin_copies}"

    st.setting_set(conn, "last_vc_digest_result", f"target send failed, candidates={len(items)}, chunks={chunks}/{total_chunks}")
    return f"❌ VC.ru-сводка не отправилась в целевой чат: частей {chunks}/{total_chunks}, очередь сайта не очищается не нужна."


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


def parse_runjob_args(conn, raw: str | None) -> tuple[bool, str, str | None]:
    tokens = (raw or "").strip().split()
    send_target = False
    day_arg: str | None = None
    category: str | None = None

    category_aliases: dict[str, str | None] = {
        "all": None,
        "все": None,
        "mp": "mp_news",
        "мп": "mp_news",
        "news": "mp_news",
        "новости": "mp_news",
        "mp_news": "mp_news",
        "marketing": "marketing",
        "market": "marketing",
        "mkt": "marketing",
        "маркетинг": "marketing",
    }

    for token in tokens:
        value = token.strip().lower()
        if value in ("send", "отправить"):
            send_target = True
            continue
        if value in ("preview", "превью"):
            send_target = False
            continue
        if value in category_aliases:
            category = category_aliases[value]
            continue
        if day_arg is None:
            day_arg = token
            continue
        raise ValueError(
            "Не понял аргументы. Примеры: /runjob, /runjob marketing, "
            "/runjob send marketing, /runjob yesterday mp_news, "
            "/runjob send marketing 2026-05-25."
        )

    return send_target, resolve_runjob_day(conn, day_arg), category


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
    category: str | None = None,
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
    categories = (category,) if category in ("mp_news", "marketing") else ("mp_news", "marketing")
    category_label = DAILY_CATEGORY_LABEL.get(category, "все категории") if category else "все категории"
    result_lines = [f"✅ Ручной запуск сводки за {day}: {mode}, {category_label}"]
    if not consume:
        result_lines.append("Очередь не очищается: утренний дайджест в 09:00 МСК соберет полный день.")
    if not send_target:
        result_lines.append("В целевой канал не отправляю: это preview для админов.")

    for cat in categories:
        bucket = daily_bucket(day, cat)
        rows = st.list_bucket(conn, bucket)
        label = DAILY_CATEGORY_LABEL.get(cat, cat)
        if not rows:
            result_lines.append(f"{label}: очередь пустая")
            continue

        if send_target and not st.get_target(conn, cat):
            any_pending = True
            all_sent = False
            result_lines.append(f"{label}: нет целевого чата/темы. Задай /here {cat}, AI-сводку не собираю.")
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
        target_chunks = 0
        target_total_chunks = 0
        if send_target:
            target_sent, target_chunks, target_total_chunks = await send_text_to_target(
                out_bot, client, conn, cat, text
            )

        admin_copies = await send_to_admins(out_bot, conn, text) if send_admins_copy else 0
        target_note = ""
        if send_target:
            target_note = f", частей в целевой чат {target_chunks}/{target_total_chunks}"

        if target_sent:
            if consume:
                st.delete_bucket(conn, bucket)
                st.bump_stat(conn, day, cat, "sent_daily_digest")
                result_lines.append(f"{label}: отправлено {len(rows)} постов, очередь очищена{target_note}, копий админам {admin_copies}")
            elif not send_target:
                st.bump_stat(conn, day, cat, "preview_digest")
                result_lines.append(f"{label}: preview собран по {len(rows)} постам, очередь сохранена, копий админам {admin_copies}")
            else:
                st.bump_stat(conn, day, cat, "sent_manual_digest")
                result_lines.append(f"{label}: отправлено {len(rows)} постов, очередь сохранена{target_note}, копий админам {admin_copies}")
            log.info("daily_digest %s %s: %d posts, consume=%s", cat, day, len(rows), consume)
        else:
            all_sent = False
            result_lines.append(f"{label}: не удалось отправить все части в целевой чат ({target_chunks}/{target_total_chunks}), очередь сохранена; копий админам {admin_copies}")

    if mark_schedule and (not any_pending or all_sent):
        st.setting_set(conn, "last_daily_digest", setting_key)

    if not any_pending:
        return f"ℹ️ Очередь за {day} пустая, отправлять нечего."

    return "\n".join(result_lines)


async def daily_digest_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    """Send yesterday's AI digest once a day after configured Moscow hour.

    The worker deliberately keeps trying after the morning hour. This prevents
    missed digests when the container restarts, Telegram/OpenAI hiccups, or
    catch-up fills the queue a few minutes after 09:00.
    """
    while True:
        await asyncio.sleep(60)
        try:
            daily_cfg = CFG.get("daily_digest", {})
            if not daily_cfg.get("enabled", True):
                continue

            now = datetime.now(MSK)
            send_hour = int(daily_cfg.get("send_hour_msk", 9))
            if now.hour < send_hour:
                continue

            day = (now.date() - timedelta(days=1)).isoformat()
            setting_key = f"{day}:{send_hour}"

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

                if not st.get_target(conn, cat):
                    all_sent = False
                    fail_key = daily_digest_fail_notice_key(day, cat, send_hour)
                    if st.setting_get(conn, fail_key) != setting_key:
                        label = DAILY_CATEGORY_LABEL.get(cat, cat)
                        await send_to_admins(
                            out_bot,
                            conn,
                            (
                                f"⚠️ Утренний дайджест за {day} по категории {label} не отправился: "
                                f"нет целевого чата/темы. Очередь сохранена, AI-сводку не собираю. "
                                f"Задай /here {cat} в нужной теме."
                            ),
                        )
                        st.setting_set(conn, fail_key, setting_key)
                    continue

                any_pending = True
                filtered_ads = filtered_ad_count(day_stats, cat)
                summary = await summarize_daily(cat, day, rows, filtered_ads)
                if not summary and not daily_cfg.get("send_without_ai", False):
                    all_sent = False
                    log.warning("daily_digest %s %s skipped: AI summary unavailable", cat, day)
                    continue
                text = build_daily_digest_text(cat, day, rows, summary, filtered_ads)

                sent, target_chunks, target_total_chunks = await send_text_to_target(
                    out_bot, client, conn, cat, text
                )

                if sent:
                    admin_copies = await send_to_admins(out_bot, conn, text)
                    st.delete_bucket(conn, bucket)
                    st.bump_stat(conn, day, cat, "sent_daily_digest")
                    st.setting_set(conn, category_done_key, setting_key)
                    log.info(
                        "daily_digest %s %s: %d posts, target chunks: %d/%d, admin copies: %d",
                        cat, day, len(rows), target_chunks, target_total_chunks, admin_copies,
                    )
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

            if any_pending and all_sent:
                st.setting_set(conn, "last_daily_digest", setting_key)
        except Exception as e:
            log.exception("daily_digest fail: %s", e)


async def vc_digest_worker(out_bot: Bot, client: TelegramClient, conn) -> None:
    """Send a separate vc.ru digest to the MP target after the Telegram digest."""
    while True:
        await asyncio.sleep(300)
        try:
            cfg = CFG.get("vc_digest", {})
            if not cfg.get("enabled", True):
                continue
            now = datetime.now(MSK)
            send_hour = int(cfg.get("send_hour_msk", 9))
            send_minute = int(cfg.get("send_minute_msk", 20))
            if (now.hour, now.minute) < (send_hour, send_minute):
                continue
            day = now.date().isoformat()
            setting_key = f"{day}:{send_hour}:{send_minute}"
            if st.setting_get(conn, "last_vc_digest") == setting_key:
                continue
            result = await run_vc_digest_once(out_bot, client, conn, force=False)
            log.info("vc_digest: %s", result)
        except Exception as e:
            log.exception("vc_digest fail: %s", e)


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
        send_target, day, category = parse_runjob_args(conn, raw_day)
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
            category=category,
        )

    async def _run_vc_job(raw_args: str | None = None) -> str:
        raw = (raw_args or "").strip().lower()
        send_target = raw in ("send", "отправить")
        return await run_vc_digest_once(
            out_bot,
            client,
            conn,
            force=True,
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
            f"Last VC digest: {html.escape(st.setting_get(conn, 'last_vc_digest_at', 'never') or 'never')}",
            f"VC result: {html.escape(st.setting_get(conn, 'last_vc_digest_result', '-') or '-')}",
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
        asyncio.create_task(vc_digest_worker(out_bot, client, conn), name="vc-digest"),
        asyncio.create_task(backup_worker(conn), name="db-backup"),
        asyncio.create_task(admin_bot.run_admin_bot(
            out_bot,
            conn,
            run_job=_runjob,
            run_vc_job=_run_vc_job,
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
