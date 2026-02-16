# Incident Response Playbook

## Quick Reference

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Empty briefings | API keys missing/expired | Check `config/secrets.json`, rotate keys |
| Briefings slow (>30s) | Agent timeouts | Check circuit breaker: `optimizer.health_report()` |
| No Telegram messages | Bot token invalid | Regenerate at @BotFather, update secrets |
| Analytics missing | D1 unreachable | Check Cloudflare status, verify env vars |
| "Rate limited" errors | User spam or abuse | Rate limits are per-user, wait 15s |

## Scenario 1: Cloudflare D1 Unreachable

**Symptoms:** Analytics writes fail silently, preferences lost on restart.

**Diagnosis:**
```bash
# Check D1 connectivity
curl -s "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/d1/database/$D1_DATABASE_ID" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python -m json.tool
```

**Resolution:**
1. Check Cloudflare status page: https://www.cloudflarestatus.com
2. Verify env vars: `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `D1_DATABASE_ID`
3. If D1 is down: system auto-falls back to local SQLite (data won't sync)
4. When D1 recovers: restart to reconnect

## Scenario 2: Telegram Bot Unresponsive

**Symptoms:** Users get no response to commands.

**Diagnosis:**
```bash
# Test bot token
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
```

**Resolution:**
1. Verify bot token is valid (check response above)
2. Check webhook vs polling mode — if using webhook, verify Cloudflare Worker is running
3. Check GitHub Actions logs for `telegram_handler.yml` failures
4. If token expired: regenerate at @BotFather, update in secrets

## Scenario 3: Research Agents Failing

**Symptoms:** Briefings have fewer items than expected, some sources missing.

**Diagnosis:**
- Check logs for "Circuit breaker OPEN" messages
- Run `/status` command in Telegram to see agent health

**Resolution:**
1. Check if external API is down (Guardian, NewsAPI, Reddit, etc.)
2. If API is rate-limited: wait for cooldown (usually 15min-1hr)
3. If API key expired: rotate key in `config/secrets.json`
4. Free agents (BBC, Google News, HackerNews) don't need keys — verify they work

## Scenario 4: Analytics Database Full

**Symptoms:** Write errors in logs, slow queries.

**Resolution:**
1. Run auto-purge: `engine.analytics.auto_purge(retention_days=90)`
2. For local SQLite: check disk space with `df -h`
3. For D1: check usage in Cloudflare dashboard
4. Consider reducing retention to 60 or 30 days

## Scenario 5: Security Incident (Credential Exposure)

**Immediate Actions:**
1. Rotate ALL exposed credentials immediately
2. Revoke the old tokens/keys on their respective platforms
3. Update `config/secrets.json` with new credentials
4. Check git history: `git log --all --oneline config/secrets.json`
5. If committed to git: run `git filter-branch` to remove from history
6. Force push to all remotes
7. Monitor for unauthorized access in API dashboards

## Escalation Path

1. **L1 (Self-service):** Check this runbook, review logs
2. **L2 (Investigation):** Review audit trail in analytics DB
3. **L3 (Code change):** Create hotfix branch, deploy via CI/CD
