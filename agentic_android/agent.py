"""Provider-agnostic agent loop. Drives a `Brain` (Anthropic or OpenAI) that
decides actions; this class executes them on the device and feeds results
(including screenshots) back to the brain.
"""

from __future__ import annotations

import json

from .adb import ADBError
from .brains import Brain
from .config import persistence_block
from .device import Device, is_destructive
from .guardrails import GuardrailMonitor
from .tools import SCREEN_CHANGING
from .ui import Renderer, make_renderer

MODEL = "claude-opus-4-8"  # default Anthropic model

_BASE_PROMPT = """\
You are Agentic Android, an agent that operates a real Android device. You act with \
tools: tap_element, tap, swipe, type_text, press_key, launch_app, list_apps, \
dump_ui, screenshot, ask_user, done.

Be autonomous and think for yourself. You are ALWAYS shown the current screen \
(see "How you see the screen" below) — at the start and again automatically \
after every action. So when the user asks what's on screen, to read messages, \
or to analyse/answer something about the current view, just READ the screen you \
already have and reason it out. Do NOT call screenshot (or any tool) merely to \
"see" or "capture" the screen — you already have it. Give a complete, direct \
answer instead of narrating that you'll look. Only use tools to change the \
screen (navigate/tap/type) or to fetch info you genuinely don't have yet.

How to work:
- TO TAP, prefer `tap_element` with the element's #index from the list (or its \
text) — it's far more reliable than computing raw tap coordinates. Use plain \
`tap` only for a spot with no listed element.
- To open "the Nth <thing>" (e.g. the 2nd conversation, the 3rd result): find \
the matching rows in the element list, count them in order, and tap_element the \
right one. If it's not visible, swipe up to scroll and look again — don't tap a \
random coordinate and hope.
- Tap a text field to focus it before type_text. To scroll down, swipe up \
(large y to small y).
- Don't guess package names for launch_app — call list_apps (optionally with a \
filter like 'clock') to find the real package. If nothing matches, the app \
isn't installed (say so, or install it from the Play Store).
- press_key BACK to dismiss dialogs; HOME for the launcher.

DO NOT take any action that commits or sends something, unless the user \
EXPLICITLY told you to in this task. That includes: sending a message/DM, \
posting/commenting, liking, following/unfollowing, buying/installing, deleting, \
or signing in. If the user asks you to "suggest", "draft", "write", or "find" a \
reply/post, just PRODUCE the text in your answer — do NOT type or send it. When \
unsure whether an action is wanted, stop and ask_user first.

When you have the answer or the task is done, give a clear final reply and call \
done() with a short summary. Don't pad with narration like "let me look" — act, \
then answer.\
"""

_PERCEPTION_VISION = """\
The current screen is provided to you as a screenshot — at the start and \
automatically after every action — so you can always see it without asking. \
Coordinates are pixels of that image (top-left origin); tap the visual centre \
of a target. Call screenshot ONLY to refresh after something changed the screen \
without returning a new image; call dump_ui when a target is too small or \
ambiguous to read exact bounds/ids.\
"""

_PERCEPTION_TEXT = """\
The current screen is given to you as a numbered list of on-screen elements — \
each with its type, label/text, an optional id, a tap point @(x,y), and flags \
(tap=clickable, input=text field, scroll=scrollable). You receive this list at \
the start and automatically after every action, so to read messages, counts, \
prices, or anything visible, just read the list you already have. To act on an \
element, call tap_element with its #index (e.g. index 5 for "#5") — do not do \
coordinate math. Call screenshot ONLY to refresh the list after a change that \
didn't return a new one; call dump_ui for the raw hierarchy if a needed detail \
is missing from the list.\
"""


def build_system_prompt(effort: int, vision: bool = True) -> str:
    perception = _PERCEPTION_VISION if vision else _PERCEPTION_TEXT
    return (
        _BASE_PROMPT + "\n\nHow you see the screen:\n" + perception
        + "\n\nPersistence & asking:\n" + persistence_block(effort, "tool")
    )


def _compact(d: dict) -> str:
    s = json.dumps(d, separators=(",", ":"))
    return s if len(s) <= 80 else s[:77] + "…"


