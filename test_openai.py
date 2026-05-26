"""Мини-тест OpenAI — проверка ключа и подсчёт реального расхода токенов.

Запуск:
  pip install openai python-dotenv
  python test_openai.py

Что проверяет:
- Ключ из .env работает
- gpt-4o-mini отвечает по-русски
- Сколько токенов на реальном посте
- Сколько $ это стоит
"""
import asyncio
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from app.ai_client import make_openai_client


def explain_exception(e: BaseException) -> str:
    parts = [f"{type(e).__name__}: {e}"]
    cur = e.__cause__ or e.__context__
    while cur:
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts)

# Реальный пост из @maxprowb для теста
TEST_POST = """Логистика, после последних изменений на WB сожрала маржу. У многих сделала проект убыточным. Но не все даже до конца разобрались, что случилось.

Если заказ едет покупателю со склада из его же федерального округа — локальный. Если с другого региона — нелокальный. Чем хуже у вас разложен товар по стране, тем выше КТР, тем дороже логистика.

КТР - это множитель на стоимость логистики по каждому заказу. Считается по каждому артикулу отдельно: чем больше локальных заказов — тем меньше КТР, тем дешевле доставка.

ИЛ (индекс локализации) - это средневзвешенный КТР по всем вашим заказам.

И сверху ИРП. Если индекс локализации ниже 60%, WB добавляет до 2,5% на ВСЮ вашу выручку. Не на конкретный артикул, а на всё.

То есть один ходовой товар с плохой локализацией может тянуть вниз весь магазин."""

SYSTEM_PROMPT = """Ты — редактор новостной ленты для селлеров маркетплейсов.
Сделай краткое саммари поста в 2-3 предложения на русском.
Сохрани ключевые цифры, даты, названия продуктов/функций.
Не добавляй ничего от себя. Не используй маркдаун."""


async def main():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("❌ OPENAI_API_KEY не найден в .env")
        sys.exit(1)

    client = make_openai_client(key)

    print("Отправляю тестовый пост в gpt-4o-mini…")
    print(f"Длина поста: {len(TEST_POST)} символов")
    print()

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": TEST_POST},
            ],
            max_tokens=180,
            temperature=0.3,
        )
    except Exception as e:
        print("❌ OpenAI ошибка:")
        print(explain_exception(e))
        sys.exit(1)

    summary = resp.choices[0].message.content.strip()
    usage = resp.usage

    print("=" * 60)
    print("САММАРИ ОТ AI:")
    print("=" * 60)
    print(summary)
    print()
    print("=" * 60)
    print("РАСХОД ТОКЕНОВ (реальные цифры от OpenAI):")
    print("=" * 60)
    print(f"Input  (промпт + пост):  {usage.prompt_tokens}")
    print(f"Output (саммари):        {usage.completion_tokens}")
    print(f"Всего:                   {usage.total_tokens}")
    print()

    # Цены gpt-4o-mini
    PRICE_IN = 0.15 / 1_000_000   # $ за токен
    PRICE_OUT = 0.60 / 1_000_000

    cost_per_post = usage.prompt_tokens * PRICE_IN + usage.completion_tokens * PRICE_OUT
    print(f"Стоимость этого запроса: ${cost_per_post:.6f}")
    print()

    print("=" * 60)
    print("ПРОГНОЗ:")
    print("=" * 60)
    for posts_per_day in [100, 200, 350, 500]:
        monthly = cost_per_post * posts_per_day * 0.70 * 30  # 70% постов длинные
        print(f"  {posts_per_day} постов/день → ${monthly:.2f}/мес ({monthly*85:.0f} ₽)")

    print()
    print(f"💰 Твой баланс $10 = ${10 / (cost_per_post * 200 * 0.7 * 30):.1f}× больше чем нужно на месяц")
    print(f"   при 200 постах/день → хватит на ~{10 / (cost_per_post * 200 * 0.7 * 30):.0f} месяцев")


if __name__ == "__main__":
    asyncio.run(main())
