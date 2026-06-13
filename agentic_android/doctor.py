"""Preflight diagnostics for Agentic Android.

`--doctor` runs the full set of checks with fix-it hints; a lighter `preflight()`
runs automatically before a normal run so setup problems surface as clear messages
instead of cryptic errors. The checks reuse the same primitives the agent uses
(adb, the Device screenshot health-check from "Reliable Runs") and never make a
paid LLM API call — provider checks only verify keys/packages/local servers.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .adb import ADB, ADBError
from .device import Device

OFFICIAL_OPENAI = "https://api.openai.com/v1"
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclasses.dataclass
class Check:
    name: str
    status: str          # PASS | FAIL | WARN | SKIP
    detail: str = ""     # what was found
    hint: str = ""       # fix-it instruction, shown only on FAIL/WARN


# --------------------------------------------------------------------------- #
# low-level helpers (stdlib only — no httpx dependency)
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, timeout: float = 4.0) -> tuple[int, dict | None]:
    """GET a URL and parse JSON. Returns (status_code, json|None); never raises.
    status_code 0 means the request itself failed (connection refused, etc.)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (local/trusted URL)
            body = r.read().decode("utf-8", "replace")
            status = getattr(r, "status", 200) or 200
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


# --------------------------------------------------------------------------- #
# adb / device / screenshot
# --------------------------------------------------------------------------- #
def check_adb(adb: ADB) -> Check:
    try:
        out = adb.run("version", retries=0).strip().splitlines()
        ver = out[0] if out else "adb"
        return Check("adb", PASS, f"{ver}  ({adb.adb_path})")
    except ADBError as e:
        return Check("adb", FAIL, str(e),
                     "Install Android platform-tools, or reinstall so the bundled "
                     "adbutils binary is available: pip install -e .")


def check_device(adb: ADB, serial: str | None, *, connect_timeout: float = 10.0) -> Check:
    if serial and ":" in serial:  # network serial — try to (re)connect, bounded
        try:
            subprocess.run([adb.adb_path, "connect", serial],
                           capture_output=True, timeout=connect_timeout)
        except Exception:
            pass
    try:
        raw = adb.run("devices", retries=0)
    except ADBError as e:
        return Check("device", FAIL, str(e), "adb isn't responding — try `adb kill-server`.")
    lines = [ln.strip() for ln in raw.splitlines()[1:] if ln.strip()]
    ready = [ln.split("\t", 1)[0] for ln in lines if ln.endswith("\tdevice")]
    if serial:
        if serial in ready:
            return Check("device", PASS, f"{serial} connected")
        bad = [ln for ln in lines if ln.split("\t", 1)[0] == serial]
        detail = bad[0] if bad else f"{serial} not connected"
        return Check("device", FAIL, detail,
                     "Network emulator: `adb connect <ip>:<port>`. If it says "
                     "'unauthorized', accept the USB-debugging prompt on the device.")
    if not ready:
        detail = "; ".join(lines) if lines else "no devices attached"
        return Check("device", FAIL, detail,
                     "Connect a device/emulator (network: `adb connect <ip>:<port>`).")
    return Check("device", PASS, f"{len(ready)} device(s): {', '.join(ready)}")


def check_screenshot(device: Device) -> Check:
    try:
        shot = device.screenshot()
    except Exception as e:
        return Check("screenshot", FAIL, f"capture failed: {e}",
                     "The device is connected but screencap failed — wake/unlock it and retry.")
    if shot.get("blank"):
        return Check("screenshot", WARN, f"{shot['width']}x{shot['height']} but looks blank/black",
                     "Wake/unlock the device. (Some app surfaces can't be captured; the "
                     "agent automatically falls back to the UI tree for those.)")
    return Check("screenshot", PASS, f"{shot['width']}x{shot['height']} PNG, non-blank")


