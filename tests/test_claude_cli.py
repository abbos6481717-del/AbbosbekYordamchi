"""ClaudeCLI against a fake ``claude`` executable (POSIX only: it relies on a shebang)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    BUILTIN_PLUGINS,
    REDACTED,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="the fake claude executable needs a POSIX shebang"
)

TOKEN = "sk-ant-oat01-" + "A1b2C3d4E5" * 5
PARENT_TOKEN = "sk-ant-oat01-" + "Zz9Yy8Xx7W" * 5

FAKE_CLAUDE = r'''#!{python}
import json, os, subprocess, sys, time

mode = os.environ.get("FAKE_MODE", "ok")
record = os.environ["FAKE_RECORD"]
argv = sys.argv[1:]
stdin = sys.stdin.read()
info = {{
    "argv": argv,
    "stdin": stdin,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "cwd_listing": sorted(os.listdir(".")),
    "cwd_mode": os.stat(".").st_mode & 0o777,
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
}}
if "--system-prompt-file" in argv:
    path = argv[argv.index("--system-prompt-file") + 1]
    info["system_prompt_path"] = path
    info["system_prompt_mode"] = os.stat(path).st_mode & 0o777
    with open(path, encoding="utf-8") as handle:
        info["system_prompt"] = handle.read()
with open(record, "w", encoding="utf-8") as handle:
    json.dump(info, handle)

token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

def result(text, **extra):
    data = {{"type": "result", "subtype": "success", "is_error": False, "result": text}}
    data.update(extra)
    print(json.dumps(data))

if mode == "ok":
    if "stream-json" in argv:
        print(json.dumps({{"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                          "plugins": [], "skills": [], "apiKeySource": "none",
                          "apiProvider": "firstParty", "model": "claude-test"}}))
    result("  Salom, hozir bandman!  \n")
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(record + ".grandchild", "w") as handle:
        handle.write(str(child.pid))
    time.sleep(60)
elif mode == "limit_epoch":
    result("Claude AI usage limit reached|1999999999", is_error=True)
elif mode == "limit_text":
    result("You've hit your limit · resets 3pm (Asia/Tashkent)", is_error=True, api_error_status=429)
elif mode == "auth":
    result("Invalid API key · token " + token + " rejected. Please run /login", is_error=True, api_error_status=401)
elif mode == "crash":
    sys.stderr.write("Segmentation fault\n")
    sys.exit(3)
elif mode == "empty":
    result("   ")
elif mode == "echo_token":
    sys.stderr.write("debug token=" + token + "\n")
    sys.exit(1)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")

    class Fake:
        bin = str(path)
        workdir = tmp_path / "data" / "claude-run"

        def cli(self, **kwargs):
            kwargs.setdefault("timeout", 20)
            return ClaudeCLI(self.bin, workdir=self.workdir, **kwargs)

        def mode(self, value):
            monkeypatch.setenv("FAKE_MODE", value)

        def record(self):
            return json.loads(record.read_text(encoding="utf-8"))

    return Fake()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    except OSError:
        return True


# --------------------------------------------------------------------- basics
async def test_result_is_stripped(fake):
    assert await fake.cli().generate("system", "hello") == "Salom, hozir bandman!"


async def test_message_only_on_stdin_and_profile_only_in_private_file(fake):
    cli = fake.cli()
    await cli.generate("PROFILE: Bobur, Toshkent", "Maxfiy xabar matni")
    rec = fake.record()
    assert rec["stdin"] == "Maxfiy xabar matni"
    joined = " ".join(rec["argv"])
    assert "Maxfiy xabar" not in joined
    assert "Bobur" not in joined
    assert rec["system_prompt"] == "PROFILE: Bobur, Toshkent"
    assert rec["system_prompt_mode"] == 0o600
    assert not Path(rec["system_prompt_path"]).exists()
    assert [p.name for p in fake.workdir.iterdir()] == []


async def test_temp_files_removed_on_error(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError):
        await fake.cli().generate("system", "hi")
    assert list(fake.workdir.iterdir()) == []


async def test_no_shell_is_used(fake, tmp_path):
    marker = tmp_path / "pwned"
    text = f"$(touch {marker}) `touch {marker}`; touch {marker}"
    await fake.cli().generate("system", text)
    assert not marker.exists()
    assert fake.record()["stdin"] == text


async def test_isolation_flags_in_order(fake):
    await fake.cli(effort="medium").generate("system", "hi")
    argv = fake.record()["argv"]
    prompt_file = argv[argv.index("--system-prompt-file") + 1]
    settings = argv[argv.index("--settings") + 1]
    assert argv == [
        "-p",
        "--safe-mode",
        "--settings", settings,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file", prompt_file,
        "--output-format", "json",
        "--effort", "medium",
    ]
    assert "--mcp-config" not in argv and "--bare" not in argv
    parsed = json.loads(settings)
    assert parsed["disableAllHooks"] is True
    assert parsed["enabledPlugins"] == {name: False for name in BUILTIN_PLUGINS}


async def test_model_and_stream_json_flags(fake):
    await fake.cli(model="claude-sonnet-test").inspect()
    argv = fake.record()["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-test"


# ------------------------------------------------------------------ environment
async def test_child_environment_is_scrubbed(fake, monkeypatch):
    for key, value in {
        "ANTHROPIC_API_KEY": "sk-ant-api03-secret",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "TELEGRAM_BOT_TOKEN": "1:secret",
        "CLAUDE_CODE_OAUTH_TOKEN": PARENT_TOKEN,
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }.items():
        monkeypatch.setenv(key, value)
    await fake.cli().generate("system", "hi")
    env = fake.record()["env"]
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    for key in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert key not in env
    assert env["HOME"] == os.environ["HOME"]
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_only_reaches_child_env(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PARENT_TOKEN)
    cli = fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env")
    await cli.generate("system prompt", "user prompt")
    rec = fake.record()
    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(rec["argv"])
    assert TOKEN not in rec["stdin"] and TOKEN not in rec["system_prompt"]
    assert TOKEN not in repr(cli)


async def test_no_token_in_child_when_not_configured(fake):
    await fake.cli().generate("system", "hi")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in fake.record()["env"]


async def test_token_redacted_in_errors(fake):
    fake.mode("echo_token")
    with pytest.raises(ClaudeError) as info:
        await fake.cli(oauth_token=TOKEN).generate("system", "hi")
    assert TOKEN not in str(info.value)
    assert REDACTED in str(info.value)


async def test_rejected_token_message(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli(oauth_token=TOKEN).generate("system", "hi")
    message = str(info.value)
    assert "claude setup-token" in message
    assert "CLI said:" in message and REDACTED in message
    assert TOKEN not in message and TOKEN not in info.value.detail


async def test_inspect_reports_auth_and_init(fake):
    cli = fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env")
    info = await cli.inspect()
    assert info["auth"] == "subscription token from .env"
    assert info["init"]["plugins"] == [] and info["init"]["tools"] == []
    assert info["reply"] == "Salom, hozir bandman!"
    assert fake.record()["stdin"] == "ping"
    assert (await fake.cli().inspect())["auth"] == "this machine's claude login"


async def test_runs_in_its_own_empty_private_dir(fake):
    await fake.cli().generate("system", "hi")
    rec = fake.record()
    assert rec["cwd_listing"] == []
    assert rec["cwd_mode"] == 0o700
    assert Path(rec["cwd"]).parent == fake.workdir.resolve()
    assert fake.workdir.stat().st_mode & 0o777 == 0o700
    assert rec["pgid"] == rec["pid"]  # own session / process group


# ------------------------------------------------------------------- failures
async def test_timeout_kills_process_group(fake, tmp_path):
    fake.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake.cli(timeout=1).generate("system", "hi")
    assert time.monotonic() - started < 5
    grandchild = int((tmp_path / "record.json.grandchild").read_text())
    child = fake.record()["pid"]
    deadline = time.monotonic() + 3
    while (_alive(grandchild) or _alive(child)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)
    assert not _alive(child)


async def test_limit_with_epoch(fake):
    fake.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("system", "hi")
    assert info.value.resets_at == 1999999999
    assert "|" not in info.value.detail
    assert str(info.value).startswith("Claude subscription usage limit reached")


async def test_limit_with_text(fake):
    fake.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("system", "hi")
    assert info.value.resets_at is None
    assert info.value.reset_text == "resets 3pm (Asia/Tashkent)"
    assert str(info.value) == "Claude subscription usage limit reached (resets 3pm (Asia/Tashkent))"


async def test_login_error_without_token(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli().generate("system", "hi")
    assert "/login" in str(info.value)


async def test_crash(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError) as info:
        await fake.cli().generate("system", "hi")
    assert "Segmentation fault" in str(info.value)
    assert not isinstance(info.value, (ClaudeAuthError, ClaudeLimitError))


async def test_empty_reply(fake):
    fake.mode("empty")
    with pytest.raises(ClaudeError, match="empty"):
        await fake.cli().generate("system", "hi")


async def test_missing_binary(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "nope" / "claude"), workdir=tmp_path / "w")
    with pytest.raises(ClaudeError, match="not found"):
        await cli.generate("system", "hi")


# ------------------------------------------------------- pure parsing helpers
def test_classify_failure():
    limit = classify_failure("Claude AI usage limit reached|1760000000")
    assert isinstance(limit, ClaudeLimitError) and limit.resets_at == 1760000000
    assert limit.detail == "Claude AI usage limit reached"
    assert isinstance(classify_failure("whatever", 429), ClaudeLimitError)
    assert isinstance(classify_failure("Rate-limit exceeded"), ClaudeLimitError)
    assert isinstance(classify_failure("You are out of extra usage"), ClaudeLimitError)
    assert isinstance(classify_failure("monthly QUOTA"), ClaudeLimitError)

    auth = classify_failure("Not logged in · Please run /login")
    assert isinstance(auth, ClaudeAuthError) and "/login" in str(auth)
    assert isinstance(classify_failure("x", 401), ClaudeAuthError)
    assert isinstance(classify_failure("x", 403), ClaudeAuthError)
    assert isinstance(classify_failure("OAuth token has expired"), ClaudeAuthError)
    assert len(classify_failure("Not logged in " + "x" * 1000).detail) <= 300

    other = classify_failure("boom " * 300)
    assert type(other) is ClaudeError
    assert len(str(other)) < 600


def test_parse_result():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi "})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"
    err = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Usage limit reached|1760000000"})
    assert isinstance(_raised(lambda: parse_result(1, err, "")), ClaudeLimitError)
    max_turns = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False, "result": "x"})
    assert type(_raised(lambda: parse_result(1, max_turns, ""))) is ClaudeError
    empty = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": ""})
    assert "empty" in str(_raised(lambda: parse_result(0, empty, "")))
    assert "Not logged in" in str(_raised(lambda: parse_result(1, "garbage", "Not logged in")))
    assert "exit code 7" in str(_raised(lambda: parse_result(7, "", "")))


def _raised(fn):
    try:
        fn()
    except ClaudeError as exc:
        return exc
    raise AssertionError("no ClaudeError raised")
