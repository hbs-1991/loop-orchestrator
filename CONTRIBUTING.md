# Contributing

Thanks for looking. This project automates a development loop, so it holds itself to the same
standards it asks of the agents it drives: a change is described before it is written, it is covered
by tests, and what was learned while making it is written down.

Read this once before opening a pull request — most of it is not boilerplate. The parts that will
actually get a PR rejected are marked **hard rule**.

## How this repository works

This is a **published snapshot** of a repository that is developed privately and deployed straight to
a single host. Practical consequences:

- Issues and pull requests here are read and welcome. A merged PR is ported into the development
  repository and comes back in the next snapshot, so your commit may reappear squashed and reworded.
  Authorship is preserved in the release notes; if that arrangement does not work for you, say so in
  the PR and we will find another way.
- The history is snapshots, not the real commit-by-commit history. Do not expect `git blame` to
  explain anything. `docs/wiki/log.md` is the actual chronology.
- `deploy.yml` is author-specific — it ships to one VPS and needs `DEPLOY_*` secrets. It will fail
  on your fork and that is expected. `ci.yml` is self-contained and is the check that matters.

## Where to start

Good first contributions, roughly in order of how likely they are to be merged:

- **Portability.** The wiki hooks under `.claude/hooks/` are PowerShell; a POSIX port is wanted.
  Anything that assumes Windows or a single operator is fair game.
- **Platform facts.** If sandboxd behaves differently than `docs/wiki/concepts/sandboxd-platform.md`
  claims, that is a valuable report even without a patch. Say what you ran and what you saw.
- **Bugs with a failing test.** A test that reproduces the problem is worth more than a description
  of it.
- **Features.** Open an issue first — see the next section. A large unannounced PR is the one thing
  most likely to be turned down for reasons that have nothing to do with its quality.

## Before you write code

**Hard rule: anything beyond a bug fix starts with a written design, not with a diff.**

The workflow the project uses on itself is: brainstorm → spec → plan → execute.

1. **Open an issue** describing the problem and what you think should change. For a feature, that
   discussion produces a spec in `docs/superpowers/specs/<YYYY-MM-DD>-<slug>-design.md`, following the
   shape of the existing ones: the problem, the scope of the first cut, a **Locked Decisions** table
   with a *why* per row, the architecture, and the open questions you could not settle.
2. **A spec earns a plan** in `docs/superpowers/plans/`, written as ordered tasks with their tests.
3. **Then the code.** Executing a plan should feel mechanical; if it does not, the plan was wrong and
   fixing the plan comes first.

This is not ceremony. The specs are the reason a stranger can read this codebase, and every "why is
it like that" question in the wiki traces back to one.

