"""Thin wrapper around the `adb` command-line tool.

Every method shells out to `adb`. A device serial can be pinned via the
constructor or the ANDROID_SERIAL environment variable; otherwise adb's
default device is used.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess


class ADBError(RuntimeError):
    """Raised when an adb invocation exits non-zero."""


# Characters that `input text` / the shell would otherwise interpret.
_TEXT_ESCAPE = "()<>|;&*\\~\"'`$ "


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
        cands = sorted(glob.glob(os.path.join(binaries, "adb*")))
        for c in cands:  # prefer the non-.exe binary on POSIX
            if not c.endswith(".exe"):
                return c
        if cands:
            return cands[0]
    except Exception:
        pass
    return "adb"


class ADB:
    def __init__(self, serial: str | None = None, adb_path: str | None = None):
        self.serial = serial or os.environ.get("ANDROID_SERIAL")
        self.adb_path = adb_path or resolve_adb_path()

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

    def run(self, *args: str, binary: bool = False, timeout: int = 60):
        """Run `adb <args>` and return stdout (str, or bytes if binary)."""
        try:
            proc = subprocess.run(
                self._base() + list(args), capture_output=True, timeout=timeout
            )
        except FileNotFoundError as exc:
            raise ADBError(
                f"`{self.adb_path}` not found. Install Android platform-tools "
                "and ensure adb is on PATH."
            ) from exc
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip()
            raise ADBError(msg or f"adb {' '.join(args)} failed (exit {proc.returncode})")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

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
