# Deployment pattern (from little-infra + municipal-votes)

Reference repos explored (read-only): `little-infra` (shared VPS platform: Traefik,
Portainer) and `municipal-votes` (a concrete app deployed behind it). This doc distills
the exact pattern to replicate for a new FastAPI service at
`mystoresync.ignitesolutions.click`.

## 1. Traefik reverse proxy

Source of truth is `little-infra/traefik/docker-compose.yml` + `traefik/traefik.yml`
(there is also an older/parallel `little-infra/platform/` stack with different env-var
naming — do not copy that one, it's a separate experiment; `municipal-votes` is wired
to the `traefik/` one via the `traefik_web` network name).

**`little-infra/traefik/traefik.yml`** (static config):
```yaml
entryPoints:
  web:
    address: :80
    http:
      redirections:
        entryPoint: { to: websecure, scheme: https, permanent: true }
  websecure:
    address: :443

providers:
  docker:
    endpoint: unix:///var/run/docker.sock
    exposedByDefault: false
    network: traefik_web
  file:
    filename: /dynamic.yml
    watch: true

certificatesResolvers:
  myresolver:
    acme:
      email: admin@ignitesolutions.click
      storage: /acme.json
      httpChallenge:
        entryPoint: web
```

- Entrypoints: `web` (80, redirects to https) and `websecure` (443).
- Cert resolver name: **`myresolver`** (HTTP-01 challenge). A `cloudflare` DNS-01
  resolver also exists in the same file for wildcard use cases.
- Docker network Traefik watches: **`traefik_web`** (external, created once by the
  Traefik stack; app stacks join it as `external: true`).
- Dashboard: routed via `service=dashboard@internal`, host
  `dashboard.ignitesolutions.click`, protected by `auth@file` + `secure-headers@file`
  middlewares (`little-infra/traefik/docker-compose.yml`).

**Exact labels a service uses** (from `municipal-votes/docker-compose.yml`, the `app`
service — this is the pattern to copy verbatim, swap `fdovotes` → your service name):
```yaml
networks:
  - traefik_web
  - internal
labels:
  - traefik.enable=true
  - traefik.docker.network=traefik_web
  - traefik.http.routers.fdovotes.rule=Host(`fdovotes.ignitesolutions.click`)
  - traefik.http.routers.fdovotes.entrypoints=websecure
  - traefik.http.routers.fdovotes.tls=true
  - traefik.http.routers.fdovotes.tls.certresolver=myresolver
  - traefik.http.routers.fdovotes.middlewares=fdovotes-permissions,secure-headers@file
  - traefik.http.middlewares.fdovotes-permissions.headers.customresponseheaders.Permissions-Policy=camera=(), microphone=(), geolocation=(self)
  - traefik.http.services.fdovotes.loadbalancer.server.port=8000
```
Minimum required for a new service (no custom Permissions-Policy needed unless you
also need browser feature restrictions):
```yaml
labels:
  - traefik.enable=true
  - traefik.docker.network=traefik_web
  - traefik.http.routers.mystoresync.rule=Host(`mystoresync.ignitesolutions.click`)
  - traefik.http.routers.mystoresync.entrypoints=websecure
  - traefik.http.routers.mystoresync.tls=true
  - traefik.http.routers.mystoresync.tls.certresolver=myresolver
  - traefik.http.routers.mystoresync.middlewares=secure-headers@file
  - traefik.http.services.mystoresync.loadbalancer.server.port=8000  # match FastAPI/uvicorn port
```
`secure-headers@file` is defined once in Traefik's dynamic file config (HSTS, frameDeny,
nosniff, etc.) — reuse it rather than redefining per app.

## 2. GitHub Actions (build/publish)

Registry: **GHCR (`ghcr.io`)**, authenticated with the built-in `GITHUB_TOKEN` (no PAT
needed) — see `.github/workflows/build.yml` in `municipal-votes`, job `build-image`.

Image naming/tagging: `ghcr.io/${{ github.repository }}:latest` and
`ghcr.io/${{ github.repository }}:${{ github.sha }}` (two tags pushed on every build,
so the deploy step can always `pull ... :latest`, and `:sha` gives a rollback target).
A secondary image for a sub-service used a `-<name>` suffix
(`ghcr.io/<repo>-baileys:latest`) — same convention would apply to e.g. a worker image.

Trigger: `on: push, pull_request` — but the `build-image` job only runs
`if: github.ref == 'refs/heads/main' && github.event_name == 'push'`, gated behind a
`lint-test` job (`needs: [lint-test]`). Build uses
`docker/build-push-action@v6` with `platforms: linux/arm64` (VPS is ARM — check yours;
adjust to `linux/amd64` if x86) and `cache-from/to: type=gha`.

Secrets used:
- `GITHUB_TOKEN` — implicit, for GHCR login.
- `WOODPECKER_TOKEN` — bearer token to call the Woodpecker API.
- `WOODPECKER_URL` — base URL of the Woodpecker server.

After a successful build, a third job (`deploy`, `environment: production`) looks up
the repo id in Woodpecker and POSTs to `/api/repos/{id}/pipelines` with
`{"branch":"main"}` to fire the Woodpecker deploy pipeline (curl, no dedicated action).

## 3. Woodpecker (deploy)

File: `.woodpecker.yml` at repo root. **Trigger is manual/API only, never on push**
(`when: event: [manual, api]`) — GitHub Actions triggers it explicitly via the API call
above, right after a successful `main` build. Two steps:

