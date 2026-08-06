# Custom sandboxd sandbox image for loop-orchestrator phase 3 (E2E).
# Extends the stock sandboxd image with the playwright-cli toolchain so
# every run starts with browsers ready and the skill visible to Claude Code.
#
# Build on the VPS (find the current image with: docker inspect --format
# '{{.Config.Image}}' $(docker ps -q --filter name=s-) or check SANDBOXD_IMAGE
# in the sandboxd config):
#   docker build --build-arg BASE_IMAGE=<current sandbox image> \
#     -t loop-sandbox:latest deploy/sandbox-image
# Then set SANDBOXD_IMAGE=loop-sandbox:latest for sandboxd and restart it.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @playwright/cli@latest

# Browsers + system deps for headless chromium, shared by every sandbox user.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN npx playwright install --with-deps chromium

# Skills must land in /opt/sandbox-skel, NOT in /home/sandbox.
#
# sandboxd bind-mounts a per-sandbox loopback workspace over /home/sandbox, so
# everything the image puts there is invisible at runtime. The home directory a
# sandbox actually gets is seeded once from this skeleton — see
# control-plane/internal/loopback/loopback.go: a one-shot container runs
# `cp -aT /opt/sandbox-skel/. /target/ && chown -R sandbox:sandbox /target`.
#
# The playwright-cli skill lived under /home/sandbox from phase 3 and therefore
# never reached a single agent; the e2e stage worked because its prompt names
# the CLI and the CLI is installed globally above.
#
# Verify after a build:
#   docker run --rm --entrypoint sh loop-sandbox:latest -c 'ls /opt/sandbox-skel/.claude/skills'
RUN mkdir -p /opt/sandbox-skel/.claude/skills/playwright-cli
ADD https://raw.githubusercontent.com/microsoft/playwright-cli/main/skills/playwright-cli/SKILL.md \
    /opt/sandbox-skel/.claude/skills/playwright-cli/SKILL.md

# Planning skills for the planner agent (phase 5a backlog mode). Vendored into
# this repository rather than fetched, so a build is reproducible and the local
# adaptations survive: the upstream superpowers skills assume a human to ask and
# sibling skills that do not exist in a sandbox. Update them by editing
# deploy/sandbox-image/skills/ — see the comment at the top of each SKILL.md.
COPY skills/ /opt/sandbox-skel/.claude/skills/

RUN chown -R sandbox:sandbox /opt/sandbox-skel/.claude /opt/pw-browsers

# Restore the stock image's unprivileged user (USER root above would
# otherwise leak into the final image and every sandbox would run as root).
USER sandbox
