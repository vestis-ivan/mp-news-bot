# VPS setup

## 1. Upload and unpack

Upload `mp-news-bot-vps.tar.gz` to the VPS, then:

```bash
mkdir -p ~/mp-news-bot
tar -xzf mp-news-bot-vps.tar.gz -C ~/mp-news-bot --strip-components=1
cd ~/mp-news-bot
```

## 2. Create `.env`

```bash
cp .env.vps.example .env
nano .env
```

Fill:

```env
TG_API_ID=
TG_API_HASH=
TG_PHONE=
TG_BOT_TOKEN=
ADMIN_USER_IDS=
ADMIN_APPROVER_USERNAME=def325
ADMIN_APPROVER_USER_ID=
OPENAI_API_KEY=
```

Admin approval works like this: when a user sends `/admin` or `/admin11i`, the bot sends an approval request to `ADMIN_APPROVER_USERNAME` with approve/deny buttons.

For reliable delivery, send `/id` from `@def325`, copy the numeric Telegram ID, then put it into `.env`:

```env
ADMIN_APPROVER_USERNAME=def325
ADMIN_APPROVER_USER_ID=123456789
```

`ADMIN_USER_IDS` still works as a direct allowlist:

```env
ADMIN_USER_IDS=123456789
```

For several admins:

```env
ADMIN_USER_IDS=123456789,987654321
```

After changing `ADMIN_USER_IDS`, restart:

```bash
docker compose restart
```

If the VPS opens Telegram and OpenAI directly, leave proxies empty:

```env
TELEGRAM_PROXY=
TELEGRAM_BOT_PROXY=
TELETHON_PROXY=
OPENAI_BASE_URL=
OPENAI_PROXY=
```

If OpenAI needs a proxy:

```env
OPENAI_PROXY=socks5://login:password@ip:port
```

If Telegram needs a proxy:

```env
TELETHON_PROXY=socks5://login:password@ip:port
TELEGRAM_BOT_PROXY=socks5://login:password@ip:port
```

`TELETHON_PROXY` is for reading channels through the user session.
`TELEGRAM_BOT_PROXY` is for BotFather Bot API commands and posting.

## 3. First interactive login

Install Docker and Compose if needed, then:

```bash
docker compose build
docker compose run --rm bot
```

Enter the Telegram code in the VPS console when Telethon asks:

```text
Please enter the code you received:
```

If Telegram asks for a cloud password, enter the 2FA password.
After successful login, stop with `Ctrl+C`. The session will be saved in `data/user.session`.

## 4. Run 24/7

```bash
docker compose up -d
docker compose logs -f --tail=100
```

## 5. Quick checks

Check BotFather token/admin panel only:

```bash
docker compose run --rm bot python test_bot_only.py
```

Check OpenAI only:

```bash
docker compose run --rm bot python test_openai.py
```

Check real channel post plus AI dry-run:

```bash
docker compose run --rm bot python test_ai_from_posts.py maxprowb --limit 10 --analyze 1
```

## 6. Daily digest mode

The bot now collects posts during the day by Moscow date and sends the previous day's digest after `daily_digest.send_hour_msk` in `config.yaml` (default: 09:00 MSK).
Before a post enters the daily queue, the bot filters obvious ads by regex and then asks OpenAI to classify hidden/self-promo ads. Filtered ads are counted in `/stats` and in the digest's "Не вошло" section.
The daily digest is selective: OpenAI ranks posts by importance, includes only high-priority items, and moves weak opinions, repeats, minor cases, and noise into "Не вошло". It also adds action points, hypotheses to test, and practical observations when they follow from the posts.

The bot also has a catch-up scanner. In addition to live Telegram updates, it walks through all DB channels every `catchup.interval_seconds` seconds (default: 300), reads the latest `catchup.limit_per_channel` posts, and adds unseen messages to the same daily queue.

Admin commands:

```text
/runjob          # send today's already collected queue immediately
/runjob today
/runjob yesterday
/runjob 2026-05-25
```

Manual `/runjob` does not clear the queue. The scheduled 09:00 MSK digest will still send the full previous day and only then clear that day's queue.

Add several Telegram channels at once:

```text
/addlist mp_news
@channel_one
https://t.me/channel_two
channel_three
```

Every digest is sent to the configured `/here` target and copied to all approved admins.

## 7. Update after replacing files

```bash
docker compose build
docker compose up -d
docker compose logs -f --tail=100
```
