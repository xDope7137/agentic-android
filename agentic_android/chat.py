"""Interactive chat that drives the device through a spawned `claude` agent.

Mirrors the cfx-open-source-tracker pattern: spawn headless
`claude -p --input-format stream-json --output-format stream-json` with the
Android MCP server attached, then relay the terminal to/from the agent. stdin
stays open, so you can type at any time — including mid-task — to steer it.

The brain is your logged-in `claude` CLI (your Claude subscription); no
ANTHROPIC_API_KEY is needed.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .config import persistence_block

# Project root (parent of this package). The agent is spawned with this as its
# working directory so its Claude Code session history is scoped to the project
# and doesn't mix with history from whatever directory you launched from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APPEND_SYSTEM_PROMPT = """\
You are Agentic Android, operating a real Android device via MCP tools from the \
`agentic_android` server: screenshot, tap_element, tap, swipe, type_text, \
press_key, launch_app, list_apps, dump_ui, create_trigger, list_triggers, \
delete_trigger. If those tools are not already \
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

Notification triggers (wake-on-notification): on the user's FIRST instruction, \
judge whether it's a recurring, REACTIVE request — i.e. "when a notification \
about X arrives, do Y" (e.g. "reply to WhatsApp messages from my boss", "when my \
food delivery arrives mark it received"). A one-off like "open settings" is NOT \
reactive — ignore all of this for those. If it IS reactive: briefly say you can \
automate it as a notification trigger, then ask ONE short question to confirm \
(which app/sender, and that they want it automated). Use list_apps to resolve the \
real package name. Only after the user says yes, call create_trigger(..., \
enabled=True), binding that app's notifications (narrow with title_contains / \
text_contains when there's a specific sender/keyword) to the task; then handle any \
matching notification that's already on screen. While this chat stays open, a \
watcher polls notifications and will feed you "[auto-trigger ...]" messages to act \
on. Use list_triggers to show what's set and delete_trigger to remove one. \
Save triggers DISABLED (a draft) if the user is unsure.

This is a live chat. Briefly tell the user what you're doing and what you see. \
Act autonomously on navigation/reading, but DO NOT commit or send anything \
unless the user explicitly told you to: sending a message/DM, posting, liking, \
following, buying/installing, deleting, or signing in. If asked to "suggest" or \
"draft" a reply/post, just give the text — do not type or send it. When unsure, \
ask first. The user may send new messages while you work — treat them as \
steering and adjust immediately.\
"""

# Always appended — tells the agent how to use its persistent personal memory.
_MEMORY_BLOCK = """\
Personal memory — a folder at {dir} persists across sessions and is private to this \
user. You're allowed the Read/Write/Edit/Glob file tools; use them to make it useful:
- At the START of a session, list and read any files there (Glob {dir}/*) to recall what \
you already know about this user: their preferences, the people/names they mention, their \
apps and accounts, routines, and how they like things done. Use it silently — don't \
recite it unless relevant.
- When you learn something DURABLE and useful during a task (e.g. "default browser is \
Firefox", "usual order is a flat white", a friend they message often), save or update a \
short markdown note there — one topic per file (preferences.md, contacts.md, …). Keep \
notes concise and factual. NEVER store secrets (passwords, OTP/2FA codes, card numbers).
- Only ever write inside that memory folder; never modify the program's own files."""

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
                destructive_keywords: list | None = None,
                forbid: list | None = None, stay_in_app: str | None = None) -> str:
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
    if forbid:
        env["AGENTIC_ANDROID_FORBID"] = ",".join(forbid)
    if stay_in_app:
        env["AGENTIC_ANDROID_STAY_IN_APP"] = stay_in_app
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


