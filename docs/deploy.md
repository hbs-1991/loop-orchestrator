# Deploying loop-orchestrator

## 1. sandboxd on the VPS

```bash
curl -fsSL https://raw.githubusercontent.com/tastyeffectco/sandboxd/main/install.sh | bash
```

Go through the console's one-time setup. Check: `curl http://127.0.0.1:9090/healthz`.

## 2. sandboxd API key

In the sandboxd console (or `POST /v1/api-keys` under a session) create a key → `LOOP_SANDBOXD_API_KEY`.

## 3. Connect the Claude subscription

In the sandboxd console: Settings → AI Agents → Claude Code → OAuth
(the `/v1/agents/claude-code/oauth/start|finish` endpoints). Make sure a test
task in the console runs under claude-code.

## 4. Git credential (a PAT for clone and push)

```bash
curl -sS -X POST http://127.0.0.1:9090/v1/git-credentials \
  -H "Authorization: Bearer $LOOP_SANDBOXD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "github-pat", "username": "x-access-token", "token": "<GITHUB_PAT>"}'
```

Take `id` from the response → `LOOP_GIT_CREDENTIAL_ID`. If the API returns 400 with a list
of required fields — fix the body per the hint (the shape is verified at this step).

## 5. The orchestrator

```bash
git clone <this repository> /opt/loop && cd /opt/loop
cp .env.example .env && $EDITOR .env
mkdir -p secrets && chmod 700 secrets
docker compose up -d --build
curl http://127.0.0.1:8000/healthz  # from inside the network — or via the traefik host
```

## 6. Network and Traefik

`docker network ls` — find the sandboxd network (e.g. `sandboxd_default`) and
fix `networks:` in docker-compose.yml. `docker ps` — the control-plane
container name → `LOOP_SANDBOXD_URL`. Check the entrypoint (`websecure`) and
the certresolver (`letsencrypt`) against sandboxd's traefik config
(`/opt/sandboxd/traefik/…`); DNS `LOOP_WEBHOOK_HOST` → the VPS IP.

## 7. Connecting a repository

```bash
# project secrets (if any are needed)
echo "DATABASE_URL=..." > secrets/owner__repo.env && chmod 600 secrets/owner__repo.env
python scripts/connect_repo.py owner/repo https://$LOOP_WEBHOOK_HOST/webhooks/github
```

The repository must contain a `.loop.yml` (see the spec, the "Repository conventions" section).

## Smoke test (MVP acceptance)

1. A test repository with a `.loop.yml`, a simple project and a
   spec+plan pair for a toy feature ("add a /ping endpoint").
2. Open a PR with that pair, put the `loop:run` label on it.
3. Expected: the label switches to `loop:running`; Telegram receives
   "queued" and "started"; a few minutes later — the agent's commits
   in the PR branch, the `loop:done` label, a comment with the summary and
   "finished" in Telegram.
4. Negative run: a PR without a plan → an instant `loop:failed` with the reason.

## Phase 2 smoke test (Reviewer)

1. In `<org>/loop-smoke-test`, prepare a PR with a spec+plan where the plan
   deliberately carries a bug (e.g. the endpoint returns 200 without the validation
   the spec asks for). Put the `loop:run` label on it.
2. Expected: the loop goes through `reviewing`, the review finds the mismatch with the spec,
   a fix iteration corrects it; the PR gets the post-fix code, the `loop:done` label
   and two comments — the Run report and "🤖 loop-orchestrator — review (Fable 5)"
   with a ✅ clean verdict and a "Fixed in the fix cycle" list.
3. Repeat with `review: {max_fix_iterations: 0}` in the test repo's `.loop.yml`:
   the same PR must end with the `loop:needs-review` label, a
   "⚠️ findings remain" comment and an escalation in Telegram.
4. Check `review: {enabled: false}` — the loop runs as in phase 1, no review tasks.

## Phase 3: custom sandbox image (E2E)

E2E runs need playwright-cli, chromium and ffmpeg inside the sandbox. They are
baked into a custom image built FROM the stock sandboxd sandbox image
(sandboxd applies one image instance-wide via SANDBOXD_IMAGE; per-app images
are rejected by the API).

