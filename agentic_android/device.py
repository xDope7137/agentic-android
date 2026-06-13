"""Higher-level device view: screenshots (optionally downscaled) and input
with coordinate translation back to real device pixels.

Claude is shown the (possibly downscaled) screenshot and works in that image's
coordinate space. `scale` maps those coordinates back to physical pixels so
taps land where Claude intended.
"""

from __future__ import annotations

import base64
import io
import re
import time

try:  # defusedxml hardens against XXE / billion-laughs; stdlib fallback is fine
    from defusedxml.ElementTree import fromstring as _xml_fromstring  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    from xml.etree.ElementTree import fromstring as _xml_fromstring

from .adb import ADB, ADBError

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

try:  # Pillow is optional — only needed to downscale large screenshots.
    from PIL import Image

    _HAVE_PIL = True
except Exception:  # pragma: no cover - import guard
    _HAVE_PIL = False


def _png_size(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG's IHDR chunk without decoding the image."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG screenshot")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


class Device:
    def __init__(self, adb: ADB | None = None, max_long_edge: int = 1568, settle: float = 0.8):
        self.adb = adb or ADB()
        self.max_long_edge = max_long_edge
        self.settle = settle
        # scale = shown_image_pixels / real_device_pixels  (<= 1.0)
        self.scale = 1.0
        # elements from the most recent ui_elements(): [{index,label,id,cx,cy,...}]
        self._last_elements: list[dict] = []

    # -- screen -------------------------------------------------------------

    def screenshot(self) -> dict:
        """Capture the screen.

        Returns a dict with base64 PNG `data` and the `width`/`height` of the
        image Claude will see. Updates `self.scale` for coordinate mapping.
        """
        png = self.adb.screencap()
        ow, oh = _png_size(png)
        long_edge = max(ow, oh)

        if self.max_long_edge and long_edge > self.max_long_edge and _HAVE_PIL:
            scale = self.max_long_edge / long_edge
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img = img.resize((round(ow * scale), round(oh * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
            self.scale = scale
            w, h = img.size
        else:
            # No resize (small enough, or Pillow unavailable). 1:1 coordinates.
            self.scale = 1.0
            w, h = ow, oh

        return {
            "data": base64.standard_b64encode(png).decode("ascii"),
            "width": w,
            "height": h,
        }

    def ui_xml(self) -> str:
        return self.adb.ui_dump()

    def list_apps(self, name_filter: str | None = None, launchable_only: bool = True,
                  limit: int = 200) -> str:
        """Installed app package names (for use with launch_app), optionally
        filtered by a case-insensitive substring like 'clock'."""
        scope = "launchable apps"
        pkgs = self.adb.launchable_packages() if launchable_only else []
        if not pkgs:
            pkgs = self.adb.list_packages()
            scope = "packages"
        if name_filter:
            f = name_filter.lower()
            pkgs = [p for p in pkgs if f in p.lower()]
        total = len(pkgs)
        if not pkgs:
            return f"No {scope}" + (f" match '{name_filter}'." if name_filter else " found.")
        head = (f"{total} {scope}" + (f" matching '{name_filter}'" if name_filter else "")
                + (f" (showing first {limit})" if total > limit else "") + ":\n")
        return head + "\n".join(pkgs[:limit])

    def ui_elements(self, max_items: int = 150) -> str:
        """Text view of the screen for models that can't see images: a numbered
        list of on-screen elements with a tap point @(x,y) in device pixels.

        Built from the uiautomator hierarchy (the DopeGram approach). Coordinates
        are real device pixels, so in text mode the screen is never downscaled and
        taps use these coordinates directly.
        """
        try:
            root = _xml_fromstring(self.ui_xml())
        except Exception:
            return "(could not parse the UI tree)"

        self.scale = 1.0  # text mode: no image scaling, coords are device pixels
        self._last_elements = []
        lines: list[str] = []
        sw = sh = 0
        idx = 0
        for node in root.iter("node"):
            a = node.attrib
            text = (a.get("text") or "").strip()
            desc = (a.get("content-desc") or "").strip()
            clickable = a.get("clickable") == "true"
            scrollable = a.get("scrollable") == "true"
            editable = a.get("class", "").endswith("EditText")
            m = _BOUNDS_RE.match(a.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            sw, sh = max(sw, x2), max(sh, y2)
            if not (text or desc or clickable or scrollable or editable):
                continue
            cls = a.get("class", "").split(".")[-1] or "View"
            label = text or desc
            rid = a.get("resource-id", "").split("/")[-1]
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            self._last_elements.append({"index": idx, "label": label, "id": rid,
                                        "cx": cx, "cy": cy, "clickable": clickable})
            flags = []
            if clickable:
                flags.append("tap")
            if editable:
                flags.append("input")
            if scrollable:
                flags.append("scroll")
            if a.get("selected") == "true":
                flags.append("selected")
            parts = [f"#{idx}", f"[{cls}]"]
            if label:
                parts.append(f'"{label[:50]}"')
            if rid:
                parts.append(f"id={rid}")
            parts.append(f"@({cx},{cy})")
            if flags:
                parts.append("[" + ",".join(flags) + "]")
            lines.append(" ".join(parts))
            idx += 1
            if idx >= max_items:
                lines.append("…(more elements truncated)")
                break

        if not lines:
            return "(no interactable elements found on screen)"
        header = (f"Screen {sw}x{sh}px — {idx} elements. Tap with the @(x,y) point. "
                  "Flags: tap=clickable, input=text field, scroll=scrollable.\n")
        return header + "\n".join(lines)

    # -- input (coords are in shown-image space) ----------------------------

    def _to_device(self, x: int, y: int) -> tuple[int, int]:
        if self.scale == 1.0:
            return int(x), int(y)
        return round(x / self.scale), round(y / self.scale)

    def tap(self, x: int, y: int) -> None:
        dx, dy = self._to_device(x, y)
        self.adb.tap(dx, dy)
        time.sleep(self.settle)

    def tap_element(self, index: int | None = None, text: str | None = None) -> dict:
        """Tap an element from the latest element list by its #index or by its
        text/label (case-insensitive substring). Returns the element tapped.
        More reliable than raw coordinates — no arithmetic for the model."""
        elements = self._last_elements
        if not elements:
            self.ui_elements()  # refresh (e.g. in vision mode where it wasn't built)
            elements = self._last_elements
        target = None
        if index is not None:
            target = next((e for e in elements if e["index"] == int(index)), None)
            if target is None:
                rng = f"0..{len(elements) - 1}" if elements else "none"
                raise ADBError(f"no element #{index} on screen (valid: {rng})")
        elif text:
            t = text.lower()
            matches = [e for e in elements
                       if t in (e["label"] or "").lower() or t in (e["id"] or "").lower()]
            pool = [e for e in matches if e["clickable"]] or matches
            if not pool:
                raise ADBError(f"no element matching text {text!r} on screen")
            target = pool[0]
        else:
            raise ADBError("tap_element needs an index or text")
        self.adb.tap(target["cx"], target["cy"])  # _last_elements coords are device px
        time.sleep(self.settle)
        return target

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        a = self._to_device(x1, y1)
        b = self._to_device(x2, y2)
        self.adb.swipe(a[0], a[1], b[0], b[1], duration_ms)
        time.sleep(self.settle)

    def type_text(self, text: str) -> None:
        self.adb.input_text(text)
        time.sleep(self.settle)

    def key(self, keycode: str) -> None:
        self.adb.key(keycode)
        time.sleep(self.settle)

    def launch_app(self, package: str) -> str:
        out = self.adb.launch_app(package)
        time.sleep(max(self.settle, 1.5))
        return out
