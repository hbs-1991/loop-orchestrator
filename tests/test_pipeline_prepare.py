import json

import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.models import Run
from loop_orchestrator.pipeline import Pipeline, RunFailure, app_name, build_prompt

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG

LOOP_YML = """
specs_dir: docs/superpowers/specs
setup: npm ci
test: npm test
required_env: [DB_URL]
timeout_minutes: 90
sandbox_preset: node
approval: never
"""


def make_pipeline(db, tmp_path, gh=None, sb=None, tg=None):
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path / "secrets")
    return Pipeline(db=db, settings=settings, gh=gh or FakeGitHub(),
                    sb=sb or FakeSandboxd(), tg=tg or FakeTG())


async def make_run(db) -> Run:
    return await dbmod.create_run(db, "o/myrepo", 5, "feat/x")


def seed_ok(gh: FakeGitHub, tmp_path) -> None:
    gh.files[".loop.yml"] = LOOP_YML
    gh.pr_files = [
        "docs/superpowers/specs/2026-07-31-f-design.md",
        "docs/superpowers/plans/2026-07-31-f.md",
        "src/x.py",
    ]
    sdir = tmp_path / "secrets"
    sdir.mkdir(exist_ok=True)
    (sdir / "o__myrepo.env").write_text("DB_URL=postgres://x\nEXTRA=1\n")


def test_app_name_and_prompt():
    run = Run(id=7, repo="o/myrepo", pr_number=5, head_branch="b", state="queued")
    assert app_name(run) == "loop-myrepo-pr5-r7"
    p = build_prompt("s.md", "p.md", "npm test")
    assert "s.md" in p and "p.md" in p and "npm test" in p and "push" in p.lower()
    assert "install the project dependencies" not in p  # no setup cmd -> no such line
    p2 = build_prompt("s.md", "p.md", "npm test", setup_cmd="npm ci")
    assert "npm ci" in p2 and "install the project dependencies" in p2


async def test_prepare_happy_path(db, tmp_path):
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb)
    run = await make_run(db)
    await pipe._prepare(run)
    assert run.spec_path == "docs/superpowers/specs/2026-07-31-f-design.md"
    assert run.timeout_minutes == 90
    assert run.app_id == "app-1" and run.sandbox_id == "sb-app-1"
    assert sb.apps_created[0]["branch"] == "feat/x"
    assert sb.apps_created[0]["repo_url"] == "https://github.com/o/myrepo.git"
    assert sb.apps_created[0]["preset"] == "node"
    assert ("app-1", "DB_URL", "postgres://x") in sb.secrets
    assert ("app-1", "EXTRA", "1") in sb.secrets  # every secret in the file is uploaded
    saved = await dbmod.get_run(db, run.id)
    assert saved.app_id == "app-1" and saved.prompt
    assert "npm ci" in saved.prompt  # setup from .loop.yml lands in the prompt
    # Secrets travel as a file (app config never reaches the sandbox at all),
    # and the prompt names only the keys — no value settles in the Run record.
    assert sb.files_written == [
        # The gitignore lands FIRST: `.loop/` is committed in some repos, and an
        # agent running `git add -A` must never be able to catch the secrets.
        ("sb-app-1", ".loop/.gitignore", "*\n"),
        ("sb-app-1", ".loop/secrets.env", "DB_URL=postgres://x\nEXTRA=1\n")]
    assert "`DB_URL`" in saved.prompt and ".loop/secrets.env" in saved.prompt
    assert "postgres://x" not in saved.prompt


async def test_prepare_fails_when_secrets_cannot_be_placed(db, tmp_path):
    # Better a clear failure here than a stage that silently runs without
    # credentials and fails later for an unrelated-looking reason.
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    sb.put_file_error = RuntimeError("workspace not ready")
    run = await make_run(db)
    with pytest.raises(RunFailure, match="could not place the project secrets"):
        await make_pipeline(db, tmp_path, gh=gh, sb=sb)._prepare(run)


async def test_prepare_without_secrets_writes_nothing(db, tmp_path):
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] = LOOP_YML.replace("required_env: [DB_URL]\n", "")
    (tmp_path / "secrets" / "o__myrepo.env").unlink()
    run = await make_run(db)
    await make_pipeline(db, tmp_path, gh=gh, sb=sb)._prepare(run)
    assert sb.files_written == []
    saved = await dbmod.get_run(db, run.id)
    assert ".loop/secrets.env" not in saved.prompt


async def test_prepare_snapshots_approval_mode(db, tmp_path):
    gh = FakeGitHub()
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] = LOOP_YML.replace("approval: never", "approval: always")
    pipe = make_pipeline(db, tmp_path, gh=gh)
    run = await make_run(db)
    await pipe._prepare(run)
    assert run.approval_mode == "always"


