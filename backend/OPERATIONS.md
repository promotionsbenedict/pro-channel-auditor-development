# Pro Channel Auditor: operations guide

Server path: `/opt/pro-channel-auditor-development/backend`. Every command below runs from there.

## Services

| Service    | Role                                                        | Health check                   |
|------------|-------------------------------------------------------------|--------------------------------|
| `postgres` | Data (volume `pgdata`). Not published on the host.          | `pg_isready`                   |
| `redis`    | Subscription cache and FSM state (volume `redisdata`).      | `redis-cli ping`               |
| `migrate`  | Runs `alembic upgrade head`, then exits.                    | must exit 0                    |
| `bot`      | `@prochannelauditor_bot` (polling).                         | `python -m pca.bot.healthcheck`|
| `worker`   | Claims audit jobs, scans join requests, finalises credits.  | `python -m pca.worker.healthcheck` |
| `api`      | Connect Mini App, Paystack webhook/callback, payment sweep. | `GET /readyz` on `127.0.0.1:18080` |

All services use `restart: unless-stopped`. Nothing has to be started by hand in a terminal.

## First-time setup of new settings

1. `deploy/setup-env.sh` creates `.env` if it is missing. It never overwrites an existing one.
2. Fill in, in `.env`:
   - `PCA_PUBLIC_BASE_URL`, `PCA_TELEGRAM_API_ID`, `PCA_TELEGRAM_API_HASH` (account connection).
   - `PCA_PAYSTACK_SECRET_KEY` (start with `sk_test_...`).
3. In Paystack (Settings → API Keys & Webhooks), set the webhook URL to `<PCA_PUBLIC_BASE_URL>/webhooks/paystack`.
4. Add the routes in `deploy/Caddyfile.snippet` to the existing `prochannelhelpbot` Caddy, then reload it:
   `docker exec prochannelhelpbot-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.

Never change `PCA_SESSION_KEYS` by replacing the key. To rotate, add a new `id:key` pair, then point `PCA_SESSION_ACTIVE_KEY_ID` at it. Removing an old key makes the sessions sealed with it unreadable.

## Deploying an update

```bash
deploy/reconcile-check.sh            # read-only: shows manual VPS edits vs the repo
git stash push -m vps-manual-fixes   # only if the check listed local edits you want to keep
deploy/update.sh                     # git pull --ff-only, backup, build, migrate, start
```

`deploy.sh` backs up the database, tags the running image as `pca-app:<stack>-previous`, builds, migrates and starts `bot worker api`. It never removes volumes.

## Rollback

```bash
deploy/rollback.sh
```

This takes a backup, then restarts the services on the previous image. Migrations here are additive (new tables, columns and indexes only), so the previous code runs against the newer schema, and **no schema downgrade is needed or performed**. Before the next deploy, put the code back to that version with `git log` and `git checkout <commit>`.

## Backups

- `deploy/backup.sh` writes `backups/pca-<UTC time>.dump` (mode 600) and keeps the newest 14.
- To run it nightly: `crontab -e` and add
  `15 3 * * * cd /opt/pro-channel-auditor-development/backend && deploy/backup.sh >> backups/cron.log 2>&1`
- Copy backups off the server regularly. A backup kept only on the VPS doesn't protect you if the VPS is lost.
- Backups contain encrypted sessions. They're only usable together with `PCA_SESSION_KEYS`, so store `.env` separately and securely.

### Restoring a backup (deliberate, destructive to current data)

```bash
docker compose stop bot worker api
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backups/<file>.dump
docker compose up -d --wait bot worker api
```

Before you restore, check the backup by restoring it into a staging stack (`PCA_STACK_NAME=pca-staging`).

## Checking health

```bash
docker compose ps
docker compose logs --tail=100 bot worker api
curl -fsS http://127.0.0.1:18080/readyz
```

In the bot, `/health` shows database, queue, stalled-job and wallet-reconciliation status.

## Admin commands (numeric admin IDs only)

`/admin` stats · `/health` · `/user <id>` · `/grant <id> <credits> <reason>` · `/debit <id> <credits> <reason>` ·
`/promo <id> <credits> <reason>` · `/suspend <id>` · `/unsuspend <id>` · `/jobs` · `/failures` ·
`/job_cancel <job>` · `/job_retry <job>` · `/packages` · `/package_set ...` · `/package_off <id>` ·
`/admin_pricing` · `/tier_set ...` · `/tier_off <id>` · `/tickets` · `/reply <ticket> <text>` ·
`/close_ticket <ticket>` · `/security` · `/admin_log` · `/sub_channel` · `/sub_enforce` · `/sub_status`.

Every credit change records the admin, the reason and the amount in the ledger and the admin log.

## Credit policy

Credits are reserved when an audit is created. A completed audit settles the quoted amount and releases the rest. A failed or cancelled audit, or one interrupted by a revoked session, releases the whole reservation. Payments are credited only after Paystack's verify API confirms the reference, amount and currency, and each reference is credited at most once. A sweep in the API process credits payments whose webhook never arrived.

## Telegram and Paystack limitations (what the product does not claim)

- **Join requests only.** Audits read *pending join requests* through `messages.getChatInviteImporters`. They can't see existing members' activity, message reads or real-world identity.
- **Permissions.** The customer's own account must be an admin of the channel with the right to manage join requests. Channels without it show as not auditable.
- **Last seen is approximate.** Telegram returns buckets (`recently`, `within a week`, `within a month`, `long ago`) or nothing at all, depending on each user's privacy settings. A hidden status is reported as unknown, not as inactive. "Recently" is never treated as "within 24 hours".
- **Rate limits.** Telegram enforces FloodWait. The worker pauses and resumes from a checkpoint, so a large channel can take a long time to finish.
- **Bot vs user API.** The bot can't read join requests in bulk. A connected user session (MTProto) is required, and its use is bound by Telegram's API Terms of Service. Customers should connect only accounts they own.
- **Sessions can end.** If a customer ends the session in Telegram (Settings → Devices), audits stop, are marked partial, and are not charged.
- **Paystack.** Only card/bank methods enabled on your Paystack account are offered. Payments are in the currency configured on the package. Refunds are handled in the Paystack dashboard, then reflected with `/debit`.

## Privacy

- Sessions are encrypted with AES-GCM (bound to user and account) and never logged. Logs are redacted for tokens, sessions, passwords and codes.
- Reports and CSVs hold numeric user IDs and status flags only, never names, usernames or phone numbers.
- "Delete my data" in the bot logs the session out on Telegram, wipes the stored ciphertext, revokes channel access and marks the account for deletion.