## Getting set up

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv/Scripts/pip
python -m pytest tests -v
```

Python 3.12+. That is the whole setup — the suite is self-contained and needs no sandbox, GitHub
account, Telegram bot or network. HTTP is mocked with `respx`, the webhook is driven through an
`httpx.ASGITransport`, and `asyncio_mode = "auto"` means async tests need no decorator. Note that
under the ASGI transport the FastAPI lifespan does not run, so no real clients are ever constructed
in tests.

A single file or a single test:

```bash
python -m pytest tests/test_pipeline_execute.py -v
python -m pytest tests/test_db.py::test_create_and_get -v
```

## The rules a pull request is checked against

**Hard rules.** A PR that breaks one of these will be asked to change before anything else is
discussed.

1. **The suite is green.** `python -m pytest tests` — new behaviour arrives with tests, a bug fix
   arrives with the test that fails without it. There is no linter in CI on purpose (see the comment
   in `ci.yml`); match the style of the file you are editing instead.

2. **English everywhere.** Code, comments, agent prompts, commit messages, PR text, Telegram strings,
   label descriptions, and every document in the repository. A new document is written in English from
   its first line — never "in my language for now, translate later". Rationale is recorded in
   `docs/wiki/decisions/0010-documentation-in-english.md`.

3. **No environment specifics.** Host addresses, domains, GitHub accounts, organisation and repository
   names, ids and absolute local paths are written as placeholders: `<vps-ip>`, `loop.example.com`,
   `<owner>`, `<org>`, `<backend-repo>`. If a value would differ for another reader, it is a
   placeholder. Real values live on the host in `~/loop/.env` and in repository secrets; `.env.example`
   shows the format only. Committing a token, a real hostname or a private repository name is the one
   mistake that is expensive to undo.

4. **Settings go through `Settings`.** pydantic-settings, prefix `LOOP_`, one definition. No
   `os.environ` reads scattered through the code, and a new setting is documented in `.env.example`.

5. **HTTP clients take an optional `httpx.AsyncClient`** so tests can inject a transport, and route
   transient failures (5xx, transport errors) through `clients/retry.with_retries`.

6. **The wiki is updated in the same PR** when the change teaches something durable. See below.

## Things that look wrong and are not

**Hard rule: do not "fix" these without a spec change.** Each one is a workaround for a verified
platform constraint, and each has already been re-discovered and re-argued at least once. They are
documented in `docs/wiki/concepts/sandboxd-platform.md` with the date and the method of verification.

- **A sandbox cannot `git push`.** Push is a host-side control-plane operation, into a *new* branch
  only, without force. That is why publication is two-phase: push to `loop/run-<id>`, then
  fast-forward the PR branch through the GitHub API.
- **An app's git branch cannot be changed after creation**, and push cannot fetch or pull. That is why
  every Run creates a fresh app and sandbox instead of reusing one.
- **The app config never reaches the agent.** Secrets travel into the sandbox as a file,
  `.loop/secrets.env`, and the prompts name only the key names. Moving them back into the app config
  would silently deliver nothing.
- **Every stage opens a fresh Claude session.** Inheriting the previous stage's session once put 61%
  of a Run's bill into cache writes (`docs/wiki/decisions/0013-one-session-per-stage.md`). A stage that
  does not inherit must be *told* what the session used to carry — that is why the prompts restate the
  branch, the documents and the test command.

Some things are **Locked Decisions** of the specs and change only by amending the spec first: the
`.loop.yml` schema (it only ever grows — no renames, no removals), the `loop:*` label names, the Run
states and their transitions, and the two-phase publication scheme.

## The wiki is not optional

`docs/wiki/` is the project's memory — what was actually built, how the platform really behaves,
what broke in production. It is the reason a change made three months ago can still be explained.

If your PR implements a feature, settles a decision, uncovers a gotcha or analyses an incident, it
updates the wiki in the same PR: the relevant page under `components/`, `concepts/` or `ops/`, a new
file in `decisions/` if a real trade-off was made, and an entry on top of `log.md`. The rules and the
page templates are in `docs/wiki/conventions.md`.

The boundary matters: the wiki **links to** the specs and plans, it does not duplicate them. A wiki
page says how the thing behaves and why it is that way; the spec says what was decided and what is
still open.

## Commits and pull requests

Commit subjects are lowercase, imperative, and say what changed in the product rather than which
files moved:

```
feat(pipeline): the contracting stage, its publication and the context upload
fix: a dead sandbox fails the stage instead of being waited out
docs(wiki): the upstream API contract handoff
refactor(pipeline): one module per stage instead of one god file
```

In the pull request, describe **what problem this solves and what you decided**, not a file-by-file
tour of the diff — the diff is already there. If you rejected an obvious alternative, one sentence on
why saves the reviewer an hour. Link the issue and the spec.

Keep a PR to one concern. A refactor and a behaviour change in the same diff cannot be reviewed
separately, and one of them will hold up the other.

## Reporting bugs

Include: what you ran, what happened, what you expected, and the relevant `run_events` rows or log
lines. If it involves a sandbox, say which sandboxd version — several documented platform facts have
already changed under us, and a version number is often the whole answer.

**Security issues are not filed as public issues.** Open a
[private security advisory](https://docs.github.com/en/code-security/security-advisories/guiding-contributors-to-report-security-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository instead. The service holds a GitHub token, a Telegram bot token and per-repository
secret files, so anything touching those paths deserves the quiet channel first.

## Code of conduct

Be straightforward and assume competence. Critique the design, not the person. Disagreement is
resolved by evidence — a probe, a trace, a failing test — and the losing side of that argument gets
written into the wiki so nobody has to have it twice.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE) that
covers this project.