On the VPS:

1. Find the current sandbox image name:
   `docker inspect --format '{{.Config.Image}}' $(docker ps -q --filter name=s- | head -1)`
   (or check the sandboxd config in `~/.sandboxd`).
2. Copy `deploy/sandbox-image/Dockerfile` to the VPS and build:
   `docker build --build-arg BASE_IMAGE=<stock image> -t loop-sandbox:latest .`
3. Before building, verify the in-image user and home
   (`docker run --rm <stock image> sh -c 'echo $HOME; id'`) and adjust the
   Dockerfile paths/chown if they differ from `/home/sandbox`.
4. Point sandboxd at the new image (SANDBOXD_IMAGE=loop-sandbox:latest in its
   environment) and restart sandboxd.
5. Verify: create a throwaway sandbox, run a task that calls
   `playwright-cli --help` and `ffmpeg -version`, and check the skill is listed
   by Claude Code.

Smoke test for phase 3 needs a small web-app repository (a Vite frontend) with
`.loop.yml` containing `run: npm run dev -- --port 3000` and an `e2e:` block —
the Python CLI smoke repo cannot exercise the E2E stage. Scenarios to cover:
a UI feature PR reaching `loop:done` with a video in Telegram; a planted UI bug
fixed by the e2e fix cycle; `e2e.max_fix_iterations: 0` with a bug escalating
with a failure video.

## Telegram topics and the progress card

Runs are delivered into per-run forum topics with a live progress card. For
topics to work the target chat must support them (a supergroup with Topics
enabled, or a private bot chat on Bot API 10.0); otherwise the bot silently
falls back to flat delivery — no configuration needed. Set `LOOP_TZ`
(IANA name, e.g. `Asia/Almaty`) in `~/loop/.env` to render card timestamps
in your local time; the default is UTC.

## Phase 4a: approval pause and Telegram control

New env vars in `~/loop/.env`:

- `LOOP_TELEGRAM_ADMIN_IDS` — CSV of Telegram user ids allowed to press run
  buttons and send revise replies (get yours from @userinfobot).
- `LOOP_TELEGRAM_WEBHOOK_SECRET` — any random string; used as the
  `secret_token` of the Telegram webhook.
- `LOOP_PUBLIC_URL` — external base URL of the orchestrator
  (e.g. `https://loop.example.com`). When set together with the secret, the
  orchestrator calls `setWebhook` on startup — no manual BotFather step.
- `LOOP_PREVIEW_TTL_MINUTES` — how long the sandbox (and its preview link)
  lives while a run awaits approval; default 120.

Preview links are sandboxd's native per-sandbox preview
(`https://s-<sandbox>-<port>.preview.<domain>`): make sure the sandboxd
install has its preview domain configured and a wildcard DNS record
`*.preview.<domain>` pointing at the VPS. The port in the link is resolved by
sandboxd at sandbox creation: repo's `sandbox.yaml` (`web: {port: N}`) →
runtime preset → 3000. Repos whose dev server uses a non-3000 port should
commit a `sandbox.yaml`.

By default every run now pauses before publishing (`approval: always`).
Repos that should publish unattended set `approval: never` in `.loop.yml`.

Smoke test:

1. PR in loop-smoke-test with `approval: always` (or nothing — it is the
   default): the run reaches "awaiting approval", the thread gets a pushed
   message with the summary, the e2e video and a working preview link.
2. Open the preview link in a browser; the app responds.
3. Reply to the approval message with a small change request — the agent
   revises, the run returns to a new approval pause.
4. Press ✅ Approve — the PR branch fast-forwards, the run finishes, the
   final message has a 🔀 Merge PR button.
5. Press 🔀 Merge PR — the PR merges (squash), branches are deleted.
6. Press ⛔ Cancel on a running card and 🔁 Restart on the cancelled final —
   a fresh run starts.
7. Wait out `LOOP_PREVIEW_TTL_MINUTES` on a paused run: the preview link
   dies, the card shows the expiry, Approve still publishes.
