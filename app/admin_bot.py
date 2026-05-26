"""Админ-бот на aiogram.

Команды:
  /start  — приветствие
  /admin  — стать админом (доступ открыт по упоминанию команды)
  /here mp_news    — назначить ЭТОТ чат/тему как место для MP-новостей
  /here marketing  — то же для маркетинга
  /add @username mp_news|marketing
  /remove @username
  /move @username mp_news|marketing
  /pause @username
  /resume @username
  /list  — список всех каналов с пагинацией
  /stats — статистика за сегодня
  /menu  — главное инлайн-меню

Любые команды управления требуют, чтобы юзер был в таблице admins.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import state as st


log = logging.getLogger("admin-bot")

CATEGORY_LABEL = {
    "mp_news": "🛒 MP-новости",
    "marketing": "📣 Маркетинг",
}


def admin_allowlist() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("Bad ADMIN_USER_IDS item ignored: %s", part)
    return out


def can_claim_admin(conn, user_id: int) -> bool:
    allowed = admin_allowlist()
    return user_id in allowed


def approver_username() -> str:
    return os.getenv("ADMIN_APPROVER_USERNAME", "def325").strip().lstrip("@").lower()


def approver_user_ids(conn) -> set[int]:
    out: set[int] = set()
    raw = os.getenv("ADMIN_APPROVER_USER_ID", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("Bad ADMIN_APPROVER_USER_ID item ignored: %s", part)

    owner = approver_username()
    for admin in st.list_admins(conn):
        username = (admin["username"] or "").strip().lstrip("@").lower()
        if username == owner:
            out.add(int(admin["user_id"]))
    return out


def approver_targets(conn) -> list[int | str]:
    ids = sorted(approver_user_ids(conn))
    if ids:
        return ids
    return [f"@{approver_username()}"]


def is_approver_user(conn, user: Any) -> bool:
    if not user:
        return False
    if user.id in approver_user_ids(conn):
        return True
    username = (user.username or "").strip().lstrip("@").lower()
    return bool(username and username == approver_username())


def admin_request_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_ok:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_no:{user_id}"),
    ]])


def user_tg_text(user: Any) -> str:
    username = f"@{html.escape(user.username)}" if getattr(user, "username", None) else "no username"
    name = html.escape(getattr(user, "full_name", None) or getattr(user, "first_name", "") or "")
    return (
        f"<a href=\"tg://user?id={user.id}\">{name or user.id}</a>\n"
        f"Username: {username}\n"
        f"ID: <code>{user.id}</code>"
    )


async def send_admin_approval_request(bot: Bot, conn, msg: Message) -> None:
    user = msg.from_user
    if not user:
        return

    text = (
        "<b>Заявка на админку</b>\n\n"
        f"{user_tg_text(user)}\n\n"
        "Подтвердить этого пользователя?"
    )
    st.setting_set(
        conn,
        f"admin_request:{user.id}",
        json.dumps({"username": user.username, "first_name": user.first_name}, ensure_ascii=False),
    )
    sent = 0
    last_error: Exception | None = None
    for target in approver_targets(conn):
        try:
            await bot.send_message(target, text, reply_markup=admin_request_kb(user.id))
            sent += 1
        except Exception as e:
            last_error = e
            log.warning("admin approval request send fail (%s): %s", target, e)

    if sent:
        await msg.answer("✅ Заявка на админку отправлена на подтверждение.")
        return

    await msg.answer(
        "⚠️ Заявку не удалось отправить @def325.\n\n"
        "Пусть @def325 сначала напишет этому боту /admin, "
        "или добавь ADMIN_APPROVER_USER_ID в .env и перезапусти бота."
    )
    if last_error:
        log.warning("admin approval request was not delivered: %s", last_error)


def parse_username(raw: str) -> str | None:
    """Принимает '@foo', 'foo', 't.me/foo', 'https://t.me/foo'. Возвращает 'foo' lowercase."""
    raw = raw.strip()
    m = re.match(r"(?:https?://)?(?:t\.me/)?@?([A-Za-z0-9_]{4,32})$", raw)
    if not m:
        return None
    return m.group(1).lower()


def parse_usernames_block(raw: str) -> tuple[list[str], list[str]]:
    usernames: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()

    for line in re.split(r"[\s,]+", raw):
        item = line.strip()
        if not item:
            continue
        username = parse_username(item)
        if not username:
            bad.append(item)
            continue
        if username in seen:
            continue
        seen.add(username)
        usernames.append(username)

    return usernames, bad


# ---------- middleware: проверка прав --------------------------------------------

def admin_only(conn) -> Callable[[Message, dict], Awaitable[bool]]:
    async def check(msg: Message) -> bool:
        if msg.from_user and st.is_admin(conn, msg.from_user.id):
            return True
        await msg.answer("⛔ Эта команда только для админов. Напиши /admin чтобы стать админом.")
        return False
    return check


# ---------- главное меню ---------------------------------------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все каналы", callback_data="list:mp_news:0"),
         InlineKeyboardButton(text="📣 Маркетинг", callback_data="list:marketing:0")],
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add:start")],
        [InlineKeyboardButton(text="📊 Стата за сегодня", callback_data="stats:today"),
         InlineKeyboardButton(text="🎯 Куда слать", callback_data="targets:show")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help:show")],
    ])


def channel_actions_kb(username: str, paused: bool, category: str) -> InlineKeyboardMarkup:
    other = "marketing" if category == "mp_news" else "mp_news"
    other_label = "→ Маркетинг" if other == "marketing" else "→ MP"
    pause_btn = (
        InlineKeyboardButton(text="▶️ Включить", callback_data=f"resume:{username}")
        if paused else
        InlineKeyboardButton(text="⏸ На паузу", callback_data=f"pause:{username}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [pause_btn, InlineKeyboardButton(text=other_label, callback_data=f"move:{username}:{other}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rm:{username}")],
        [InlineKeyboardButton(text="← К списку", callback_data=f"list:{category}:0")],
    ])


PAGE = 10


def channels_list_kb(category: str, page: int, channels: list[st.Channel]) -> InlineKeyboardMarkup:
    start, end = page * PAGE, (page + 1) * PAGE
    chunk = channels[start:end]
    rows = []
    for ch in chunk:
        prefix = "⏸ " if ch.paused else ""
        rows.append([InlineKeyboardButton(
            text=f"{prefix}@{ch.username}",
            callback_data=f"ch:{ch.username}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"list:{category}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(len(channels) + PAGE - 1) // PAGE or 1}", callback_data="noop"))
    if end < len(channels):
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"list:{category}:{page+1}"))
    if nav:
        rows.append(nav)
    other = "marketing" if category == "mp_news" else "mp_news"
    rows.append([
        InlineKeyboardButton(text=f"Показать {CATEGORY_LABEL[other]}", callback_data=f"list:{other}:0"),
        InlineKeyboardButton(text="← В меню", callback_data="menu:show"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- FSM для добавления через кнопки --------------------------------------

class AddFlow(StatesGroup):
    pick_category = State()
    enter_username = State()


# ---------- роутер --------------------------------------------------------------

def build_router(conn, bot: Bot, run_job: Callable[[str | None], Awaitable[str]] | None = None) -> Router:
    r = Router(name="admin")
    check = admin_only(conn)

    async def send_channel_list(msg: Message, category: str, prefix: str = "") -> None:
        channels = st.list_channels(conn, category)
        if not channels:
            text = f"{prefix}\n\n📭 В {CATEGORY_LABEL[category]} пока пусто.".strip()
            await msg.answer(text)
            return

        text = f"{prefix}\n\n📋 <b>{CATEGORY_LABEL[category]}</b> ({len(channels)})".strip()
        await msg.answer(text, reply_markup=channels_list_kb(category, 0, channels))

    @r.message(CommandStart())
    async def cmd_start(msg: Message):
        is_a = bool(msg.from_user and st.is_admin(conn, msg.from_user.id))
        text = (
            "👋 Привет! Я — админка <b>MP News Bot</b>.\n\n"
            "Я управляю парсером новостей из ТГ-каналов про маркетплейсы и маркетинг.\n\n"
        )
        if is_a:
            text += "Ты в списке админов. Открой /menu или используй команды (/list, /add, /stats…)."
        else:
            text += "Напиши <b>/admin</b> чтобы получить доступ к управлению."
        await msg.answer(text)

    @r.message(Command("id"))
    async def cmd_id(msg: Message):
        if not msg.from_user:
            return
        await msg.answer(f"Твой Telegram ID: <code>{msg.from_user.id}</code>")

    @r.message(Command("admin", "admin11i"))
    async def cmd_admin(msg: Message):
        u = msg.from_user
        if not u:
            return
        if st.is_admin(conn, u.id):
            await msg.answer("✅ Ты уже админ.", reply_markup=main_menu_kb())
            return

        if not (can_claim_admin(conn, u.id) or is_approver_user(conn, u)):
            await send_admin_approval_request(bot, conn, msg)
            log.info("Admin approval requested: %s (%s)", u.username or u.id, u.first_name)
            return

        st.add_admin(conn, u.id, u.username, u.first_name)
        log.info("New admin: %s (%s)", u.username or u.id, u.first_name)
        await msg.answer(
            f"✅ <b>Готово!</b> {html.escape(u.first_name or '')}, ты теперь админ.\n\n"
            "Открой /menu или попробуй команды:\n"
            "• /list — все каналы\n"
            "• /add @username mp_news — добавить канал\n"
            "• /here mp_news — назначить этот чат как приёмник\n"
            "• /stats — статистика за сегодня",
            reply_markup=main_menu_kb(),
        )

    @r.message(Command("menu"))
    async def cmd_menu(msg: Message, state: FSMContext):
        await state.clear()
        if not await check(msg):
            return
        await msg.answer("📋 <b>Главное меню</b>", reply_markup=main_menu_kb())

    @r.message(Command("cancel"))
    async def cmd_cancel(msg: Message, state: FSMContext):
        await state.clear()
        if not await check(msg):
            return
        await msg.answer("✅ Отменил текущий ввод.", reply_markup=main_menu_kb())

    # /here mp_news|marketing — назначает приёмник
    @r.message(Command("here"))
    async def cmd_here(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        arg = (command.args or "").strip()
        if arg not in ("mp_news", "marketing"):
            await msg.answer(
                "Использование: <code>/here mp_news</code> или <code>/here marketing</code>"
            )
            return
        thread_id = msg.message_thread_id  # для топиков в супергруппе
        st.set_target(conn, arg, msg.chat.id, thread_id, msg.from_user.id)
        where = f"чат <b>{msg.chat.title or msg.chat.id}</b>"
        if thread_id:
            where += f" / тема #{thread_id}"
        await msg.answer(
            f"✅ Приёмник для <b>{CATEGORY_LABEL[arg]}</b> назначен сюда: {where}.\n\n"
            "Все новые посты этой категории будут лететь в этот чат/тему."
        )

    @r.message(Command("add"))
    async def cmd_add(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        parts = (command.args or "").split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/add @username mp_news</code>\n"
                "или: <code>/add @username marketing</code>"
            )
            return
        username = parse_username(parts[0])
        category = parts[1].lower()
        if not username:
            await msg.answer("❌ Не понял username канала.")
            return
        if category not in ("mp_news", "marketing"):
            await msg.answer("❌ Категория должна быть <code>mp_news</code> или <code>marketing</code>.")
            return
        ok = st.add_channel(conn, username, category, added_by=msg.from_user.id)
        if ok:
            await send_channel_list(
                msg,
                category,
                f"✅ Добавлен <b>@{username}</b> в {CATEGORY_LABEL[category]}.\n\n"
                "✅ Если Telegram-аккаунт бота подписан на канал, новые посты начнут попадать в очередь без рестарта.",
            )
        else:
            await send_channel_list(msg, category, f"⚠️ <b>@{username}</b> уже в списке.")

    @r.message(Command("addlist"))
    async def cmd_addlist(msg: Message, command: CommandObject):
        if not await check(msg):
            return

        raw = command.args or ""
        parts = raw.split(maxsplit=1)
        if not parts or parts[0].lower() not in ("mp_news", "marketing"):
            await msg.answer(
                "Использование:\n"
                "<code>/addlist mp_news</code>\n"
                "<code>@channel_one</code>\n"
                "<code>https://t.me/channel_two</code>\n\n"
                "Категории: <code>mp_news</code> или <code>marketing</code>."
            )
            return

        category = parts[0].lower()
        body = parts[1] if len(parts) > 1 else ""
        usernames, bad = parse_usernames_block(body)
        if not usernames:
            await msg.answer("❌ Не нашел каналов в списке. Кидай каждый канал отдельной строкой после категории.")
            return

        added: list[str] = []
        existed: list[str] = []
        for username in usernames:
            ok = st.add_channel(conn, username, category, added_by=msg.from_user.id)
            if ok:
                added.append(username)
            else:
                existed.append(username)

        lines = [
            f"✅ Пакетное добавление в {CATEGORY_LABEL[category]}",
            f"Добавлено: {len(added)}",
            f"Уже были в базе: {len(existed)}",
        ]
        if bad:
            lines.append(f"Не распознал строк: {len(bad)}")
        if added:
            lines.append("\nНовые:\n" + "\n".join(f"@{u}" for u in added[:30]))
            if len(added) > 30:
                lines.append(f"...и еще {len(added) - 30}")
        if existed:
            lines.append("\nУже были:\n" + "\n".join(f"@{u}" for u in existed[:20]))
            if len(existed) > 20:
                lines.append(f"...и еще {len(existed) - 20}")
        if bad:
            lines.append("\nНе понял:\n" + "\n".join(f"<code>{b}</code>" for b in bad[:10]))
            if len(bad) > 10:
                lines.append(f"...и еще {len(bad) - 10}")
        lines.append("\n✅ Если Telegram-аккаунт бота подписан на эти каналы, новые посты начнут попадать в очередь без рестарта.")

        await send_channel_list(msg, category, "\n".join(lines))

    @r.message(Command("remove"))
    async def cmd_remove(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        username = parse_username((command.args or "").strip())
        if not username:
            await msg.answer("Использование: <code>/remove @username</code>")
            return
        ch = st.get_channel(conn, username)
        if ch and st.remove_channel(conn, username):
            await send_channel_list(msg, ch.category, f"🗑 <b>@{username}</b> удалён.")
        else:
            await msg.answer(f"❌ <b>@{username}</b> не найден.")

    @r.message(Command("move"))
    async def cmd_move(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        parts = (command.args or "").split()
        if len(parts) < 2:
            await msg.answer("Использование: <code>/move @username mp_news</code>")
            return
        username = parse_username(parts[0])
        category = parts[1].lower()
        if not username or category not in ("mp_news", "marketing"):
            await msg.answer("❌ Не понял аргументы.")
            return
        if st.set_channel_category(conn, username, category):
            await msg.answer(f"✅ <b>@{username}</b> → {CATEGORY_LABEL[category]}.")
        else:
            await msg.answer(f"❌ <b>@{username}</b> не найден.")

    @r.message(Command("pause"))
    async def cmd_pause(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        username = parse_username((command.args or "").strip())
        if not username:
            await msg.answer("Использование: <code>/pause @username</code>")
            return
        if st.set_channel_paused(conn, username, True):
            await msg.answer(f"⏸ <b>@{username}</b> на паузе.")
        else:
            await msg.answer(f"❌ <b>@{username}</b> не найден.")

    @r.message(Command("resume"))
    async def cmd_resume(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        username = parse_username((command.args or "").strip())
        if not username:
            await msg.answer("Использование: <code>/resume @username</code>")
            return
        if st.set_channel_paused(conn, username, False):
            await msg.answer(f"▶️ <b>@{username}</b> снова в строю.")
        else:
            await msg.answer(f"❌ <b>@{username}</b> не найден.")

    @r.message(Command("list"))
    async def cmd_list(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        cat = (command.args or "mp_news").strip().lower()
        if cat not in ("mp_news", "marketing"):
            cat = "mp_news"
        channels = st.list_channels(conn, cat)
        if not channels:
            await msg.answer(f"📭 В {CATEGORY_LABEL[cat]} пока пусто.")
            return
        await msg.answer(
            f"📋 <b>{CATEGORY_LABEL[cat]}</b> ({len(channels)})",
            reply_markup=channels_list_kb(cat, 0, channels),
        )

    @r.message(Command("stats"))
    async def cmd_stats(msg: Message):
        if not await check(msg):
            return
        await msg.answer(_stats_text(conn), reply_markup=main_menu_kb())

    @r.message(Command("runjob"))
    async def cmd_runjob(msg: Message, command: CommandObject):
        if not await check(msg):
            return
        if run_job is None:
            await msg.answer("❌ Ручной запуск не подключен в этом процессе.")
            return

        raw_day = (command.args or "").strip() or None
        notice = await msg.answer("⏳ Собираю и отправляю дайджест из уже накопленной очереди...")
        try:
            result = await run_job(raw_day)
        except ValueError as e:
            await notice.edit_text(f"❌ {e}")
        except Exception as e:
            log.exception("runjob fail: %s", e)
            await notice.edit_text(f"❌ Не получилось запустить сводку: {e}")
        else:
            await notice.edit_text(result)

    @r.callback_query(F.data.startswith("adm_ok:"))
    async def cb_admin_approve(c: CallbackQuery):
        if not is_approver_user(conn, c.from_user):
            await c.answer("Only approver can do this", show_alert=True)
            return
        try:
            user_id = int(c.data.split(":", 1)[1])
        except Exception:
            await c.answer("Bad request", show_alert=True)
            return

        username = None
        first_name = None
        raw_request = st.setting_get(conn, f"admin_request:{user_id}")
        if raw_request:
            try:
                request_data = json.loads(raw_request)
                username = request_data.get("username")
                first_name = request_data.get("first_name")
            except Exception:
                pass

        st.add_admin(conn, user_id, username, first_name)
        if c.message:
            await c.message.edit_reply_markup(reply_markup=None)
            await c.message.answer(f"✅ Админ подтверждён: <code>{user_id}</code>")
        try:
            await bot.send_message(
                user_id,
                "✅ Тебе подтвердили доступ к админке. Открой /menu.",
                reply_markup=main_menu_kb(),
            )
        except Exception as e:
            log.warning("approved admin notify fail (%s): %s", user_id, e)
        await c.answer("Approved")

    @r.callback_query(F.data.startswith("adm_no:"))
    async def cb_admin_decline(c: CallbackQuery):
        if not is_approver_user(conn, c.from_user):
            await c.answer("Only approver can do this", show_alert=True)
            return
        try:
            user_id = int(c.data.split(":", 1)[1])
        except Exception:
            await c.answer("Bad request", show_alert=True)
            return

        if c.message:
            await c.message.edit_reply_markup(reply_markup=None)
            await c.message.answer(f"❌ Админка отклонена: <code>{user_id}</code>")
        try:
            await bot.send_message(user_id, "❌ Доступ к админке не подтверждён.")
        except Exception as e:
            log.warning("declined admin notify fail (%s): %s", user_id, e)
        await c.answer("Declined")

    # ----- callbacks -----

    @r.callback_query(F.data == "menu:show")
    async def cb_menu(c: CallbackQuery, state: FSMContext):
        await state.clear()
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔ Только админам", show_alert=True)
            return
        await c.message.edit_text("📋 <b>Главное меню</b>", reply_markup=main_menu_kb())
        await c.answer()

    @r.callback_query(F.data.startswith("list:"))
    async def cb_list(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        _, cat, page = c.data.split(":")
        page = int(page)
        channels = st.list_channels(conn, cat)
        await c.message.edit_text(
            f"📋 <b>{CATEGORY_LABEL[cat]}</b> ({len(channels)})",
            reply_markup=channels_list_kb(cat, page, channels),
        )
        await c.answer()

    @r.callback_query(F.data.startswith("ch:"))
    async def cb_channel(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        username = c.data.split(":", 1)[1]
        ch = st.get_channel(conn, username)
        if not ch:
            await c.answer("Не найден", show_alert=True)
            return
        status = "⏸ на паузе" if ch.paused else "▶️ активен"
        mode = "🕘 дайджест" if ch.mode == "digest" else "⚡ live"
        text = (
            f"<b>@{ch.username}</b>\n\n"
            f"Категория: {CATEGORY_LABEL[ch.category]}\n"
            f"Статус: {status}\n"
            f"Режим: {mode}\n"
            f"Ссылка: https://t.me/{ch.username}"
        )
        await c.message.edit_text(text, reply_markup=channel_actions_kb(ch.username, ch.paused, ch.category))
        await c.answer()

    @r.callback_query(F.data.startswith("pause:"))
    async def cb_pause(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        username = c.data.split(":", 1)[1]
        st.set_channel_paused(conn, username, True)
        await c.answer(f"⏸ @{username} на паузе")
        await _refresh_channel_view(c, conn, username)

    @r.callback_query(F.data.startswith("resume:"))
    async def cb_resume(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        username = c.data.split(":", 1)[1]
        st.set_channel_paused(conn, username, False)
        await c.answer(f"▶️ @{username} включён")
        await _refresh_channel_view(c, conn, username)

    @r.callback_query(F.data.startswith("move:"))
    async def cb_move(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        _, username, new_cat = c.data.split(":")
        st.set_channel_category(conn, username, new_cat)
        await c.answer(f"✅ → {CATEGORY_LABEL[new_cat]}")
        await _refresh_channel_view(c, conn, username)

    @r.callback_query(F.data.startswith("rm:"))
    async def cb_rm(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        username = c.data.split(":", 1)[1]
        ch = st.get_channel(conn, username)
        category = ch.category if ch else "mp_news"
        st.remove_channel(conn, username)
        await c.answer(f"🗑 @{username} удалён", show_alert=True)
        channels = st.list_channels(conn, category)
        await c.message.edit_text(
            f"📋 <b>{CATEGORY_LABEL[category]}</b> ({len(channels)})",
            reply_markup=channels_list_kb(category, 0, channels),
        )

    @r.callback_query(F.data == "add:start")
    async def cb_add_start(c: CallbackQuery, state: FSMContext):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        await c.message.edit_text(
            "➕ <b>Новый канал</b>\n\nВ какую категорию?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=CATEGORY_LABEL["mp_news"], callback_data="add:cat:mp_news"),
                 InlineKeyboardButton(text=CATEGORY_LABEL["marketing"], callback_data="add:cat:marketing")],
                [InlineKeyboardButton(text="← Отмена", callback_data="menu:show")],
            ]),
        )
        await c.answer()

    @r.callback_query(F.data.startswith("add:cat:"))
    async def cb_add_cat(c: CallbackQuery, state: FSMContext):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        cat = c.data.split(":")[2]
        await state.set_state(AddFlow.enter_username)
        await state.update_data(category=cat)
        await c.message.edit_text(
            f"Категория: <b>{CATEGORY_LABEL[cat]}</b>\n\n"
            "Теперь пришли @username канала (можно ссылку <code>t.me/...</code>).\n"
            "Для отмены — /menu",
        )
        await c.answer()

    @r.message(AddFlow.enter_username)
    async def fsm_enter_username(msg: Message, state: FSMContext):
        if not st.is_admin(conn, msg.from_user.id):
            await state.clear()
            return
        if (msg.text or "").strip().startswith("/"):
            await state.clear()
            command = (msg.text or "").strip().split()[0].split("@")[0].lower()
            if command == "/menu":
                await msg.answer("📋 <b>Главное меню</b>", reply_markup=main_menu_kb())
            elif command == "/cancel":
                await msg.answer("✅ Отменил текущий ввод.", reply_markup=main_menu_kb())
            elif command in ("/admin", "/admin11i"):
                await cmd_admin(msg)
            elif command == "/start":
                await cmd_start(msg)
            else:
                await msg.answer("Ок, режим добавления канала сброшен. Отправь команду ещё раз.")
            return
        username = parse_username(msg.text or "")
        if not username:
            await msg.answer("❌ Не похоже на username. Попробуй ещё раз или /menu для отмены.")
            return
        data = await state.get_data()
        cat = data["category"]
        ok = st.add_channel(conn, username, cat, added_by=msg.from_user.id)
        await state.clear()
        if ok:
            await send_channel_list(
                msg,
                cat,
                f"✅ <b>@{username}</b> добавлен в {CATEGORY_LABEL[cat]}.\n\n"
                "✅ Если Telegram-аккаунт бота подписан на канал, новые посты начнут попадать в очередь без рестарта.",
            )
        else:
            await send_channel_list(msg, cat, f"⚠️ <b>@{username}</b> уже в списке.")

    @r.callback_query(F.data == "stats:today")
    async def cb_stats(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        await c.message.edit_text(_stats_text(conn), reply_markup=main_menu_kb())
        await c.answer()

    @r.callback_query(F.data == "targets:show")
    async def cb_targets(c: CallbackQuery):
        if not st.is_admin(conn, c.from_user.id):
            await c.answer("⛔", show_alert=True)
            return
        lines = ["🎯 <b>Куда летят новости</b>\n"]
        for cat in ("mp_news", "marketing"):
            t = st.get_target(conn, cat)
            if t:
                cid, tid = t
                lines.append(f"{CATEGORY_LABEL[cat]}: chat={cid}" + (f", thread={tid}" if tid else ""))
            else:
                lines.append(f"{CATEGORY_LABEL[cat]}: ❌ не задан (используй /here)")
        lines.append(
            "\nЧтобы задать приёмник — зайди в нужный чат/тему и напиши там:\n"
            "<code>/here mp_news</code> или <code>/here marketing</code>"
        )
        await c.message.edit_text("\n".join(lines), reply_markup=main_menu_kb())
        await c.answer()

    @r.callback_query(F.data == "help:show")
    async def cb_help(c: CallbackQuery):
        text = (
            "📖 <b>Команды</b>\n\n"
            "<code>/admin</code> — запросить доступ к админке\n"
            "<code>/admin11i</code> — скрытая команда запроса доступа\n"
            "<code>/menu</code> — главное меню\n"
            "<code>/here mp_news</code> — назначить ЭТОТ чат/тему как приёмник\n"
            "<code>/here marketing</code> — то же для маркетинга\n"
            "<code>/add @username mp_news|marketing</code>\n"
            "<code>/addlist mp_news</code> + список каналов строками\n"
            "<code>/remove @username</code>\n"
            "<code>/move @username mp_news|marketing</code>\n"
            "<code>/pause @username</code>\n"
            "<code>/resume @username</code>\n"
            "<code>/list mp_news|marketing</code>\n"
            "<code>/stats</code> — статистика за день\n"
            "<code>/runjob</code> — сразу отправить дайджест за сегодня, не очищая очередь\n"
            "<code>/cancel</code> — сбросить текущий ввод\n\n"
            "💡 Бот копит посты весь день по московской дате и после 09:00 МСК отправляет сводку за вчера."
        )
        await c.message.edit_text(text, reply_markup=main_menu_kb())
        await c.answer()

    @r.callback_query(F.data == "noop")
    async def cb_noop(c: CallbackQuery):
        await c.answer()

    return r


async def _refresh_channel_view(c: CallbackQuery, conn, username: str) -> None:
    ch = st.get_channel(conn, username)
    if not ch:
        return
    status = "⏸ на паузе" if ch.paused else "▶️ активен"
    mode = "🕘 дайджест" if ch.mode == "digest" else "⚡ live"
    text = (
        f"<b>@{ch.username}</b>\n\n"
        f"Категория: {CATEGORY_LABEL[ch.category]}\n"
        f"Статус: {status}\n"
        f"Режим: {mode}\n"
        f"Ссылка: https://t.me/{ch.username}"
    )
    try:
        await c.message.edit_text(text, reply_markup=channel_actions_kb(ch.username, ch.paused, ch.category))
    except Exception:
        pass


def _stats_text(conn) -> str:
    day = datetime.utcnow().strftime("%Y-%m-%d")
    s = st.stats_for_day(conn, day)
    yday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    sy = st.stats_for_day(conn, yday)
    q_w_mp = st.queue_size(conn, "weekend:mp_news")
    q_w_m = st.queue_size(conn, "weekend:marketing")
    q_oz = st.queue_size(conn, "ozon_novosti")

    def cell(d, cat, metric):
        return d.get((cat, metric), 0)

    def ads_total(d, cat):
        return cell(d, cat, "blocked_ad") + cell(d, cat, "blocked_ai_ad")

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"<b>Сегодня</b> ({day})\n"
        f"🛒 MP-новости: отправлено {cell(s, 'mp_news', 'sent')}, "
        f"реклама {ads_total(s, 'mp_news')}\n"
        f"📣 Маркетинг: отправлено {cell(s, 'marketing', 'sent')}, "
        f"реклама {ads_total(s, 'marketing')}\n\n"
        f"<b>Вчера</b> ({yday})\n"
        f"🛒 MP-новости: {cell(sy, 'mp_news', 'sent')}\n"
        f"📣 Маркетинг: {cell(sy, 'marketing', 'sent')}\n\n"
        f"<b>В очередях</b>\n"
        f"🕘 ozon_novosti: {q_oz}\n"
        f"📅 уикэнд MP: {q_w_mp}\n"
        f"📅 уикэнд маркетинг: {q_w_m}\n"
    )
    return text


# ---------- runner -----------------------------------------------------------

MSK = ZoneInfo("Europe/Moscow")


def _stats_text(conn) -> str:
    today = datetime.now(MSK).date()
    yesterday = today - timedelta(days=1)
    today_s = today.isoformat()
    yesterday_s = yesterday.isoformat()
    s = st.stats_for_day(conn, today_s)
    sy = st.stats_for_day(conn, yesterday_s)

    def cell(d, cat, metric):
        return d.get((cat, metric), 0)

    def ads_total(d, cat):
        return cell(d, cat, "blocked_ad") + cell(d, cat, "blocked_ai_ad")

    q_today_mp = st.queue_size(conn, f"daily:{today_s}:mp_news")
    q_today_m = st.queue_size(conn, f"daily:{today_s}:marketing")
    q_yday_mp = st.queue_size(conn, f"daily:{yesterday_s}:mp_news")
    q_yday_m = st.queue_size(conn, f"daily:{yesterday_s}:marketing")

    return (
        "📊 <b>Статистика</b>\n\n"
        f"<b>Сегодня</b> ({today_s})\n"
        f"🛒 MP: в очереди {q_today_mp}, реклама {ads_total(s, 'mp_news')}\n"
        f"📣 Маркетинг: в очереди {q_today_m}, реклама {ads_total(s, 'marketing')}\n\n"
        f"<b>Вчера</b> ({yesterday_s})\n"
        f"🛒 MP: в очереди {q_yday_mp}, утренних сводок {cell(sy, 'mp_news', 'sent_daily_digest')}\n"
        f"📣 Маркетинг: в очереди {q_yday_m}, утренних сводок {cell(sy, 'marketing', 'sent_daily_digest')}\n\n"
        "<b>Режим</b>\n"
        "Бот копит посты весь день по московской дате и после 09:00 МСК отправляет сводку за вчера."
    )


async def run_admin_bot(
    bot: Bot,
    conn,
    run_job: Callable[[str | None], Awaitable[str]] | None = None,
) -> None:
    """Запускает aiogram-роутер на ПЕРЕДАННОМ боте (тот же, что шлёт посты)."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(conn, bot, run_job=run_job))
    me = await bot.get_me()
    log.info("Админ-панель работает в боте @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, handle_signals=False)
