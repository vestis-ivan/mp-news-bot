"""Мини-тест бота — проверка работоспособности без Telethon.

Запуск:
  pip install aiogram python-dotenv
  python test_bot_only.py

Что проверяет:
- Токен бота из .env работает
- Бот отвечает на /start и /admin
- Инлайн-кнопки кликаются
- БД создаётся и пишет

НЕ требует: Telegram API (api_id/api_hash), номера телефона, VPS, каналов-источников.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Добавим путь к app, чтобы импортировать наши модули
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage

from app import state as st
from app.admin_bot import build_router
from app.proxy import aiogram_session_from_env, proxy_label

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test")


async def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("❌ TG_BOT_TOKEN не найден в .env")
        sys.exit(1)

    # Создаём БД и сеем тестовые каналы
    db = ROOT / "data" / "test.db"
    if db.exists():
        db.unlink()
    conn = st.connect(db)

    # Засеиваем минимальный список для проверки команд
    st.add_channel(conn, "ozonmarketplace", "mp_news")
    st.add_channel(conn, "maxprowb", "mp_news")
    st.add_channel(conn, "marketpsy", "marketing")
    st.add_channel(conn, "redman", "marketing")
    print(f"✅ Тестовая БД создана: {db}")
    print(f"✅ Засеяно: {len(st.list_channels(conn))} каналов")

    # Запускаем aiogram-роутер
    bot_kwargs = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
    bot_session = aiogram_session_from_env()
    if bot_session:
        bot_kwargs["session"] = bot_session
        print(f"🌐 Telegram Bot API proxy: {proxy_label('bot')}")
    bot = Bot(token=token, **bot_kwargs)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(conn))

    try:
        me = await bot.get_me()
    except TelegramNetworkError as e:
        print("❌ Не удалось подключиться к api.telegram.org:443")
        print(f"Причина: {e}")
        print()
        print("Если ты в РФ и сидишь через VPN, включи в VPN-приложении TUN/System proxy")
        print("или добавь в .env строку вида:")
        print("TELEGRAM_PROXY=socks5://127.0.0.1:1080")
        await bot.session.close()
        sys.exit(2)

    print(f"✅ Бот в Telegram: @{me.username}")
    print()
    print(f"=" * 50)
    print(f"Открой Telegram → найди @{me.username} → нажми Start")
    print(f"Попробуй команды:")
    print(f"  /admin   — стать админом")
    print(f"  /menu    — главное меню с кнопками")
    print(f"  /list    — список тестовых каналов")
    print(f"  /add @testchannel marketing")
    print(f"  /stats")
    print(f"=" * 50)
    print(f"Прерви через Ctrl+C когда наиграешься.")
    print()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Остановлено")