**Step `update`** (image `alpine/git`) — updates the on-host working copy in place:
```yaml
volumes:
  - /home/ubuntu/apps/municipal-votes:/srv/app
  - /home/ubuntu/apps:/srv/parent:ro
  - /home/ubuntu/.ssh:/home/ubuntu/.ssh:ro
commands:
  - OWNER=$(stat -c '%u:%g' /srv/parent)
  - chown -R "$OWNER" /srv/app
  - cd /srv/app
  - git -c safe.directory=/srv/app fetch --prune origin
  - git -c safe.directory=/srv/app reset --hard origin/main
  - chown -R "$OWNER" /srv/app
```
Key detail: ownership is chowned to whatever the *parent* directory
(`/home/ubuntu/apps`) is owned by — read dynamically, not hardcoded uid 1000 — because
the interactive/SSH user's uid varies by host. `fetch` + `reset --hard` (not `pull`) for
a deterministic, conflict-free checkout; `.env` and other gitignored files on the host
survive because reset only touches tracked files.

**Step `deploy`** (image `docker:cli`) — pulls the fresh image and restarts just the
app containers, mounting the host's docker socket and docker CLI credentials:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - /home/ubuntu/apps/municipal-votes:/srv/app
  - /home/ubuntu/.docker/config.json:/root/.docker/config.json:ro
commands:
  - cd /srv/app
  - docker compose -p municipal-votes pull app worker beat
  - docker compose -p municipal-votes rm -sf app worker beat
  - docker compose -p municipal-votes up -d --no-deps app worker beat
  - docker image prune -f
```
`-p <project>` pins the compose project name explicitly (don't rely on directory-name
inference). `--no-deps` limits the restart to the named services, leaving `db`/`redis`
running untouched.

Path convention: app lives on the VPS at **`/home/ubuntu/apps/<repo-name>/`**
(`/opt/<app>` is NOT used here — ask the user to confirm the actual VPS path/user for
this host before replicating, since `little-infra`'s own two stacks disagree: one uses
`ubuntu`/`/home/ubuntu/apps`, the file-config one used a `$PLATFORM_*` env-var
indirection — treat the `municipal-votes` path as the currently-live convention).

Env/secret injection: **`.env` file on the host**, referenced via `env_file: .env` in
`docker-compose.yml`, copied there manually from `.env.prod.example` (not managed by
Woodpecker secrets — Woodpecker only holds `WOODPECKER_TOKEN`/`WOODPECKER_URL` on the
GitHub Actions side; no Woodpecker-native secrets were found in this repo).

No explicit healthcheck step in the Woodpecker pipeline itself — health is enforced at
the compose level (`depends_on: condition: service_healthy` on `db`/`redis`) and
implicitly by Traefik (routers only receive traffic once the container responds on its
declared port).

## 4. Postgres pattern

**Per-app container**, not shared: `db` service in `municipal-votes/docker-compose.yml`
uses `imresamu/postgis:16-3.6-alpine`, `container_name: municipal-votes-db`, own named
volume `pg_data:/var/lib/postgresql/data`, on the app's private `internal` network only
(not exposed to `traefik_web`). Healthcheck: `pg_isready -U $${POSTGRES_USER}`.

Contrast: `little-infra/platform/docker-compose.yml` runs one **shared** `postgres`
container for platform tools (n8n) — that pattern exists but is not what
`municipal-votes` (the closer analog to a new FastAPI app) follows. **Recommendation
for `mystoresync`: per-app Postgres**, matching the currently-deployed pattern, unless
told otherwise.

No automated backup job was found in either repo — ask the user whether backups
(pg_dump cron, volume snapshot, etc.) are expected for the new service.

## 5. Conventions summary

- **Env files**: `.env.example` (local dev defaults) + `.env.prod.example` (annotated
  template for the VPS, copied to `.env` manually, gitignored). Compose services use
  `env_file: .env`, never inline secrets in the compose file.
- **Domains**: `<service>.ignitesolutions.click`, one subdomain per app, matching the
  Traefik router name to the subdomain prefix (`fdovotes` router ↔
  `fdovotes.ignitesolutions.click`). New service → router name `mystoresync`.
- **Container names**: `<repo-name>` for the main app, `<repo-name>-<role>` for sidecars
  (`municipal-votes-db`, `municipal-votes-worker`, `municipal-votes-redis`).
- **Restart policy**: `restart: unless-stopped` on every service, everywhere.
- **Networks**: three-tier per app — `traefik_web` (external, Traefik-facing, only the
  web-facing service joins it), `internal` (`internal: true`, no external routing, for
  db/redis/app-to-app traffic), and optionally `egress` (plain bridge, no ports
  published) for background workers that need outbound internet but shouldn't be
  reachable inbound.
- **Logging**: no centralized log driver override found (Docker default json-file);
  nothing further configured in either repo — ask if the user wants log shipping.
- **Compose files**: single canonical `docker-compose.yml` for production (explicitly
  documented in `municipal-votes` as "the only prod compose file" — a duplicate
  `docker-compose.prod.yml` caused a real incident from config drift); a separate
  `docker-compose.local.yml` selected explicitly with `-f` for local dev, never
  auto-picked.

## Open questions to ask the user before replicating for `mystoresync`

1. VPS host user/path for the new service (`/home/ubuntu/apps/mystoresync/` by
   convention, but confirm — depends on which host/user this deploys under).
2. Whether `WOODPECKER_TOKEN` / `WOODPECKER_URL` GitHub secrets already exist at the org
   level or need to be created for this new repo.
3. CPU architecture of the target VPS (`linux/arm64` vs `amd64`) for the build matrix.
4. Whether Postgres should be per-app (recommended, matches current pattern) or shared.
5. Backup strategy for the new Postgres volume (none exists as precedent).
6. Whether `mystoresync` needs a worker/beat-style background process (Celery
   equivalent) requiring the extra `egress` network, or is a single web service.
