"""Thin wrapper around the `adb` command-line tool.

Every method shells out to `adb`. A device serial can be pinned via the
constructor or the ANDROID_SERIAL environment variable; otherwise adb's
default device is used.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time


class ADBError(RuntimeError):
    """Raised when an adb invocation exits non-zero."""


# Error fragments that usually mean "try again" (a flaky/dropped connection)
# rather than a real failure like "no such element". Retried with backoff.
_TRANSIENT = (
    "device offline", "device not found", "no devices", "device still authorizing",
    "device unauthorized", "closed", "protocol fault", "connection reset",
    "connection refused", "timed out", "timeout", "error: device",
)


# Characters that `input text` / the shell would otherwise interpret.
_TEXT_ESCAPE = "()<>|;&*\\~\"'`$ "

# A valid 'package/activity' component, used to gate `am start -n` (the am path
# bypasses the input-text escaping below, so validate before shelling).
_COMPONENT_RE = re.compile(r"^[\w.]+/[\w.$]+$")


def resolve_adb_path() -> str:
    """Find an adb binary: $AGENTIC_ANDROID_ADB, then PATH, then the one bundled
    with the `adbutils` package (so no system install is required)."""
    env = os.environ.get("AGENTIC_ANDROID_ADB")
    if env:
        return env
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    try:
        import adbutils

        binaries = os.path.join(os.path.dirname(adbutils.__file__), "binaries")
        # Match the adb binary by exact name per-platform: adb.exe on Windows,
        # the extension-less binary on POSIX. A loose `adb*` glob also matches
        # the bundled AdbWinApi*.dll helpers, which aren't runnable (WinError 193).
        name = "adb.exe" if os.name == "nt" else "adb"
        cand = os.path.join(binaries, name)
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    return "adb"


# Markers that begin the trailer sections of `dumpsys notification` (config,
# history, listeners). We only parse the live records before these, so dismissed
# / archived notifications aren't mistaken for active ones.
_NOTIF_TRAILER = re.compile(
    r"\n\s*(?:mZenLog|mArchive|Notification groups|NotificationRankingHelper|"
    r"mEnabledNotificationListeners|mAssistants|Ranking Config|mNotificationsByKey)\b")
_NOTIF_PKG_RE = re.compile(r"\bpkg=(\S+)")
_NOTIF_KEY_RE = re.compile(r"\bkey=(\S+)")
_NOTIF_WHEN_RE = re.compile(r"\bwhen=(\d+)")


def _notif_value(raw: str) -> str:
    """Clean an `android.*` extras value across the various dumpsys formats:
    `Boss`, `String (5) "Boss"`, `Boss (String)`."""
    v = raw.strip()
    v = re.sub(r'^String \(\d+\)\s*', "", v)   # `String (5) "Boss"` -> `"Boss"`
    v = re.sub(r'\s*\((?:String|CharSequence|SpannableString)\)$', "", v)  # trailing type tag
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v.strip()


def _notif_extra(block: str, *keys: str) -> str:
    """First non-empty value among the given `android.<key>` extras in a block."""
    for k in keys:
        m = re.search(rf"\bandroid\.{re.escape(k)}=(.*)", block)
        if m:
            val = _notif_value(m.group(1))
            if val and val.lower() != "null":
                return val
    return ""


def _parse_notifications(out: str) -> list[dict]:
    """Parse `dumpsys notification` text into a list of active-notification dicts.
    Tolerant of version differences; unknown/missing fields become ''/None."""
    if not out or "NotificationRecord(" not in out:
        return []
    trailer = _NOTIF_TRAILER.search(out)
    if trailer:
        out = out[: trailer.start()]
    # Split into per-record blocks; chunk[0] is the preamble before the first record.
    chunks = out.split("NotificationRecord(")
    seen: set[str] = set()
    records: list[dict] = []
    for chunk in chunks[1:]:
        pkg_m = _NOTIF_PKG_RE.search(chunk)
        key_m = _NOTIF_KEY_RE.search(chunk)
        if not pkg_m:
            continue
        key = key_m.group(1).rstrip(")") if key_m else ""
        dedupe = key or f"{pkg_m.group(1)}#{len(records)}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        when_m = _NOTIF_WHEN_RE.search(chunk)
        records.append({
            "key": key,
            "package": pkg_m.group(1),
            "title": _notif_extra(chunk, "title"),
            "text": _notif_extra(chunk, "text"),
            "big_text": _notif_extra(chunk, "bigText", "subText") or None,
            "when": int(when_m.group(1)) if when_m else None,
        })
    return records


class ADB:
    def __init__(self, serial: str | None = None, adb_path: str | None = None,
                 retries: int = 2, backoff: float = 0.5, timeout: int = 60):
        self.serial = serial or os.environ.get("ANDROID_SERIAL")
        self.adb_path = adb_path or resolve_adb_path()
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def ensure_connected(self) -> None:
        """For a network serial (host:port), make sure adb has connected to it."""
        if self.serial and ":" in self.serial:
            try:
                self.run("connect", self.serial)
            except ADBError:
                pass

    # -- core ---------------------------------------------------------------

    def _base(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, binary: bool = False, timeout: int | None = None,
            retries: int | None = None, backoff: float | None = None):
        """Run `adb <args>` and return stdout (str, or bytes if binary).

        Transient failures (a dropped/flaky connection, a timeout) are retried
        with exponential backoff, reconnecting first for network serials. A real
        error (non-zero exit that isn't transient) fails fast. A timeout is
        surfaced as an ADBError so callers' `except ADBError` handles it."""
        timeout = self.timeout if timeout is None else timeout
        retries = self.retries if retries is None else retries
        backoff = self.backoff if backoff is None else backoff
        last: ADBError | None = None
        for attempt in range(retries + 1):
            try:
                proc = subprocess.run(
                    self._base() + list(args), capture_output=True, timeout=timeout
                )
            except FileNotFoundError as exc:
                raise ADBError(
                    f"`{self.adb_path}` not found. Install Android platform-tools "
                    "and ensure adb is on PATH."
                ) from exc
            except subprocess.TimeoutExpired:
                last = ADBError(f"adb {' '.join(args)} timed out after {timeout}s")
            else:
                if proc.returncode == 0:
                    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")
                msg = proc.stderr.decode("utf-8", "replace").strip()
                last = ADBError(msg or f"adb {' '.join(args)} failed (exit {proc.returncode})")
                if not any(t in msg.lower() for t in _TRANSIENT):
                    raise last  # a real error — don't waste retries on it
            if attempt < retries:
                self._reconnect()
                time.sleep(backoff * (2 ** attempt))
        raise last or ADBError(f"adb {' '.join(args)} failed")

    def _reconnect(self) -> None:
        """Best-effort re-establish a dropped network (host:port) connection.
        Runs a bare `adb connect` directly (no -s, no retry) to avoid recursion."""
        if self.serial and ":" in self.serial:
            try:
                subprocess.run([self.adb_path, "connect", self.serial],
                               capture_output=True, timeout=self.timeout)
            except Exception:
                pass

    def shell(self, command: str, binary: bool = False, timeout: int = 60):
        """Run a command inside `adb shell`."""
        return self.run("shell", command, binary=binary, timeout=timeout)

    # -- discovery ----------------------------------------------------------

    def devices(self) -> list[str]:
        """Return serials of attached, ready devices."""
        out = self.run("devices")
        serials = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and line.endswith("\tdevice"):
                serials.append(line.split("\t", 1)[0])
        return serials

    # -- capture ------------------------------------------------------------

    def screencap(self) -> bytes:
        """Return the current screen as raw PNG bytes."""
        return self.run("exec-out", "screencap", "-p", binary=True)

    def ui_dump(self) -> str:
        """Return the current uiautomator view hierarchy as XML text."""
        # Dump to the device, then stream it back. /dev/tty piping is flaky
        # across Android versions, so go through a file.
        self.shell("uiautomator dump /sdcard/window_dump.xml >/dev/null 2>&1")
        return self.run("exec-out", "cat", "/sdcard/window_dump.xml")

    # -- input --------------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def input_text(self, text: str) -> None:
        safe = text
        for ch in _TEXT_ESCAPE:
            safe = safe.replace(ch, "%s" if ch == " " else "\\" + ch)
        self.shell(f"input text {safe}")

    def key(self, keycode: str) -> None:
        """Send a key event. Accepts `BACK`, `KEYCODE_BACK`, or a raw number."""
        kc = keycode.strip().upper()
        if kc.isdigit() or kc.startswith("KEYCODE_"):
            arg = kc
        else:
            arg = f"KEYCODE_{kc}"
        self.shell(f"input keyevent {arg}")

    # -- apps ---------------------------------------------------------------

    def launch_app(self, package: str) -> str:
        return self.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
        )

    def list_packages(self, third_party_only: bool = False) -> list[str]:
        cmd = "pm list packages" + (" -3" if third_party_only else "")
        out = self.shell(cmd)
        pkgs = [ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith("package:")]
        return sorted(set(pkgs))

    def launchable_packages(self) -> list[str]:
        """Packages that have a home-screen launcher entry (the apps a user sees)."""
        try:
            out = self.shell(
                "cmd package query-activities -a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER"
            )
        except ADBError:
            return []
        return sorted(set(re.findall(r"packageName=(\S+)", out)))

    # -- activities / intents (used by skills + guardrails) -----------------

    def top_activity(self) -> str | None:
        """The resumed component 'pkg/activity' (the screen in front), or None."""
        try:
            out = self.shell("dumpsys activity activities")
        except ADBError:
            return None
        m = re.search(
            r"(?:topResumedActivity|mResumedActivity|ResumedActivity)\b.*?(\S+/\S+)", out)
        if not m:
            return None
        comp = m.group(1).rstrip("}").strip()
        return comp if "/" in comp else None

    def foreground_package(self) -> str | None:
        """Package name of the foreground app, or None."""
        comp = self.top_activity()
        return comp.split("/", 1)[0] if comp else None

    def start_activity(self, component: str) -> str:
        """Launch a specific component 'pkg/activity' via `am start -n` (a learned
        shortcut, or a checkpoint relaunch). Component is validated first."""
        if not _COMPONENT_RE.match(component or ""):
            raise ADBError(f"refusing to start unsafe component {component!r}")
        return self.shell(f"am start -n {component}")

    def start_action(self, action: str, data: str | None = None) -> str:
        """Fire an intent by action (+ optional data URI / deeplink) via `am start -a`."""
        if not re.match(r"^[\w.]+$", action or ""):
            raise ADBError(f"refusing unsafe action {action!r}")
        cmd = f"am start -a {action}"
        if data:
            cmd += f" -d {shlex.quote(data)}"
        return self.shell(cmd)

    # -- notifications (used by the trigger watcher) ------------------------

    def notifications(self) -> list[dict]:
        """Currently-posted notifications, parsed from `dumpsys notification`.

        Returns a list of dicts:
            {"key": str, "package": str, "title": str, "text": str,
             "big_text": str | None, "when": int | None}

        The dumpsys output format is Android-version-specific, so parsing is
        deliberately defensive: any failure (unknown format, missing fields)
        degrades to [] / empty fields rather than raising, so a polling watcher
        never crashes on a quirky device."""
        out = ""
        for cmd in ("dumpsys notification --noredact", "dumpsys notification"):
            # `--noredact` keeps android.title/text from being shown as REDACTED on
            # secure notifications; it's unsupported on older Android, so fall back.
            try:
                out = self.shell(cmd, timeout=20)
            except ADBError:
                out = ""
            if "NotificationRecord(" in out:
                break
        try:
            return _parse_notifications(out)
        except Exception:
            return []
