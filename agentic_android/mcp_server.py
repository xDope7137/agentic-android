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
from .device import Device

mcp = FastMCP("agentic_android")

_device: Device | None = None


def _dev() -> Device:
    global _device
    if _device is None:
        adb = ADB(
            serial=os.environ.get("ANDROID_SERIAL") or None,
            adb_path=os.environ.get("AGENTIC_ANDROID_ADB") or None,
        )
        adb.ensure_connected()  # connect network serials (host:port)
        _device = Device(
            adb=adb,
            max_long_edge=int(os.environ.get("AGENTIC_ANDROID_MAX_LONG_EDGE", "1568")),
        )
    return _device


def _shot() -> Image:
    shot = _dev().screenshot()
    return Image(data=base64.b64decode(shot["data"]), format="png")


@mcp.tool()
def screenshot() -> Image:
    """Capture and return the current device screen as an image. Coordinates for
    tap/swipe are in pixels of this image (top-left origin)."""
    return _shot()


@mcp.tool()
def tap(x: int, y: int):
    """Tap at (x, y) in screenshot pixels. Returns the resulting screen."""
    try:
        _dev().tap(x, y)
    except ADBError as e:
        return f"tap failed: {e}"
    return _shot()


@mcp.tool()
def tap_element(index: int | None = None, text: str = ""):
    """Preferred tap: tap an element from the dump_ui/element list by its #index
    or by its text/label (case-insensitive). More reliable than raw coordinates."""
    try:
        _dev().tap_element(index=index, text=text or None)
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
