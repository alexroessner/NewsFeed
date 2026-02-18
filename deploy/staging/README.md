# Staging Environment

## Setup

1. Create a **separate** Telegram bot via BotFather for staging
2. Create a **separate** Cloudflare D1 database for staging
3. Copy `.env.staging.example` to `.env.staging` and fill in values
4. Deploy:

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d
```

## Key differences from production

- Lower resource limits (256MB RAM, 0.5 CPU)
- Separate D1 database ID
- Separate Telegram bot token
- Health check on port 8081 (not 8080)
- Shorter log retention
