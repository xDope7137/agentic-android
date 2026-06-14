"""MCP stdio server exposing Android device control to a Claude agent.

The `chat` front-end spawns `claude -p` with this server wired in via
`--mcp-config`. The agent calls these tools to drive the device; screen-changing
tools return a fresh screenshot image so the agent sees the result.

Config comes from the environment (set by chat.py):
  ANDROID_SERIAL            adb serial, e.g. 192.168.1.79:5555
  AGENTIC_ANDROID_ADB          path to the adb binary
  AGENTIC_ANDROID_MAX_LONG_EDGE  downscale long edge (default 1568, keeps coords 1:1 for Claude)
"""

from __future__ import annotations

import base64
import os

from mcp.server.fastmcp import FastMCP, Image

from .adb import ADB, ADBError
from .device import DEFAULT_DESTRUCTIVE, Device, is_destructive

mcp = FastMCP("agentic_android")

_device: Device | None = None


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "", "false", "no")


def _confirm_on() -> bool:
    return _envb("AGENTIC_ANDROID_CONFIRM_DESTRUCTIVE", False)


def _forbid_list() -> list[str]:
    raw = os.environ.get("AGENTIC_ANDROID_FORBID", "")
    return [x for x in raw.split(",") if x]


def _dev() -> Device:
    global _device
    if _device is None:
        adb = ADB(
            serial=os.environ.get("ANDROID_SERIAL") or None,
            adb_path=os.environ.get("AGENTIC_ANDROID_ADB") or None,
            retries=int(os.environ.get("AGENTIC_ANDROID_ADB_RETRIES", "2")),
        )
        adb.ensure_connected()  # connect network serials (host:port)
        kw = os.environ.get("AGENTIC_ANDROID_DESTRUCTIVE_KEYWORDS")
        _device = Device(
            adb=adb,
            max_long_edge=int(os.environ.get("AGENTIC_ANDROID_MAX_LONG_EDGE", "1568")),
            blank_png_bytes=int(os.environ.get("AGENTIC_ANDROID_BLANK_PNG_BYTES", "20000")),
            auto_ui_fallback=_envb("AGENTIC_ANDROID_AUTO_UI_FALLBACK", True),
            wait_idle=_envb("AGENTIC_ANDROID_WAIT_IDLE", True),
            settle_timeout=float(os.environ.get("AGENTIC_ANDROID_SETTLE_TIMEOUT", "4.0")),
            destructive_keywords=[k for k in kw.split(",") if k] if kw else DEFAULT_DESTRUCTIVE,
        )
    return _device


def _shot():
    """Return the current screen for a tool result. Normally an Image; if the
    capture comes back blank/black (a protected/unrendered surface), return the
    UI element list as text instead so the agent isn't stranded on a dead image."""
    dev = _dev()
    o = dev.observe(vision=True)
    if o["blank"] and dev.auto_ui_fallback:
        return o["text"]
    return Image(data=base64.b64decode(o["image"]), format="png")


def _gate_coords(dev: Device, x: int, y: int) -> str | None:
    """Best-effort 'BLOCKED' message if a raw tap lands on a known destructive
    element. No-op when nothing relevant is nearby."""
    els = dev._last_elements
    if not els:
        return None
    dx, dy = dev._to_device(x, y)
    near = min(els, key=lambda e: (e["cx"] - dx) ** 2 + (e["cy"] - dy) ** 2)
    if (near["cx"] - dx) ** 2 + (near["cy"] - dy) ** 2 > 75 * 75:
        return None
    kw = is_destructive(near.get("label"), dev.destructive_keywords)
    if kw:
        label = near.get("label") or ""
        return (f"BLOCKED: tapping near '{label}' looks destructive (matched '{kw}'). "
                "Ask the user to confirm in chat, then retry tap with confirm=true.")
    return None


@mcp.tool()
def screenshot() -> Image:
    """Capture and return the current device screen as an image. Coordinates for
    tap/swipe are in pixels of this image (top-left origin)."""
    return _shot()


