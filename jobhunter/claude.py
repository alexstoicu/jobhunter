"""Headless Claude calls on the Pro subscription (`claude -p`), with usage logged per call.

Calls run without tools or MCP servers, with a custom system prompt, from a neutral
folder (so no CLAUDE.md is loaded): only the prompt itself costs tokens.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from jobhunter import db

WORKDIR = Path(tempfile.gettempdir()) / "jobhunter-claude"


class ClaudeError(RuntimeError):
    pass


def run(conn, step, system, prompt, schema, model="sonnet", jobs=1, timeout=600):
    """Send one prompt; return the structured output (a dict matching `schema`)."""
    WORKDIR.mkdir(exist_ok=True)
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    cmd = [exe, "-p", "--output-format", "json", "--model", model, "--tools", "",
           "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          cwd=WORKDIR, timeout=timeout, env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "jobhunter"})
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _log(conn, step, model, jobs, {}, ok=False)
        raise ClaudeError(f"claude exited {proc.returncode}: {proc.stderr[-500:] or proc.stdout[-500:]}")

    ok = not out.get("is_error") and out.get("structured_output") is not None
    _log(conn, step, model, jobs, out, ok)
    if not ok:
        raise ClaudeError(f"claude returned no structured output: {str(out.get('result'))[:500]}")
    return out["structured_output"]


def _log(conn, step, model, jobs, out, ok):
    usage = out.get("usage", {})
    models = list(out.get("modelUsage", {}).keys())
    conn.execute(
        """INSERT INTO usage (at, step, model, jobs, input_tokens, cache_write, cache_read, output_tokens,
                              cost_usd, duration_ms, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (db.now(), step, ",".join(models) or model, jobs, usage.get("input_tokens"),
         usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens"),
         usage.get("output_tokens"), out.get("total_cost_usd"), out.get("duration_ms"), int(ok)))
    conn.commit()