async def test_prepare_fails_without_loop_yml(db, tmp_path):
    pipe = make_pipeline(db, tmp_path)
    run = await make_run(db)
    with pytest.raises(RunFailure) as e:
        await pipe._prepare(run)
    assert ".loop.yml" in str(e.value)


async def test_prepare_fails_on_missing_secret(db, tmp_path):
    gh = FakeGitHub()
    seed_ok(gh, tmp_path)
    (tmp_path / "secrets" / "o__myrepo.env").write_text("OTHER=1\n")
    pipe = make_pipeline(db, tmp_path, gh=gh)
    run = await make_run(db)
    with pytest.raises(RunFailure) as e:
        await pipe._prepare(run)
    assert "DB_URL" in str(e.value)


async def test_prepare_deletes_previous_apps(db, tmp_path):
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    old = await make_run(db)
    old.app_id, old.state = "app-old", "failed"
    await dbmod.save_run(db, old)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb)
    run = await make_run(db)
    await pipe._prepare(run)
    assert "app-old" in sb.apps_deleted


E2E_YML = (
    "specs_dir: docs/specs\nrun: npm run dev\n"
    "e2e:\n  env:\n    VITE_API_URL: http://localhost:8000\n")

SERVICES_YML = (
    "specs_dir: docs/specs\nrun: npm run dev\n"
    "e2e:\n  services:\n    - repo: o/backend\n")

E2E_NO_TARGET_YML = "specs_dir: docs/specs\ne2e: {}\n"


async def test_prepare_fills_e2e_fields(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    gh.files[".loop.yml"] = E2E_YML
    gh.pr_files = ["docs/specs/2026-08-01-f-design.md", "docs/plans/2026-08-01-f.md"]
    p = Pipeline(db, settings, gh, sb, tg)
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    await p._prepare(run)
    assert run.e2e_enabled is True
    assert run.run_cmd == "npm run dev"
    assert json.loads(run.e2e_env_json) == {"VITE_API_URL": "http://localhost:8000"}
    assert run.e2e_max_iterations == 2


async def test_prepare_rejects_services(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    gh.files[".loop.yml"] = SERVICES_YML
    gh.pr_files = ["docs/specs/2026-08-01-f-design.md", "docs/plans/2026-08-01-f.md"]
    p = Pipeline(db, settings, gh, sb, tg)
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    with pytest.raises(RunFailure, match="e2e.services is not supported yet"):
        await p._prepare(run)


async def test_prepare_rejects_e2e_without_target(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    gh.files[".loop.yml"] = E2E_NO_TARGET_YML
    gh.pr_files = ["docs/specs/2026-08-01-f-design.md", "docs/plans/2026-08-01-f.md"]
    p = Pipeline(db, settings, gh, sb, tg)
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    with pytest.raises(RunFailure, match="neither a run command nor e2e.env"):
        await p._prepare(run)


async def test_prepare_accepts_staging_mode(db, tmp_path):
    # No run command, but e2e.env points at an external stand — valid.
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    gh.files[".loop.yml"] = (
        "specs_dir: docs/specs\n"
        "e2e:\n  env:\n    E2E_BASE_URL: https://stage.app\n")
    gh.pr_files = ["docs/specs/2026-08-01-f-design.md", "docs/plans/2026-08-01-f.md"]
    p = Pipeline(db, settings, gh, sb, tg)
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    await p._prepare(run)
    assert run.e2e_enabled is True
    assert run.run_cmd is None
    assert json.loads(run.e2e_env_json) == {"E2E_BASE_URL": "https://stage.app"}


async def test_prepare_no_e2e_block_disables(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    gh.files[".loop.yml"] = "specs_dir: docs/specs\n"
    gh.pr_files = ["docs/specs/2026-08-01-f-design.md", "docs/plans/2026-08-01-f.md"]
    p = Pipeline(db, settings, gh, sb, tg)
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    await p._prepare(run)
    assert run.e2e_enabled is False


async def test_prepare_uploads_the_upstream_context(db, tmp_path):
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    gh.files["src/api.py"] = "print('real')"
    await dbmod.save_contract(db, "o/backend", 12, run_id=1, pr_number=45,
                              head_sha="abc", contract_md="### POST /v1/x",
                              sources=["src/api.py"], breaking=[])
    await it.upsert_task(db, "o/myrepo", 13, "F", None)
    await it.set_depends_on(db, "o/myrepo", 13,
                            [{"repo": "o/backend", "number": 12}])
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    run.issue_number = 13
    await dbmod.save_run(db, run)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb)
    await pipe._prepare(run)
    written = {p: c for _, p, c in sb.files_written}
    assert written[".loop/context/o/backend/src/api.py"] == "print('real')"
    assert "o/backend#12" in written[".loop/context/README.md"]


async def test_prepare_without_dependencies_writes_no_context(db, tmp_path):
    gh, sb = FakeGitHub(), FakeSandboxd()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb)
    await pipe._prepare(run)
    assert not any(p.startswith(".loop/context/") for _, p, _ in sb.files_written)
