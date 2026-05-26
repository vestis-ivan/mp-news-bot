FROM python:3.12-slim

# Системные зависимости для cryptg (быстрая расшифровка MTProto)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config.yaml .
COPY test_bot_only.py .
COPY test_openai.py .
COPY test_ai_from_posts.py .

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

CMD ["python", "-m", "app.bot"]
