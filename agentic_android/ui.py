"""Terminal presentation layer: optional `rich` styling + inline device screenshots.

Both interactive paths render through a single `Renderer` so output is consistent:
the API agent loop (agent.py) and the claude-cli chat (chat.py). `rich` is an
OPTIONAL dependency — when it's missing, OR when stdout isn't a TTY (e.g. piped for
a demo recording), the renderer degrades to plain text that matches what the tool
printed before. Inline screenshots use the terminal's own image protocol (kitty /
iTerm2), with no third-party dependency, and are silently skipped when unsupported.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys

try:  # rich is optional (mirrors the Pillow guard in device.py)
    from rich.console import Console
    from rich.markup import escape as _rescape
    from rich.panel import Panel
    from rich.text import Text

    _HAVE_RICH = True
except Exception:  # pragma: no cover - import guard
    _HAVE_RICH = False

    def _rescape(s):  # no-op fallback (never called when rich is absent)
        return s

try:
    from PIL import Image as _PILImage

    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


def _compact(d: dict) -> str:
    s = json.dumps(d, separators=(",", ":"))
    return s if len(s) <= 80 else s[:77] + "…"


def _humanize(name: str, args: dict) -> str:
    """A short, friendly description of a tool call instead of raw JSON."""
    a = args or {}
    if name == "screenshot":
        return "look at the screen"
    if name == "tap_element":
        t = a.get("text") or (f"#{a.get('index')}" if a.get("index") is not None else "")
        return f"tap “{t}”" if t else "tap element"
    if name == "tap":
        return f"tap ({a.get('x')}, {a.get('y')})"
    if name == "swipe":
        return "swipe"
    if name == "type_text":
        return f"type “{a.get('text', '')}”"
    if name == "press_key":
        return f"press {a.get('key', '')}"
    if name == "launch_app":
        return f"open {a.get('package', '')}"
    if name == "list_apps":
        return "list apps" + (f" ~ “{a.get('filter')}”" if a.get("filter") else "")
    if name == "dump_ui":
        return "read the screen layout"
    if name == "done":
        return "done"
    if name in ("Read", "Write", "Edit", "Glob"):  # memory / file tools
        p = a.get("file_path") or a.get("path") or a.get("pattern") or ""
        return f"{name.lower()} memory: {os.path.basename(str(p))}" if p else name.lower()
    return f"{name} {_compact(a)}"


# --------------------------------------------------------------------------- #
# terminal image protocol
# --------------------------------------------------------------------------- #
def detect_image_protocol() -> str:
    """Return 'kitty' | 'iterm2' | 'none' from the environment (cheap, no probing)."""
    if not sys.stdout.isatty():
        return "none"
    term = os.environ.get("TERM", "")
    if os.environ.get("KITTY_WINDOW_ID") or term == "xterm-kitty":
        return "kitty"
    prog = os.environ.get("TERM_PROGRAM", "")
    if prog in ("iTerm.app", "WezTerm") or os.environ.get("LC_TERMINAL") == "iTerm2":
        return "iterm2"  # WezTerm also speaks the iTerm2 inline-image protocol
    return "none"


def _to_png(data: bytes) -> bytes | None:
    """kitty needs PNG. Pass PNG through; transcode JPEG with Pillow if present."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    if _HAVE_PIL:
        try:
            im = _PILImage.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None
    return None