class AgenticAndroid:
    def __init__(self, brain: Brain, device: Device | None = None,
                 max_steps: int = 40, verbose: bool = True, vision: bool = True,
                 pricing: dict | None = None, confirm_destructive: bool = False,
                 ui: Renderer | None = None, guardrails=None, judge_factory=None):
        self.brain = brain
        self.device = device or Device()
        self.max_steps = max_steps
        self.verbose = verbose
        self.vision = vision  # False = feed the screen as a text element list
        self.pricing = pricing or {}
        self.confirm_destructive = confirm_destructive
        self.ui = ui or make_renderer()
        self.recorder = None  # a skills.SkillRecorder when recording a skill
        self.monitor = (GuardrailMonitor(guardrails, self.device, judge_factory=judge_factory,
                                         confirm=self._confirm)
                        if guardrails is not None and not guardrails.is_empty() else None)
        self.verdict = None   # set to a guardrails.Verdict when guardrails are active

    def _print_cost(self) -> None:
        u = getattr(self.brain, "usage", None)
        if not u or not (u["input"] or u["output"]):
            return
        from .config import estimate_cost

        c = estimate_cost(self.brain.model, u["input"], u["output"], u["cached"], self.pricing)
        money = f"${c:.4f}" if c is not None else f"(no price set for {self.brain.model})"
        cached = f", {u['cached']:,} cached" if u["cached"] else ""
        if self.verbose:
            self.ui.cost(f"[cost] {self.brain.model}: {u['input']:,} in{cached} "
                         f"+ {u['output']:,} out  →  {money}")

    def _observe(self) -> tuple[str, str | None]:
        """The current screen as the model perceives it: (text, image_b64|None).

        `device.observe` adds the safety net: in vision mode, if the screenshot
        comes back blank/black, the text becomes the UI element list so the model
        isn't stranded on a dead image (the image still rides along)."""
        o = self.device.observe(self.vision)
        return o["text"], o["image"]

    def _log(self, *args) -> None:
        if self.verbose:
            print(*args, flush=True)

    # -- tool execution -----------------------------------------------------

    def _ask_user(self, question: str, options: list | None) -> str:
        return self.ui.ask(question, options)

    # -- destructive-action gate -------------------------------------------

    def _confirm(self, action: str, label: str, keyword: str) -> bool:
        """Ask the operator to approve a high-risk action. Returns True to proceed."""
        return self.ui.confirm(action, label, keyword)

    def _gate_element(self, target: dict) -> str | None:
        """Block message if a tap_element target is destructive and the user declines."""
        if not self.confirm_destructive:
            return None
        label = target.get("label") or target.get("id") or ""
        kw = is_destructive(label, self.device.destructive_keywords)
        if kw and not self._confirm("tap", label, kw):
            return (f"Blocked: '{label}' is a destructive action and the user declined. "
                    "Try a different approach or ask the user what to do instead.")
        return None

    def _gate_tap(self, x: int, y: int) -> str | None:
        """Best-effort gate for raw coordinate taps: if the tap lands on a known
        destructive element, confirm first. No-op when nothing is nearby."""
        if not self.confirm_destructive:
            return None
        els = self.device._last_elements
        if not els:
            return None
        dx, dy = self.device._to_device(x, y)
        near = min(els, key=lambda e: (e["cx"] - dx) ** 2 + (e["cy"] - dy) ** 2)
        if (near["cx"] - dx) ** 2 + (near["cy"] - dy) ** 2 > 75 * 75:
            return None  # not clearly on a listed element
        label = near.get("label") or ""
        kw = is_destructive(label, self.device.destructive_keywords)
        if kw and not self._confirm("tap", label, kw):
            return (f"Blocked: tapping near '{label}' looks destructive and the user "
                    "declined. Try a different approach or ask the user.")
        return None

    def _execute(self, name: str, args: dict) -> tuple[str, str | None]:
        """Run one tool. Returns (text, screenshot_b64 | None)."""
        note = ""
        try:
            if name == "tap":
                blocked = self._gate_tap(args["x"], args["y"])
                if blocked:
                    return blocked, None
                self.device.tap(args["x"], args["y"])
                note = f"Tapped ({args['x']}, {args['y']})."
            elif name == "tap_element":
                target = self.device.resolve_element(index=args.get("index"), text=args.get("text"))
                blocked = self._gate_element(target)
                if blocked:
                    return blocked, None
                el = self.device.tap_element(index=args.get("index"), text=args.get("text"))
                note = f"Tapped element #{el['index']} {el.get('label') or el.get('id') or ''!r}."
            elif name == "swipe":
                self.device.swipe(args["x1"], args["y1"], args["x2"], args["y2"], args.get("duration_ms", 300))
                note = "Swiped."
            elif name == "type_text":
                self.device.type_text(args["text"])
                note = f"Typed: {args['text']!r}."
            elif name == "press_key":
                self.device.key(args["key"])
                note = f"Pressed {args['key']}."
            elif name == "launch_app":
                self.device.launch_app(args["package"])
                note = f"Launched {args['package']}."
            elif name == "screenshot":
                note = "Current screen:"
            elif name == "dump_ui":
                xml = self.device.ui_xml()
                return (xml[:40000] + "\n…[truncated]" if len(xml) > 40000 else xml), None
            elif name == "list_apps":
                return self.device.list_apps(args.get("filter"), args.get("launchable_only", True)), None
            elif name == "ask_user":
                return self._ask_user(args.get("question", ""), args.get("options")), None
            else:
                return f"Unknown tool {name}", None
        except ADBError as exc:
            return f"ADB error running {name}: {exc}", None

        if name in SCREEN_CHANGING:
            obs_text, image = self._observe()
            return f"{note}\n{obs_text}", image
        return note or "ok", None

    # -- loop ---------------------------------------------------------------

    def _run_turn(self) -> str:
        """Step the brain until it stops calling tools (or calls done)."""
        last_text = ""
        for _ in range(self.max_steps):
            res = self.brain.step()
            if res.text:
                last_text = res.text
                if self.recorder:
                    self.recorder.note_intent(res.text)
                if self.verbose:
                    self.ui.agent_text(res.text)
            if not res.tool_calls:
                return last_text

            results = []
            finished = None
            stop_guardrail = False
            for c in res.tool_calls:
                if c.name == "done":
                    finished = c.args.get("summary", "") or last_text
                    if self.recorder:
                        self.recorder.on_done(c.args)
                    if self.monitor and self.verdict is None:
                        self.verdict = self.monitor.at_done()
                    results.append((c.id, "Acknowledged.", None))
                    continue
                handle = self.ui.tool_start(c.name, c.args) if self.verbose else None
                if self.recorder:
                    self.recorder.before(c.name, c.args)
                text, image = self._execute(c.name, c.args)
                ok = not text.startswith(("ADB error", "Blocked"))
                if self.recorder:
                    self.recorder.after(c.name, c.args, ok=ok)
                if self.monitor and c.name in SCREEN_CHANGING:
                    stop, note = self.monitor.after_step()
                    if note:
                        text = f"{text}\n{note}"
                    stop_guardrail = stop_guardrail or stop
                if self.verbose:
                    self.ui.tool_end(handle, ok=ok)
                    if image and c.name in SCREEN_CHANGING:
                        self.ui.screen(image)  # inline phone screenshot (if supported)
                results.append((c.id, text, image))
            self.brain.add_tool_results(results)
            if stop_guardrail:
                if self.monitor and self.verdict is None:
                    self.verdict = self.monitor.at_done()
                return last_text or "Stopped: a guardrail was violated."
            if finished is not None:
                return finished
        return last_text or "Stopped: reached the maximum number of steps."

    def run(self, task: str) -> str:
        """One-shot: run a single task to completion."""
        obs_text, image = self._observe()
        self.brain.start(f"Task: {task}\n\nCurrent screen:\n{obs_text}\n\nBegin.", image)
        result = self._run_turn()
        self._print_cost()
        return result

    def chat(self) -> None:
        """Interactive: one ongoing conversation; each of your messages is sent
        with a fresh screenshot, the agent acts, then control returns to you."""
        self.ui.status(brain=self.brain.label, model=getattr(self.brain, "model", ""),
                       vision="image" if self.vision else "text")
        self.ui.banner(f"\n[chat ready · brain {self.brain.label}]")
        self.ui.banner("Type a task (e.g. 'Install WhatsApp on my phone'). Ctrl-D or 'exit' to quit.\n")
        first = True
        while True:
            try:
                line = self.ui.prompt("you> ")
            except EOFError:
                break
            if line.strip().lower() in {"exit", "quit", ":q"}:
                break
            if not line.strip():
                continue
            obs_text, image = self._observe()
            msg = f"{line}\n\nCurrent screen:\n{obs_text}"
            if first:
                self.brain.start(msg, image)
                first = False
            else:
                self.brain.add_user(msg, image)
            try:
                self._run_turn()
            except KeyboardInterrupt:
                self.ui.info("\n[interrupted — back to you]")
            except Exception as exc:  # keep the chat alive on API/network errors
                self.ui.error(f"\n[error from {self.brain.label}: {type(exc).__name__}: {exc}]")
            self._print_cost()