def _reader(proc: subprocess.Popen, renderer, turn_active=None,
            debug_path: str | None = None) -> None:
    """Parse the agent's stream-json stdout and render it (styled + inline screens).
    `turn_active` (a threading.Event) is cleared on each turn result so the input
    loop knows when a turn is in flight (and must interrupt to steer it)."""
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
            renderer.status(model=ev.get("model", "?"))
            renderer.banner(f"\n[agent ready · session {sid} · model {ev.get('model', '?')}]")
            renderer.banner("Type a task (e.g. 'Install WhatsApp on my phone'). "
                            "You can keep typing to steer it mid-task. Ctrl-C or 'exit' to quit.\n")
        elif t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    renderer.agent_text(b["text"].strip())
                elif b.get("type") == "tool_use":
                    name = b.get("name", "").split("__")[-1]
                    renderer.tool_start(name, b.get("input", {}))
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":  # replayed user message (your typed steer)
                    txt = (b.get("text") or "").strip()
                    if txt.startswith("[Request interrupted"):
                        renderer.info("  (interrupted the previous step)")
                    elif txt:
                        renderer.user_msg(txt)
                elif b.get("type") == "tool_result":  # render any resulting screenshot inline
                    rc = b.get("content", [])
                    for blk in rc if isinstance(rc, list) else []:
                        if isinstance(blk, dict) and blk.get("type") == "image":
                            src = blk.get("source", {})
                            if src.get("type") == "base64" and src.get("data"):
                                renderer.screen(src["data"],
                                                media_type=src.get("media_type", "image/png"))
        elif t == "result":
            if turn_active is not None:
                turn_active.clear()
            renderer.result(ev.get("subtype", "ok"), ev.get("total_cost_usd"))
    # stdout closed → agent exited
    renderer.banner("\n[agent process ended]")


def _stderr(proc: subprocess.Popen) -> None:
    for line in proc.stderr:
        if line.strip():
            print(f"[claude] {line.rstrip()}", file=sys.stderr)


