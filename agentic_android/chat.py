"""Interactive chat that drives the device through a spawned `claude` agent.

Mirrors the cfx-open-source-tracker pattern: spawn headless
`claude -p --input-format stream-json --output-format stream-json` with the
Android MCP server attached, then relay the terminal to/from the agent. stdin
stays open, so you can type at any time — including mid-task — to steer it.

The brain is your logged-in `claude` CLI (your Claude subscription); no
ANTHROPIC_API_KEY is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

from .config import persistence_block

# Project root (parent of this package). The agent is spawned with this as its
# working directory so its Claude Code session history is scoped to the project
# and doesn't mix with history from whatever directory you launched from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APPEND_SYSTEM_PROMPT = """\
You are Agentic Android, operating a real Android device via MCP tools from the \
`agentic_android` server: screenshot, tap_element, tap, swipe, type_text, \
press_key, launch_app, list_apps, dump_ui. If those tools are not already \
loaded, use ToolSearch to find them (search "agentic_android"). You have no other \
way to touch the device — do not use Bash/adb directly.

Working rules:
- tap, swipe, type_text, press_key and launch_app each return the resulting \
screen as an image, so you always have the current screen. When the user asks \
what's on screen or to read/analyse it, just look at the screen you already \
have and answer directly — do NOT call screenshot merely to "see" it. Use \
screenshot only to refresh after a change you couldn't observe otherwise.
- Coordinates are pixels of the screenshot image (top-left origin); tap the \
visual centre of a target. To scroll down, swipe up (large y to small y).
- Tap a text field before type_text; submit with press_key ENTER or the on-screen button.
- To tap something dump_ui lists, prefer tap_element (by text or #index) over \
guessing raw coordinates. For "the Nth item", count the matching rows in order \
and tap the right one; scroll if it isn't visible.
- Don't guess package names for launch_app — call list_apps (optionally with a \
filter like 'clock') to find the real one; if nothing matches, it isn't installed.
- Play Store package is com.android.vending. To install an app: open Play Store, \
tap Search, type the name, open the correct result, tap Install, wait for it to finish.
- press_key BACK to dismiss dialogs; HOME for the launcher.

This is a live chat. Briefly tell the user what you're doing and what you see. \
Act autonomously on navigation/reading, but DO NOT commit or send anything \
unless the user explicitly told you to: sending a message/DM, posting, liking, \
following, buying/installing, deleting, or signing in. If asked to "suggest" or \
"draft" a reply/post, just give the text — do not type or send it. When unsure, \
ask first. The user may send new messages while you work — treat them as \
steering and adjust immediately.\
"""

# Added to the system prompt only when confirm_destructive is on.
_CONFIRM_PROTOCOL = (
    "Destructive-action guard: before any irreversible or committing action "
    "(uninstalling, deleting, buying/paying, placing an order, factory reset, or "
    "signing out), STOP and ask the user in one short message that states the exact "
    "action and target, and wait for an explicit 'yes' before doing it. The device "
    "tools also block these and return a 'BLOCKED' message until you confirm; when "
    "that happens, ask the user, then retry the same tool with confirm=true."
)


def _mcp_config(serial: str, adb_path: str, max_long_edge: int, *,
                blank_png_bytes: int = 20000, auto_ui_fallback: bool = True,
                wait_idle: bool = True, settle_timeout: float = 4.0,
                adb_retries: int = 2, confirm_destructive: bool = False,
                destructive_keywords: list | None = None) -> str:
    env = {
        "ANDROID_SERIAL": serial or "",
        "AGENTIC_ANDROID_ADB": adb_path,
        "AGENTIC_ANDROID_MAX_LONG_EDGE": str(max_long_edge),
        # "Reliable Runs" knobs (mcp_server._dev reads these)
        "AGENTIC_ANDROID_BLANK_PNG_BYTES": str(blank_png_bytes),
        "AGENTIC_ANDROID_AUTO_UI_FALLBACK": "1" if auto_ui_fallback else "0",
        "AGENTIC_ANDROID_WAIT_IDLE": "1" if wait_idle else "0",
        "AGENTIC_ANDROID_SETTLE_TIMEOUT": str(settle_timeout),
        "AGENTIC_ANDROID_ADB_RETRIES": str(adb_retries),
        "AGENTIC_ANDROID_CONFIRM_DESTRUCTIVE": "1" if confirm_destructive else "0",
    }
    if destructive_keywords:
        env["AGENTIC_ANDROID_DESTRUCTIVE_KEYWORDS"] = ",".join(destructive_keywords)
    cfg = {
        "mcpServers": {
            "agentic_android": {
                "command": sys.executable,
                "args": ["-m", "agentic_android.mcp_server"],
                "env": env,
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="agentic_android-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def _compact(d: dict) -> str:
    s = json.dumps(d, separators=(",", ":"))
    return s if len(s) <= 80 else s[:77] + "…"


def _reader(proc: subprocess.Popen, debug_path: str | None = None) -> None:
    """Parse the agent's stream-json stdout and print it readably."""
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        if debug_path:
            with open(debug_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            sid = str(ev.get("session_id", ""))[:8]
            print(f"\n[agent ready · session {sid} · model {ev.get('model', '?')}]")
            print("Type a task (e.g. 'Install WhatsApp on my phone'). "
                  "You can keep typing to steer it mid-task. Ctrl-C or 'exit' to quit.\n")
        elif t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    print(f"\nagent> {b['text'].strip()}")
                elif b.get("type") == "tool_use":
                    name = b.get("name", "").split("__")[-1]
                    print(f"   · {name} {_compact(b.get('input', {}))}")
        elif t == "result":
            cost = ev.get("total_cost_usd")
            tail = f" · ${cost:.3f}" if isinstance(cost, (int, float)) else ""
            print(f"[turn complete · {ev.get('subtype', 'ok')}{tail}]\n")
    # stdout closed → agent exited
    print("\n[agent process ended]")


def _stderr(proc: subprocess.Popen) -> None:
    for line in proc.stderr:
        if line.strip():
            print(f"[claude] {line.rstrip()}", file=sys.stderr)


def run_chat(serial: str, adb_path: str, model: str = "sonnet",
             budget: float | None = None, max_long_edge: int = 1568,
             effort: int = 3, debug_path: str | None = None, *,
             blank_png_bytes: int = 20000, auto_ui_fallback: bool = True,
             wait_idle: bool = True, settle_timeout: float = 4.0, adb_retries: int = 2,
             confirm_destructive: bool = False, destructive_keywords: list | None = None) -> int:
    try:  # flush each line as it's printed (so piped/redirected output stays live)
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    claude = shutil.which("claude")
    if not claude:
        print("error: `claude` CLI not found on PATH. Install Claude Code and log in "
              "(`claude` once, interactively).", file=sys.stderr)
        return 2

    system_prompt = APPEND_SYSTEM_PROMPT + "\n\nPersistence & asking:\n" + persistence_block(effort, "chat")
    if confirm_destructive:
        system_prompt += "\n\n" + _CONFIRM_PROTOCOL

    mcp_cfg = _mcp_config(serial, adb_path, max_long_edge,
                          blank_png_bytes=blank_png_bytes, auto_ui_fallback=auto_ui_fallback,
                          wait_idle=wait_idle, settle_timeout=settle_timeout,
                          adb_retries=adb_retries, confirm_destructive=confirm_destructive,
                          destructive_keywords=destructive_keywords)
    args = [
        claude, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "ToolSearch,mcp__agentic_android",
        "--mcp-config", mcp_cfg,
        "--strict-mcp-config",
        "--append-system-prompt", system_prompt,
        "--model", model,
    ]
    if budget:
        args += ["--max-budget-usd", str(budget)]

    print(f"Spawning agent: claude {model} · device {serial} · effort {effort}/5 · "
          f"cwd {PROJECT_ROOT} · {'$%.2f budget' % budget if budget else 'no budget cap'}")
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, start_new_session=True, cwd=PROJECT_ROOT,
    )

    if debug_path:
        print(f"[debug] saving session to {debug_path}")
    threading.Thread(target=_reader, args=(proc, debug_path), daemon=True).start()
    threading.Thread(target=_stderr, args=(proc,), daemon=True).start()

    rc = 0
    try:
        while proc.poll() is None:
            try:
                line = input()
            except EOFError:
                break
            if line.strip().lower() in {"exit", "quit", ":q"}:
                break
            if not line.strip():
                continue
            frame = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": line}]},
            })
            try:
                proc.stdin.write(frame + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                print("[agent stdin closed]", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("\n[exiting]")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.unlink(mcp_cfg)
        except OSError:
            pass
    return rc
