from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOP_", env_file=".env", extra="ignore")

    github_token: str
    github_webhook_secret: str
    telegram_bot_token: str
    telegram_chat_id: int
    sandboxd_url: str = "http://127.0.0.1:9090"
    sandboxd_api_key: str
    git_credential_id: str
    db_path: str = "data/loop.db"
    secrets_dir: str = "secrets"
    max_concurrent_runs: int = 4
    default_timeout_minutes: int = 180
    poll_interval_seconds: int = 20
    rate_limit_retry_minutes: int = 60
    # In-place resumes of a stage task after a transient agent API failure
    # (stream stalled mid-response, dropped connection). The session survives
    # in the sandbox, so every resume continues the work instead of restarting
    # it — which is why the count is generous: during the 2026-08-05 provider
    # incident runs #34/#35 died only because 2 attempts ran out faster than
    # the API recovered, discarding real progress each time. 10 matches
    # Claude Code's own default API retry budget.
    agent_retry_attempts: int = 10
    # Stalls cluster during provider incidents (observed ~5 min apart); an
    # immediate resume jumps straight back into the same outage window.
    agent_retry_backoff_seconds: int = 120
    reviewer_model: str = "claude-fable-5"
    review_timeout_minutes: int = 30
    review_max_fix_iterations: int = 2
    e2e_max_fix_iterations: int = 2
    e2e_model: str = ""  # empty = the executor's default model
    planner_model: str = ""  # empty = the executor's default model
    advisor_model: str = "claude-fable-5"
    plan_max_iterations: int = 3
    tz: str = "UTC"  # IANA zone for progress-card timestamps
    telegram_admin_ids: str = ""  # CSV of Telegram user ids allowed to press buttons
    telegram_webhook_secret: str = ""  # X-Telegram-Bot-Api-Secret-Token value
    preview_ttl_minutes: int = 120  # sandbox lifetime while awaiting approval
    # Window each keepalive buys against sandboxd's idle reaper. Refreshed on
    # every poll tick, so it only has to outlast a poll gap or a short
    # orchestrator restart — and it caps how long an abandoned sandbox lingers.
    keepalive_minutes: int = 30
    # Label the "Merge & Deploy" button puts on the PR right before merging;
    # the repo's promote workflow reacts to a merged PR carrying it.
    promote_label: str = "promote:staging"
    public_url: str = ""  # external base URL of the orchestrator (for setWebhook)
    backlog_poll_minutes: int = 5
    backlog_repos: str = ""  # CSV of owner/repo polled even before first webhook

    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_admin_ids.replace(" ", "").split(",") if x}

    def backlog_repo_list(self) -> list[str]:
        return [r for r in self.backlog_repos.replace(" ", "").split(",") if r]
