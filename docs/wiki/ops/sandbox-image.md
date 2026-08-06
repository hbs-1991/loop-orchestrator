# Ops: the `loop-sandbox` sandbox image

Source — `deploy/sandbox-image/` (Dockerfile + `skills/`), build instructions — in the header of the
Dockerfile itself. Built **manually on the VPS**, never updated by deploy (4.5 GB, shared with sandboxd).

## What's inside

`FROM sandboxd-base:0.3.0` + ffmpeg, `@playwright/cli`, chromium in `/opt/pw-browsers`, vendored
skills. Size ~4.53 GB.

## Build

```bash
# base image — from the root of the sandboxd repository (the context must be the root!)
cd ~/.sandboxd/src && docker build -f image/Dockerfile -t sandboxd-base:0.3.0 .
# our image on top of it
cd ~/loop/deploy/sandbox-image && docker build --build-arg BASE_IMAGE=sandboxd-base:0.3.0 -t loop-sandbox:latest .
```

Wiring it up: `SANDBOXD_IMAGE=loop-sandbox:latest` in `~/.sandboxd/src/.env`, then
`docker compose up -d sandboxd` (the service is called `sandboxd`, not control-plane).

## Skills go into `/opt/sandbox-skel/.claude/skills/`

Not into `/home/sandbox` — that directory is shadowed by the per-sandbox loopback workspace, and
anything the image writes there is invisible at runtime. Full story —
[[decisions/0004-skills-into-sandbox-skel]].

Check after a build:

```bash
docker run --rm --entrypoint sh loop-sandbox:latest -c 'ls /opt/sandbox-skel/.claude/skills'
```

Right now it holds: `writing-plans`, `writing-specs` (vendored adaptations of the superpowers skills
for the planner) and `playwright-cli`.

## Gotchas

- **Don't lose `USER sandbox`** after `USER root` in the Dockerfile — a bug inherited from the phase 3 plan.
- The images are vulnerable to the cron prune — see the incident in [[ops/vps]]; the anchor containers
  `keep-loop-sandbox`/`keep-sandboxd-base` keep them "in use".
- Playwright in the target repository can drift in version against the pre-baked browser — that's why
  the frontend's `.loop.yml` runs `pnpm exec playwright install chromium` in `setup`.

## Links

[[concepts/agent-steering]] · [[ops/vps]] · [[ops/target-repos]]