# --------------------------------------------------------------------------- #
# provider reachability (no paid API calls)
# --------------------------------------------------------------------------- #
def check_provider(cfg, provider: str, *, model: str | None = None,
                   base_url: str | None = None) -> list[Check]:
    if provider == "claude-cli":
        return _check_claude_cli()
    if provider == "anthropic":
        return _check_anthropic(cfg)
    if provider == "openai":
        return _check_openai(cfg, base_url or cfg.openai_base_url)
    if provider == "ollama":
        return _check_local("ollama", base_url or cfg.ollama_base_url)
    if provider == "lmstudio":
        return _check_local("lmstudio", base_url or cfg.lmstudio_base_url)
    return [Check("provider", SKIP, provider)]


def _check_claude_cli() -> list[Check]:
    path = shutil.which("claude")
    if not path:
        return [Check("claude cli", FAIL, "not found on PATH",
                      "Install Claude Code, then run `claude` once to log in. No API key needed.")]
    checks = [Check("claude cli", PASS, f"{path} (ensure you've run `claude` once to log in)")]
    try:
        out = subprocess.run([path, "--version"], capture_output=True, timeout=8)
        ver = out.stdout.decode("utf-8", "replace").strip()
        checks.append(Check("claude version", PASS, ver or "ok"))
    except Exception:
        checks.append(Check("claude version", WARN, "couldn't run `claude --version`",
                            "Make sure the claude CLI actually runs."))
    return checks


def _check_anthropic(cfg) -> list[Check]:
    if importlib.util.find_spec("anthropic") is None:
        return [Check("anthropic", FAIL, "package not installed", "pip install anthropic")]
    key = cfg.anthropic_api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        return [Check("anthropic", FAIL, "no API key",
                      "Set [anthropic] api_key in agentic-android.toml or $ANTHROPIC_API_KEY.")]
    return [Check("anthropic", PASS, "API key present, package installed")]


def _check_openai(cfg, base_url: str) -> list[Check]:
    if importlib.util.find_spec("openai") is None:
        return [Check("openai", FAIL, "package not installed", "pip install openai")]
    if base_url.rstrip("/") == OFFICIAL_OPENAI.rstrip("/"):
        if not cfg.openai_api_key:
            return [Check("openai", FAIL, "no API key",
                          "Set [openai] api_key or $OPENAI_API_KEY (or point base_url at a local server).")]
        return [Check("openai", PASS, "API key present, package installed")]
    return _probe_models("openai", base_url)  # OpenAI-compatible local/remote server


def _check_local(kind: str, base_url: str) -> list[Check]:
    return _probe_models(kind, base_url)


def _probe_models(kind: str, base_url: str) -> list[Check]:
    url = base_url.rstrip("/") + "/models"
    status, data = _http_get_json(url)
    if status == 0:
        return [Check(f"{kind} server", FAIL, f"no response from {base_url}", _server_hint(kind))]
    if not data:
        return [Check(f"{kind} server", PASS, f"{base_url} reachable"),
                Check(f"{kind} model", WARN, "couldn't read the model list", _model_hint(kind))]
    models = [m.get("id", "") for m in (data.get("data") or []) if isinstance(m, dict)]
    chat = [m for m in models if "embed" not in m.lower()]
    checks = [Check(f"{kind} server", PASS, f"{base_url} reachable")]
    if not chat:
        checks.append(Check(f"{kind} model", WARN, "no tool-calling model loaded", _model_hint(kind)))
    else:
        checks.append(Check(f"{kind} model", PASS, f"{len(chat)} model(s), e.g. {chat[0]}"))
    return checks


def _server_hint(kind: str) -> str:
    return ("Start `ollama serve`." if kind == "ollama"
            else "Start the LM Studio server (Developer tab) with a model loaded.")


def _model_hint(kind: str) -> str:
    return ("Pull a tool-calling model, e.g. `ollama pull qwen3`." if kind == "ollama"
            else "Load a tool-calling model in LM Studio.")


