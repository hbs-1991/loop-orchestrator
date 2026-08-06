"""Connect a repository to loop: create the loop:* labels and the webhook.

Usage:
    python scripts/connect_repo.py owner/repo https://loop.example.com/webhooks/github

Reads LOOP_GITHUB_TOKEN and LOOP_GITHUB_WEBHOOK_SECRET from the environment or .env.
"""
import os
import sys

import httpx

LABELS = {
    "loop:run": "1d76db",
    "loop:running": "fbca04",
    "loop:done": "0e8a16",
    "loop:failed": "b60205",
}


def env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.partition("=")[2].strip()
    sys.exit(f"missing {name}")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    repo, hook_url = sys.argv[1], sys.argv[2]
    token = env("LOOP_GITHUB_TOKEN")
    secret = env("LOOP_GITHUB_WEBHOOK_SECRET")
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"})
    for name, color in LABELS.items():
        r = client.post(f"/repos/{repo}/labels", json={"name": name, "color": color})
        print(f"label {name}: {'ok' if r.status_code == 201 else r.status_code}")
    r = client.post(f"/repos/{repo}/hooks", json={
        "config": {"url": hook_url, "secret": secret, "content_type": "json"},
        "events": ["pull_request"],
    })
    print(f"webhook: {r.status_code} {r.json() if r.status_code >= 400 else 'ok'}")


if __name__ == "__main__":
    main()
