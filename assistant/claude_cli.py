"""Run the Claude Code CLI (``claude -p``) as an isolated, tool-less text generator.

Every reply is one ``claude -p`` run:

* no shell is ever used (``asyncio.create_subprocess_exec``);
* the conversation goes only through stdin, never argv;
* the system prompt (with the profile) is written to a 0600 temp file and
  passed with ``--system-prompt-file`` so it never shows up in ``ps``;
* tools, MCP servers, hooks, plugins, skills, slash commands, CLAUDE.md and
  auto-memory are all disabled;
* the child environment is scrubbed of API keys and the bot token;
* ``--bare`` is NOT used: it ignores the OAuth login, so the subscription
  would stop working.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

IS_WINDOWS = sys.platform.startswith("win")

# Built-in plugins of the Claude Code CLI version pinned in the Dockerfile
# (2.1.325, read from the stream-json init event). All of them are disabled.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-privacy@builtin",
    "cc-plugin-plugin-authoring@builtin",
)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

REMOVED_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_API_HASH",
    "CRM_PASSWORD",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Set by --bare; it turns the OAuth login off.
    "CLAUDE_CODE_SIMPLE",
)

REDACTED = "[REDACTED]"
_TOKEN_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9]+-[A-Za-z0-9_\-]{8,}")

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|"
    r"out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|(\d{9,11})\b")
_RESETS_RE = re.compile(r"(resets?\s+[^.|\n]*)", re.IGNORECASE)


class ClaudeError(Exception):
    """Base error. The message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(
        self,
        detail: str = "",
        resets_at: Optional[float] = None,
        reset_text: Optional[str] = None,
    ) -> None:
        when = reset_text or "later"
        super().__init__(f"Claude subscription usage limit reached ({when})")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI failure text into the matching exception (returned, not raised)."""
    text = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(text):
        resets_at: Optional[float] = None
        match = _EPOCH_RE.search(text)
        if match:
            resets_at = float(match.group(1))
        detail = _EPOCH_RE.sub("", text).strip()
        reset_text: Optional[str] = None
        match = _RESETS_RE.search(detail)
        if match:
            reset_text = match.group(1).strip()
        return ClaudeLimitError(detail=detail, resets_at=resets_at, reset_text=reset_text)
    if status in (401, 403) or _AUTH_RE.search(text):
        detail = text[:300]
        return ClaudeAuthError(
            "Claude CLI is not logged in: run `claude` and use /login "
            f"(or set CLAUDE_CODE_OAUTH_TOKEN). CLI said: {detail}",
            detail,
        )
    return ClaudeError(f"Claude CLI failed: {text[:500] or 'no output'}")


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """Parse ``--output-format json`` / ``stream-json`` output into the reply text."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    data: Any = None
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        fallback = (stderr or "").strip() or (stdout or "").strip() or f"exit code {returncode}"
        raise classify_failure(fallback)

    result = data.get("result")
    result_text = result if isinstance(result, str) else ""
    if data.get("is_error") or data.get("subtype") != "success":
        status = data.get("api_error_status")
        status_int = status if isinstance(status, int) else None
        message = "\n".join(part for part in (result_text, (stderr or "").strip()) if part)
        if not message:
            message = f"{data.get('subtype') or 'error'} (exit code {returncode})"
        raise classify_failure(message, status_int)
    text = result_text.strip()
    if not text:
        raise ClaudeError("Claude CLI returned an empty reply")
    return text