def run_chat(serial: str, adb_path: str, model: str = "claude-opus-4-8",
             budget: float | None = None, max_long_edge: int = 1568,
             effort: int = 3, debug_path: str | None = None, *,
             blank_png_bytes: int = 20000, auto_ui_fallback: bool = True,
             wait_idle: bool = True, settle_timeout: float = 4.0, adb_retries: int = 2,
             confirm_destructive: bool = False, destructive_keywords: list | None = None,
             ui_mode: str = "auto", inline_screen: str = "auto", screen_max_cells: int = 40,
             guardrails=None, max_output_tokens: int = 32000,
             triggers_enabled: bool = True, triggers_poll_interval_s: float = 8.0,
             triggers_cooldown_s: float = 15.0) -> int:
    from .ui import make_renderer

    try:  # flush each line as it's printed (so piped/redirected output stays live)
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    claude = shutil.which("claude")
    if not claude:
        print("error: `claude` CLI not found on PATH. Install Claude Code and log in "
              "(`claude` once, interactively).", file=sys.stderr)
        return 2

    renderer = make_renderer(ui_mode=ui_mode, inline_screen=inline_screen,
                             screen_max_cells=screen_max_cells,
                             status_fields={"provider": "claude-cli", "model": model,
                                            "device": serial, "effort": f"{effort}/5"})

    # Persistent, per-user memory folder (private; git-ignored). Lives under the
    # install dir so it sits beside the project the agent is spawned in.
    memory_dir = os.path.join(PROJECT_ROOT, "memory", getpass.getuser())
    os.makedirs(memory_dir, exist_ok=True)

    system_prompt = APPEND_SYSTEM_PROMPT + "\n\nPersistence & asking:\n" + persistence_block(effort, "chat")
    system_prompt += "\n\n" + _MEMORY_BLOCK.format(dir=memory_dir)
    if confirm_destructive:
        system_prompt += "\n\n" + _CONFIRM_PROTOCOL
    if guardrails is not None and not guardrails.is_empty():
        system_prompt += "\n\n" + guardrails.system_prompt_block()

    mcp_cfg = _mcp_config(serial, adb_path, max_long_edge,
                          blank_png_bytes=blank_png_bytes, auto_ui_fallback=auto_ui_fallback,
                          wait_idle=wait_idle, settle_timeout=settle_timeout,
                          adb_retries=adb_retries, confirm_destructive=confirm_destructive,
                          destructive_keywords=destructive_keywords,
                          forbid=(guardrails.forbid if guardrails else None),
                          stay_in_app=(guardrails.stay_in_app if guardrails else None))
    args = [
        claude, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "ToolSearch,Read,Write,Edit,Glob,mcp__agentic_android",
        "--replay-user-messages",   # echo typed messages back so steering is visible
        "--mcp-config", mcp_cfg,
        "--strict-mcp-config",
        "--append-system-prompt", system_prompt,
        "--model", model,
    ]
    if budget:
        args += ["--max-budget-usd", str(budget)]

    renderer.banner(f"Spawning agent: claude {model} · device {serial} · effort {effort}/5 · "
                    f"cwd {PROJECT_ROOT} · {'$%.2f budget' % budget if budget else 'no budget cap'}")
    # Give the chat room: a high response cap, and a high MCP-output cap so big
    # screenshots/element lists (a screenshot is tens of thousands of tokens) aren't
    # truncated by Claude Code's default per-tool-result limit.
    child_env = {
        **os.environ,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
        "MAX_MCP_OUTPUT_TOKENS": str(max(max_output_tokens, 100000)),
    }
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        start_new_session=True, cwd=PROJECT_ROOT, env=child_env,
    )

    renderer.info(f"[memory] {memory_dir}")
    if debug_path:
        renderer.info(f"[debug] saving session to {debug_path}")
    turn_active = threading.Event()  # set while a turn is running; cleared on result
    threading.Thread(target=_reader, args=(proc, renderer, turn_active, debug_path),
                     daemon=True).start()
    threading.Thread(target=_stderr, args=(proc,), daemon=True).start()

    # Both the human input loop and the notification watcher send messages to the
    # agent. Serialize stdin writes (interrupt + message) under one lock so they
    # can't interleave. Returns False if the agent's stdin is gone.
    stdin_lock = threading.Lock()
    req_box = {"n": 0}

    def send_to_agent(text: str) -> bool:
        with stdin_lock:
            try:
                # If a turn is already running, interrupt it so this message takes
                # effect NOW (otherwise it would just queue until the turn ends).
                if turn_active.is_set():
                    req_box["n"] += 1
                    proc.stdin.write(json.dumps({
                        "type": "control_request", "request_id": f"req_{req_box['n']}",
                        "request": {"subtype": "interrupt"}}) + "\n")
                    proc.stdin.flush()
                    time.sleep(0.3)  # let the interrupt land before the new message
                proc.stdin.write(json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                }) + "\n")
                proc.stdin.flush()
                turn_active.set()
                return True
            except (BrokenPipeError, ValueError):
                return False

    # Inline notification watcher: when a saved+enabled trigger matches a phone
    # notification, feed its task to the live agent like a typed steer.
    watcher = None
    if triggers_enabled:
        from .adb import ADB
        from .triggers import NotificationWatcher, list_triggers

        def _on_fire(trigger, notif):
            title = notif.get("title") or ""
            text = notif.get("text") or notif.get("big_text") or ""
            renderer.info(f"[trigger] '{trigger.name}' fired on {notif.get('package')} "
                          f"notification — waking agent")
            send_to_agent(
                f"[auto-trigger '{trigger.name}'] A notification from "
                f"{notif.get('package')} arrived — {title}: {text}. "
                f"Now perform this task: {trigger.task}")

        watcher = NotificationWatcher(
            ADB(serial=serial, adb_path=adb_path, retries=adb_retries),
            _on_fire, triggers=None, poll_interval_s=triggers_poll_interval_s,
            cooldown_s=triggers_cooldown_s, log=renderer.info)
        watcher.start()
        n_on = sum(1 for t in list_triggers() if t.enabled)
        renderer.info(f"[triggers] watching notifications (poll {triggers_poll_interval_s:g}s; "
                      f"{n_on} enabled)")

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
            if not send_to_agent(line):
                renderer.error("[agent stdin closed]")
                break
    except KeyboardInterrupt:
        print("\n[exiting]")
    finally:
        if watcher:
            watcher.stop()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.unlink(mcp_cfg)
        except OSError:
            pass
    return rc
