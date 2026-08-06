from loop_orchestrator.secrets import (
    SECRETS_FILE,
    load_repo_secrets,
    render_env_file,
    source_hint,
)


def test_load(tmp_path):
    f = tmp_path / "owner__repo.env"
    f.write_text("# comment\nDATABASE_URL=postgres://x\n\nAPI_KEY = secret \nBROKEN LINE\n")
    got = load_repo_secrets(str(tmp_path), "owner/repo")
    assert got == {"DATABASE_URL": "postgres://x", "API_KEY": "secret"}


def test_missing_file(tmp_path):
    assert load_repo_secrets(str(tmp_path), "no/such") == {}


def test_render_env_file_quotes_awkward_values():
    out = render_env_file({"PW": "a b'c$d", "PLAIN": "x"})
    assert out.endswith("\n") and out.count("\n") == 2
    assert "PLAIN=x\n" in out
    # `set -a; . file` must reproduce the value byte for byte
    import shlex
    line = next(ln for ln in out.splitlines() if ln.startswith("PW="))
    assert shlex.split(line)[0] == "PW=a b'c$d"


def test_source_hint_names_keys_but_never_values():
    hint = source_hint({"E2E_USER_PASSWORD": "hunter2"})
    assert "`E2E_USER_PASSWORD`" in hint and SECRETS_FILE in hint
    assert "hunter2" not in hint
    assert source_hint({}) == ""