@mcp.tool()
def tap(x: int, y: int, confirm: bool = False):
    """Tap at (x, y) in screenshot pixels. Returns the resulting screen. If the
    destructive-action gate is on and the tap lands on a risky control, it is
    blocked until you pass confirm=true (after the user agrees)."""
    dev = _dev()
    if _confirm_on() and not confirm:
        blocked = _gate_coords(dev, x, y)
        if blocked:
            return blocked
    try:
        dev.tap(x, y)
    except ADBError as e:
        return f"tap failed: {e}"
    return _shot()


@mcp.tool()
def tap_element(index: int | None = None, text: str = "", confirm: bool = False):
    """Preferred tap: tap an element from the dump_ui/element list by its #index
    or by its text/label (case-insensitive). More reliable than raw coordinates.
    If the destructive-action gate is on and the target is risky (uninstall, buy,
    delete, …), it is blocked until you pass confirm=true (after the user agrees)."""
    dev = _dev()
    try:
        target = dev.resolve_element(index=index, text=text or None)
    except ADBError as e:
        return f"tap_element failed: {e}"
    label = target.get("label") or target.get("id") or ""
    hit = next((f for f in _forbid_list() if f.lower() in label.lower()), None)
    if hit:
        return (f"GUARDRAIL VIOLATED: tapping '{label}' matches the forbidden rule '{hit}'. "
                "Do not do this — tell the user and stop.")
    if _confirm_on() and not confirm:
        kw = is_destructive(target.get("label") or target.get("id"), dev.destructive_keywords)
        if kw:
            label = target.get("label") or target.get("id") or ""
            return (f"BLOCKED: tapping '{label}' is a destructive action (matched '{kw}'). "
                    "Ask the user to confirm in chat, then retry tap_element with confirm=true.")
    try:
        dev.tap_element(index=index, text=text or None)
    except ADBError as e:
        return f"tap_element failed: {e}"
    return _shot()


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
    """Swipe/drag from (x1,y1) to (x2,y2) in screenshot pixels. To scroll the page
    down, swipe up (large y -> small y). Returns the resulting screen."""
    try:
        _dev().swipe(x1, y1, x2, y2, duration_ms)
    except ADBError as e:
        return f"swipe failed: {e}"
    return _shot()


@mcp.tool()
def type_text(text: str):
    """Type text into the focused field (tap it first). Returns the resulting screen."""
    try:
        _dev().type_text(text)
    except ADBError as e:
        return f"type_text failed: {e}"
    return _shot()


@mcp.tool()
def press_key(key: str):
    """Send a key event: BACK, HOME, ENTER, TAB, DEL, APP_SWITCH, etc. Returns the screen."""
    try:
        _dev().key(key)
    except ADBError as e:
        return f"press_key failed: {e}"
    return _shot()


@mcp.tool()
def launch_app(package: str):
    """Launch an app by package name, e.g. com.android.vending (Play Store). Returns the screen."""
    try:
        _dev().launch_app(package)
    except ADBError as e:
        return f"launch_app failed: {e}"
    return _shot()


@mcp.tool()
def list_apps(filter: str = "", launchable_only: bool = True) -> str:
    """List installed app package names (for use with launch_app). Filter by a
    case-insensitive substring like 'clock'. Use this instead of guessing a
    package name; if nothing matches, that app isn't installed."""
    try:
        return _dev().list_apps(filter or None, launchable_only)
    except ADBError as e:
        return f"list_apps failed: {e}"


@mcp.tool()
def dump_ui() -> str:
    """Return the uiautomator XML view hierarchy (exact text/resource-id/bounds of
    on-screen elements). Use when visual tapping is ambiguous."""
    try:
        xml = _dev().ui_xml()
    except ADBError as e:
        return f"dump_ui failed: {e}"
    return xml[:40000] + "\n…[truncated]" if len(xml) > 40000 else xml


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