def emit_image(data: bytes, proto: str, *, cell_width: int) -> bool:
    """Write an inline-image escape sequence for `data` to stdout. Returns True if emitted."""
    if proto == "iterm2":  # accepts PNG and JPEG directly
        b64 = base64.b64encode(data).decode("ascii")
        sys.stdout.write(
            f"\x1b]1337;File=inline=1;width={cell_width};"
            f"preserveAspectRatio=1;size={len(data)}:{b64}\x07\n"
        )
        sys.stdout.flush()
        return True
    if proto == "kitty":
        png = _to_png(data)
        if png is None:
            return False
        b64 = base64.b64encode(png).decode("ascii")
        chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)] or [""]
        for i, ch in enumerate(chunks):
            last = i == len(chunks) - 1
            m = 0 if last else 1
            if i == 0:
                sys.stdout.write(f"\x1b_Ga=T,f=100,c={cell_width},m={m};{ch}\x1b\\")
            else:
                sys.stdout.write(f"\x1b_Gm={m};{ch}\x1b\\")
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True
    return False


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
class Renderer:
    """Styled-or-plain output + optional inline screenshots. One class, internal
    branching on `self.styled` / `self.inline` (simpler than a class hierarchy)."""

    def __init__(self, *, styled: bool, image_proto: str, inline_screen: bool,
                 screen_max_cells: int = 40, status_fields: dict | None = None):
        self.styled = styled and _HAVE_RICH
        self.console = Console() if self.styled else None
        self.image_proto = image_proto
        self.inline = inline_screen and image_proto != "none"
        self.screen_max_cells = screen_max_cells
        self.status_fields = dict(status_fields or {})

    # -- text ---------------------------------------------------------------
    def banner(self, text: str) -> None:
        if self.styled:
            self.console.print(text, style="dim", markup=False, highlight=False)
        else:
            print(text, flush=True)

    def info(self, text: str) -> None:
        if self.styled:
            self.console.print(text, markup=False, highlight=False)
        else:
            print(text, flush=True)

    def error(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    def agent_text(self, text: str) -> None:
        if self.styled:
            self.console.print(Panel(Text(text), title="agent", title_align="left",
                                     border_style="green", padding=(0, 1)))
        else:
            print(f"\nagent> {text}", flush=True)

    def user_msg(self, text: str) -> None:
        """Show a message the user sent (used for mid-task steering echoes)."""
        if self.styled:
            self.console.print(Panel(Text(text), title="you", title_align="right",
                                     border_style="blue", padding=(0, 1)))
        else:
            print(f"\nyou ▸ {text}", flush=True)

    def tool_start(self, name: str, args: dict):
        if self.styled:
            self.console.print(f"   [cyan]·[/cyan] [dim]{_rescape(_humanize(name, args))}[/dim]",
                               highlight=False)
        else:
            print(f"   · {_humanize(name, args)}", flush=True)
        return None  # ToolHandle slot (spinners would attach here later)

    def tool_end(self, handle, ok: bool = True, note: str = "") -> None:
        if note:
            if self.styled:
                mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
                self.console.print(f"     {mark} [dim]{_rescape(note)}[/dim]", highlight=False)
            else:
                print(f"     {'ok' if ok else 'x'} {note}", flush=True)

    def result(self, subtype: str, cost) -> None:
        tail = f"  ${cost:.3f}" if isinstance(cost, (int, float)) else ""
        if subtype == "error_during_execution":
            line, style = f"— interrupted —{tail}", "dim"
        else:
            line, style = f"— done —{tail}", "dim"
        if self.styled:
            self.console.print(line, style=style, markup=False, highlight=False)
            self.console.print("")
        else:
            print(line + "\n", flush=True)

    def cost(self, line: str) -> None:
        if self.styled:
            self.console.print(line, style="yellow", markup=False, highlight=False)
        else:
            print(line, flush=True)

    def status(self, **fields) -> None:
        self.status_fields.update({k: v for k, v in fields.items() if v is not None})
        if self.styled and self.status_fields:
            parts = " · ".join(str(v) for v in self.status_fields.values())
            self.console.rule(_rescape(parts), style="dim")
        # plain mode: status bar is a no-op (keeps output byte-identical to before)

    # -- screen -------------------------------------------------------------
    def screen(self, img_b64: str | None, *, media_type: str = "image/png",
               elements_text: str | None = None) -> None:
        if not self.inline or not img_b64:
            return
        try:
            data = base64.b64decode(img_b64)
        except Exception:
            return
        cols = shutil.get_terminal_size((80, 24)).columns
        cell_w = max(10, min(self.screen_max_cells, cols - 2))
        emit_image(data, self.image_proto, cell_width=cell_w)

    # -- input --------------------------------------------------------------
    def prompt(self, label: str = "you> ") -> str:
        # Plain input() even in styled mode: in the claude-cli path the renderer
        # runs on a daemon reader thread while this blocks on the main thread, and
        # plain input avoids any rich/stdin contention.
        return input(label)

    def ask(self, question: str, options: list | None) -> str:
        print(f"\n❓ {question}")
        opts = options or []
        for i, opt in enumerate(opts, 1):
            print(f"   {i}) {opt}")
        try:
            raw = input("   your answer> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "User did not answer."
        if raw.isdigit() and opts and 1 <= int(raw) <= len(opts):
            return f"User chose option {raw}: {opts[int(raw) - 1]}"
        return f"User answered: {raw}" if raw else "User gave no answer."

    def confirm(self, action: str, label: str, keyword: str) -> bool:
        print(f"\n⚠️  About to {action}: \"{label}\"  (matched '{keyword}').")
        try:
            ans = input("   Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")


def make_renderer(*, ui_mode: str = "auto", inline_screen: str = "auto",
                  screen_max_cells: int = 40, status_fields: dict | None = None) -> Renderer:
    """Build a Renderer. Non-TTY output (piped/redirected, e.g. recordings) is
    FORCED to plain with no images regardless of the requested mode."""
    if not sys.stdout.isatty():
        return Renderer(styled=False, image_proto="none", inline_screen=False,
                        screen_max_cells=screen_max_cells, status_fields=status_fields)
    styled = ui_mode in ("auto", "rich") and _HAVE_RICH
    proto = detect_image_protocol()
    inline = inline_screen != "off" and proto != "none"
    return Renderer(styled=styled, image_proto=proto, inline_screen=inline,
                    screen_max_cells=screen_max_cells, status_fields=status_fields)