def build_env(oauth_token: Optional[str] = None, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Environment for the child process: scrubbed copy of ``os.environ``."""
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith("ANTHROPIC_") and key not in REMOVED_ENV_KEYS
    }
    env.update(ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def _kill_process_tree(proc: "asyncio.subprocess.Process") -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Path,
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token or None
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, model={self.model!r}, "
            f"effort={self.effort!r}, timeout={self.timeout!r}, auth={self.auth_mode!r})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    def build_command(
        self, system_prompt_file: str, output_format: str = "json", input_format: Optional[str] = None
    ) -> List[str]:
        cmd = [
            self.claude_bin,
            "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]
        if input_format:
            cmd.extend(["--input-format", input_format])
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.model:
            cmd.extend(["--model", self.model])
        return cmd

    def _redact(self, text: str) -> str:
        if self._oauth_token:
            text = text.replace(self._oauth_token, REDACTED)
        return _TOKEN_PATTERN.sub(REDACTED, text)

    def _resolve_bin(self) -> str:
        path = self.claude_bin
        found = shutil.which(path) if not os.path.isabs(path) else (path if os.path.isfile(path) else None)
        if not found:
            raise ClaudeError(
                f"Claude CLI not found at {path!r}. Install Claude Code or set CLAUDE_BIN."
            )
        if IS_WINDOWS and found.lower().endswith((".cmd", ".bat")):
            raise ClaudeError(
                f"{found} is a batch shim, which would run through cmd.exe. Install the "
                "native Claude Code (claude.exe) or point CLAUDE_BIN at claude.exe."
            )
        return found

    async def _run(
        self,
        system_prompt: str,
        stdin_text: str,
        output_format: str,
        input_format: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, str, str]:
        timeout = timeout or self.timeout
        claude_bin = self._resolve_bin()
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not IS_WINDOWS:
            os.chmod(self.workdir, 0o700)

        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=str(self.workdir))
        run_dir: Optional[str] = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            if not IS_WINDOWS:
                os.chmod(prompt_path, 0o600)
            # An empty private directory as cwd for each run.
            run_dir = tempfile.mkdtemp(prefix="run-", dir=str(self.workdir))

            cmd = self.build_command(prompt_path, output_format, input_format)
            cmd[0] = claude_bin
            kwargs: Dict[str, Any] = {}
            if not IS_WINDOWS:
                kwargs["start_new_session"] = True
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=run_dir,
                    env=build_env(self._oauth_token),
                    **kwargs,
                )
            except FileNotFoundError:
                raise ClaudeError(
                    f"Claude CLI not found at {claude_bin!r}. Install Claude Code or set CLAUDE_BIN."
                ) from None

            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")), timeout=timeout
                )
            except asyncio.TimeoutError:
                _kill_process_tree(proc)
                await proc.wait()
                raise ClaudeTimeout(f"Claude CLI did not answer within {timeout} seconds") from None
            except asyncio.CancelledError:
                _kill_process_tree(proc)
                raise

            stdout = self._redact(out.decode("utf-8", errors="replace"))
            stderr = self._redact(err.decode("utf-8", errors="replace"))
            return proc.returncode if proc.returncode is not None else -1, stdout, stderr
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
            if run_dir:
                shutil.rmtree(run_dir, ignore_errors=True)

    def _wrap_auth(self, exc: ClaudeError) -> ClaudeError:
        if isinstance(exc, ClaudeAuthError) and self._oauth_token:
            detail = self._redact(exc.detail)
            return ClaudeAuthError(
                "Claude rejected the subscription token or it has expired: run "
                "`claude setup-token` on a computer with a browser and update "
                f"CLAUDE_CODE_OAUTH_TOKEN. CLI said: {detail}",
                detail,
            )
        return exc

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeError as exc:
            raise self._wrap_auth(exc) from None

    async def generate_with_images(
        self,
        system_prompt: str,
        text: str,
        images: Sequence[Tuple[bytes, str]],
        *,
        timeout: Optional[float] = None,
    ) -> str:
        """Like ``generate`` but the user message also carries images ((data, media type) pairs).

        Tools stay disabled: the images go in as content blocks through ``--input-format
        stream-json``, so Claude only looks at them and writes text back.
        """
        content: List[Dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
            }
            for data, media_type in images
        ]
        content.append({"type": "text", "text": text})
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
        returncode, stdout, stderr = await self._run(
            system_prompt, line + "\n", "stream-json", input_format="stream-json", timeout=timeout
        )
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeError as exc:
            raise self._wrap_auth(exc) from None

    async def inspect(self) -> Dict[str, Any]:
        """Tiny request that reports what the CLI loaded and which auth it used."""
        returncode, stdout, stderr = await self._run("Reply with exactly: OK", "ping", "stream-json")
        init: Optional[Dict[str, Any]] = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {
                    key: event.get(key)
                    for key in ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")
                }
                break
        try:
            reply = parse_result(returncode, stdout, stderr)
        except ClaudeError as exc:
            raise self._wrap_auth(exc) from None
        return {"auth": self.auth_mode, "init": init, "reply": reply}
