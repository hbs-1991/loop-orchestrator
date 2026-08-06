from loop_orchestrator.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        github_token="ghp_x",
        github_webhook_secret="whs",
        telegram_bot_token="123:abc",
        telegram_chat_id=42,
        sandboxd_api_key="sbk",
        git_credential_id="cred1",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_defaults():
    s = _settings()
    assert s.sandboxd_url == "http://127.0.0.1:9090"
    assert s.max_concurrent_runs == 4
    assert s.default_timeout_minutes == 180
    assert s.poll_interval_seconds == 20
    assert s.rate_limit_retry_minutes == 60
    assert s.db_path == "data/loop.db"
    assert s.secrets_dir == "secrets"


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("LOOP_GITHUB_TOKEN", "from-env")
    for k, v in {
        "LOOP_GITHUB_WEBHOOK_SECRET": "s", "LOOP_TELEGRAM_BOT_TOKEN": "t",
        "LOOP_TELEGRAM_CHAT_ID": "1", "LOOP_SANDBOXD_API_KEY": "k",
        "LOOP_GIT_CREDENTIAL_ID": "c",
    }.items():
        monkeypatch.setenv(k, v)
    assert Settings(_env_file=None).github_token == "from-env"


def test_review_settings_defaults(monkeypatch, tmp_path):
    for k, v in {
        "LOOP_GITHUB_TOKEN": "t", "LOOP_GITHUB_WEBHOOK_SECRET": "s",
        "LOOP_TELEGRAM_BOT_TOKEN": "b", "LOOP_TELEGRAM_CHAT_ID": "1",
        "LOOP_SANDBOXD_API_KEY": "k", "LOOP_GIT_CREDENTIAL_ID": "c",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.reviewer_model == "claude-fable-5"
    assert s.review_timeout_minutes == 30
    assert s.review_max_fix_iterations == 2


def test_e2e_settings_defaults(monkeypatch):
    for key, val in (("LOOP_GITHUB_TOKEN", "t"), ("LOOP_GITHUB_WEBHOOK_SECRET", "s"),
                     ("LOOP_TELEGRAM_BOT_TOKEN", "b"), ("LOOP_TELEGRAM_CHAT_ID", "1"),
                     ("LOOP_SANDBOXD_API_KEY", "k"), ("LOOP_GIT_CREDENTIAL_ID", "c")):
        monkeypatch.setenv(key, val)
    s = Settings(_env_file=None)
    assert s.e2e_max_fix_iterations == 2
    assert s.e2e_model == ""


def test_tz_default(monkeypatch):
    for key, val in (("LOOP_GITHUB_TOKEN", "t"), ("LOOP_GITHUB_WEBHOOK_SECRET", "s"),
                     ("LOOP_TELEGRAM_BOT_TOKEN", "b"), ("LOOP_TELEGRAM_CHAT_ID", "1"),
                     ("LOOP_SANDBOXD_API_KEY", "k"), ("LOOP_GIT_CREDENTIAL_ID", "c")):
        monkeypatch.setenv(key, val)
    s = Settings(_env_file=None)
    assert s.tz == "UTC"
    monkeypatch.setenv("LOOP_TZ", "Asia/Almaty")
    assert Settings(_env_file=None).tz == "Asia/Almaty"


def test_backlog_settings_defaults(monkeypatch):
    for key in ("LOOP_GITHUB_TOKEN", "LOOP_GITHUB_WEBHOOK_SECRET",
                "LOOP_TELEGRAM_BOT_TOKEN", "LOOP_SANDBOXD_API_KEY",
                "LOOP_GIT_CREDENTIAL_ID"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("LOOP_TELEGRAM_CHAT_ID", "1")
    s = Settings(_env_file=None)
    assert s.planner_model == ""
    assert s.advisor_model == "claude-fable-5"
    assert s.plan_max_iterations == 3
    assert s.backlog_poll_minutes == 5
    assert s.backlog_repo_list() == []
    monkeypatch.setenv("LOOP_BACKLOG_REPOS", "o/r, o/r2")
    assert Settings(_env_file=None).backlog_repo_list() == ["o/r", "o/r2"]


def test_admin_ids_parsing(monkeypatch):
    for key in ("LOOP_GITHUB_TOKEN", "LOOP_GITHUB_WEBHOOK_SECRET",
                "LOOP_TELEGRAM_BOT_TOKEN", "LOOP_SANDBOXD_API_KEY",
                "LOOP_GIT_CREDENTIAL_ID"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("LOOP_TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("LOOP_TELEGRAM_ADMIN_IDS", "123, 456")
    s = Settings(_env_file=None)
    assert s.admin_ids() == {123, 456}
    assert s.preview_ttl_minutes == 120
    assert s.telegram_webhook_secret == ""
    assert s.public_url == ""
    monkeypatch.setenv("LOOP_TELEGRAM_ADMIN_IDS", "")
    assert Settings(_env_file=None).admin_ids() == set()
