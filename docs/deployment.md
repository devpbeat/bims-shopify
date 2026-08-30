# Deployment runbook

Deploys `bims-shopify` to `mystoresync.ignitesolutions.click` behind the shared
Traefik instance (`traefik_web` network), following the pattern in
`docs/deploy-pattern.md`. GitHub Actions builds and pushes the image to GHCR, then
triggers a Woodpecker pipeline that pulls it on the VPS.

## Architecture

- **Image registry**: `ghcr.io/devpbeat/bims-shopify` (`:latest` + `:<sha>`).
- **CI/CD**: `.github/workflows/ci.yml` (lint, test, build/push, trigger Woodpecker).
- **Deploy**: `.woodpecker.yml`, triggered via the Woodpecker API, never on push.
- **Host path**: `/home/ubuntu/apps/bims-shopify` on the VPS.
- **Reverse proxy**: Traefik, `traefik_web` network, `myresolver` certresolver,
  `secure-headers@file` middleware.
- **Database**: per-app Postgres container (`bims-shopify-db`), on the private
  `internal` network only, not reachable from Traefik.

## First-time VPS setup

1. Confirm the Traefik stack is already running and the external `traefik_web`
   network exists (`docker network ls | grep traefik_web`). If not, this repo does
   not create it — it must come from the shared `little-infra` Traefik stack.
2. Create the app directory and clone the repo:
   ```bash
   sudo mkdir -p /home/ubuntu/apps/bims-shopify
   sudo chown ubuntu:ubuntu /home/ubuntu/apps/bims-shopify
   cd /home/ubuntu/apps/bims-shopify
   git clone git@github.com:devpbeat/bims-shopify.git .
   ```
3. Copy the env template and fill in real secrets:
   ```bash
   cp env.prod.example .env
   $EDITOR .env
   ```
   Generate `ADMIN_TOKEN` and `FERNET_KEY` with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Bring the stack up for the first time:
   ```bash
   docker compose -p bims-shopify pull
   docker compose -p bims-shopify up -d
   ```
5. Migrations run automatically at app startup (`alembic upgrade head` in the
   FastAPI lifespan; look for the `database_migrated` log line). See the
   "legacy DB" note below only if importing an existing dataset.
6. Verify:
   ```bash
   curl -sf https://mystoresync.ignitesolutions.click/health
   ```

## Ongoing deploys (automatic)

Push to `main` → GitHub Actions runs lint/test → builds and pushes the image to
GHCR → calls the Woodpecker API to trigger `.woodpecker.yml`, which:

1. `git fetch` + `git reset --hard origin/main` on the host bind mount.
2. `docker compose -p bims-shopify pull && up -d --no-deps app` (Postgres is left
   untouched).
3. Polls `docker inspect` until the `app` container's HEALTHCHECK reports
   `healthy`, or fails the pipeline after ~75s.

Schema changes ship inside the image: the app runs `alembic upgrade head` on
startup, so a deploy that includes a migration applies it before serving traffic.
Confirm with `docker compose -p bims-shopify logs app | grep database_migrated`.

## Legacy/existing DB note

If a pre-existing dataset is ever imported into this Postgres instance outside of
Alembic (e.g. restored from a dump that predates this repo's migration history),
stamp the DB at the matching revision instead of running `upgrade head` blind:
```bash
docker compose -p bims-shopify exec app uv run alembic stamp <revision>
docker compose -p bims-shopify exec app uv run alembic upgrade head
```
Check `alembic/versions/` for the correct starting revision before stamping.

## Rollback

Every build pushes both `:latest` and `:<sha>`. To roll back:
```bash
cd /home/ubuntu/apps/bims-shopify
docker compose -p bims-shopify pull  # optional, if the sha image isn't local yet
docker tag ghcr.io/devpbeat/bims-shopify:<known-good-sha> ghcr.io/devpbeat/bims-shopify:latest
docker compose -p bims-shopify up -d --no-deps app
```
Find known-good SHAs from GitHub Actions run history or `git log --oneline main`.

## Logs

```bash
docker compose -p bims-shopify logs -f app
docker compose -p bims-shopify logs -f postgres
```
No centralized log shipping is configured; logs are the default Docker json-file
driver on the host.

## Secrets checklist

**GitHub repo secrets** (Settings → Secrets and variables → Actions):
- `WOODPECKER_TOKEN` — bearer token for the Woodpecker API.
- `WOODPECKER_URL` — base URL of the Woodpecker server.
- `GITHUB_TOKEN` is implicit (used to authenticate to GHCR); no setup needed.

**VPS `.env`** (copied from `env.prod.example`, never committed):
- `DATABASE_URL`
- `ADMIN_TOKEN`
- `FERNET_KEY`
- `SYNC_INTERVAL_MINUTES`
- `LOG_LEVEL`
- `ENVIRONMENT`
- `SHOPIFY_API_KEY`
- `SHOPIFY_API_SECRET`
- `PUBLIC_BASE_URL`

**Manual one-time VPS steps**:
- Ensure `/home/ubuntu/.docker/config.json` has GHCR read credentials (a
  personal access token with `read:packages`, `docker login ghcr.io`).
- Ensure the Woodpecker agent has the `alpine/git` and `docker:cli` images
  available or can pull them.
- Register this repo in Woodpecker and confirm its pipeline is enabled (activated
  repos only accept API-triggered pipelines).