# --------------------------------------------------------------------------- #
# device selection (shared by the normal run and --doctor)
# --------------------------------------------------------------------------- #
def select_device(adb: ADB, serial: str | None, attached: list[str], *,
                  auto_select: bool = True, interactive: bool = True,
                  assume_yes: bool = False) -> tuple[str | None, str | None]:
    """Resolve the device serial. Returns (serial, error_message).

    - serial already set -> use it.
    - no devices -> error.
    - exactly one device -> use it (when auto_select).
    - many devices -> prompt when interactive, else a clear error.
    """
    if serial:
        return serial, None
    if not attached:
        return None, ("no Android device detected. Boot/connect one (network emulator: "
                      "`adb connect <ip>:<port>`) and check --list-devices.")
    if len(attached) == 1:
        return (attached[0], None) if auto_select else (attached[0], None)
    if interactive and not assume_yes:
        print("Multiple devices attached:")
        for i, s in enumerate(attached, 1):
            print(f"  {i}) {s}")
        try:
            raw = input("Pick a device [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None, "no device selected."
        if not raw:
            return attached[0], None
        if raw.isdigit() and 1 <= int(raw) <= len(attached):
            return attached[int(raw) - 1], None
        return None, f"invalid selection {raw!r}."
    return None, ("multiple devices attached; set [device].serial or pass --serial:\n  "
                  + "\n  ".join(attached))


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run_checks(cfg, *, provider: str, serial: str | None, model: str | None = None,
               base_url: str | None = None, max_long_edge: int = 1568,
               include_screenshot: bool = True, connect_timeout: float = 10.0) -> list[Check]:
    adb = ADB(serial=serial)
    checks = [check_adb(adb)]
    dev = check_device(adb, serial, connect_timeout=connect_timeout)
    checks.append(dev)
    if include_screenshot:
        if dev.status == PASS:
            checks.append(check_screenshot(Device(adb=adb, max_long_edge=max_long_edge)))
        else:
            checks.append(Check("screenshot", SKIP, "device not ready"))
    checks += check_provider(cfg, provider, model=model, base_url=base_url)
    return checks


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"  [{c.status}] {c.name:<14} {c.detail}")
        if c.status in (FAIL, WARN) and c.hint:
            lines.append(f"           → {c.hint}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    if fails:
        lines.append(f"  {fails} check(s) failed — fix the above and re-run --doctor.")
    elif warns:
        lines.append(f"  {warns} warning(s). You can run, but see the notes above.")
    else:
        lines.append("  All checks passed — you're ready to run.")
    return "\n".join(lines)


def doctor(cfg, *, provider: str, serial: str | None, model: str | None = None,
           base_url: str | None = None, max_long_edge: int = 1568,
           connect_timeout: float = 10.0) -> int:
    print("Agentic Android — preflight doctor")
    print(f"  provider={provider} · device={serial or '(auto-detect)'}\n")
    checks = run_checks(cfg, provider=provider, serial=serial, model=model, base_url=base_url,
                        max_long_edge=max_long_edge, include_screenshot=True,
                        connect_timeout=connect_timeout)
    print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def preflight(cfg, *, provider: str, serial: str | None, model: str | None = None,
              base_url: str | None = None, max_long_edge: int = 1568,
              include_screenshot: bool = True, connect_timeout: float = 10.0) -> tuple[bool, str]:
    """Lightweight fail-fast checks before a normal run. Returns (ok, message);
    message is only populated on failure (lists the failing checks + hints)."""
    checks = run_checks(cfg, provider=provider, serial=serial, model=model, base_url=base_url,
                        max_long_edge=max_long_edge, include_screenshot=include_screenshot,
                        connect_timeout=connect_timeout)
    fails = [c for c in checks if c.status == FAIL]
    if not fails:
        return True, ""
    msg = ["preflight failed:"]
    for c in fails:
        msg.append(f"  [FAIL] {c.name}: {c.detail}")
        if c.hint:
            msg.append(f"         → {c.hint}")
    msg.append("  (skip these checks with --no-preflight)")
    return False, "\n".join(msg)
