"""Skill compiler: learn a task once, then replay it for ~free.

The first time a task runs, the LLM agent does it normally and a `SkillRecorder`
captures each step (action + a robust target + a verification checkpoint). On
success the trace is distilled into a deterministic, self-healing **skill** saved
to `data/skills/<slug>.json`. Replaying it (`SkillRunner`) drives the device with
zero LLM calls on the happy path; if a step's screen no longer matches (the UI
drifted), it heals just that step with the LLM and rewrites the skill. It also
mines an `am start -n <component>` shortcut to jump past navigation when possible.

The replay engine is a pure `Device` driver — it reuses the same primitives the
live agent uses (`resolve_element`, `tap_element`, `type_text`, `_screen_sig`,
`observe`), so a skill is essentially a recorded sequence of Device calls.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

from .adb import ADBError
from .config import PROJECT_ROOT
from .device import Device, is_destructive

# tools that can appear as a replayable step (subset of the live tool set)
_REPLAYABLE = {"tap", "tap_element", "swipe", "type_text", "press_key", "launch_app"}
# fields whose typed value may be a secret — never persisted verbatim
_SECRET_HINT = re.compile(r"(otp|pin|password|passcode|cvv|card|secret|2fa|code)", re.I)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
@dataclass
class Checkpoint:
    post_sig: str | None = None
    expect_text: str | None = None
    expect_id: str | None = None
    match: str = "element_or_sig"


@dataclass
class Step:
    i: int
    intent: str
    action: str
    args: dict = field(default_factory=dict)
    target: dict | None = None            # {text,id,label} for tap_element
    fallback_coord: dict | None = None    # {cx,cy} device px
    pre_sig: str | None = None
    checkpoint: Checkpoint | None = None
    replayable: bool = True


@dataclass
class Shortcut:
    component: str | None = None
    lands_at_step: int = 0
    expect_text: str | None = None
    expect_id: str | None = None


@dataclass
class Skill:
    name: str
    slug: str
    task: str
    created_at: str = ""
    device_profile: dict = field(default_factory=dict)
    shortcut: Shortcut | None = None
    steps: list[Step] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        steps = [Step(**{**s, "checkpoint": Checkpoint(**s["checkpoint"]) if s.get("checkpoint") else None})
                 for s in d.get("steps", [])]
        sc = d.get("shortcut")
        return cls(
            name=d["name"], slug=d["slug"], task=d.get("task", ""),
            created_at=d.get("created_at", ""), device_profile=d.get("device_profile", {}),
            shortcut=Shortcut(**sc) if sc else None, steps=steps,
            schema_version=d.get("schema_version", 1),
        )

    def save(self, path: str | None = None) -> str:
        path = path or os.path.join(skills_dir(), f"{self.slug}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "Skill":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def skills_dir() -> str:
    d = os.path.join(PROJECT_ROOT, "data", "skills")
    os.makedirs(d, exist_ok=True)
    return d


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "skill"


def list_skills() -> list[Skill]:
    out = []
    if not os.path.isdir(skills_dir()):
        return out
    for fn in sorted(os.listdir(skills_dir())):
        if fn.endswith(".json"):
            try:
                out.append(Skill.load(os.path.join(skills_dir(), fn)))
            except Exception:
                pass
    return out


def find_skill(name: str) -> Skill | None:
    slug = slugify(name)
    p = os.path.join(skills_dir(), f"{slug}.json")
    if os.path.isfile(p):
        return Skill.load(p)
    for sk in list_skills():  # fall back to task substring
        if name.lower() in sk.task.lower() or name.lower() in sk.name.lower():
            return sk
    return None


# --------------------------------------------------------------------------- #
# recording (path A — attached to the live agent)
# --------------------------------------------------------------------------- #
class SkillRecorder:
    def __init__(self, device: Device):
        self.device = device
        self.steps: list[Step] = []
        self._pre_sig: str | None = None
        self._target: dict | None = None
        self._coord: dict | None = None
        self._last_intent: str = ""
        self.success = False

    def note_intent(self, text: str) -> None:
        if text:
            self._last_intent = text.strip()[:200]

    def before(self, name: str, args: dict) -> None:
        self._pre_sig = self.device._screen_sig()
        self._target = self._coord = None
        if name == "tap_element":
            try:
                t = self.device.resolve_element(index=args.get("index"), text=args.get("text"))
                self._target = {"text": t.get("label"), "id": t.get("id"), "label": t.get("label")}
                self._coord = {"cx": t["cx"], "cy": t["cy"]}
            except ADBError:
                pass

    def after(self, name: str, args: dict, ok: bool) -> None:
        if not ok or name not in _REPLAYABLE:
            return
        safe_args = dict(args)
        replayable = True
        if name == "type_text" and self._typed_is_secret():
            safe_args = {"text": ""}        # never store secrets
            replayable = False
        expect = self._salient_element()
        self.steps.append(Step(
            i=len(self.steps), intent=self._last_intent or f"{name}", action=name,
            args=safe_args, target=self._target, fallback_coord=self._coord,
            pre_sig=self._pre_sig,
            checkpoint=Checkpoint(post_sig=self.device._screen_sig(),
                                  expect_text=expect.get("text"), expect_id=expect.get("id")),
            replayable=replayable,
        ))

    def on_done(self, args: dict) -> None:
        self.success = bool(args.get("success", True))

    def _typed_is_secret(self) -> bool:
        # If the focused field's label/id hints at a secret, redact.
        for e in self.device._last_elements:
            if e.get("clickable") and _SECRET_HINT.search((e.get("label") or "") + (e.get("id") or "")):
                return True
        return False

    def _salient_element(self) -> dict:
        els = self.device._last_elements
        with_id = [e for e in els if e.get("id")]
        if with_id:
            e = with_id[0]
            return {"text": None, "id": e["id"]}
        labelled = [e for e in els if e.get("label") and len(e["label"]) <= 30]
        if labelled:
            return {"text": labelled[0]["label"], "id": None}
        return {"text": None, "id": None}

    def finalize(self, name: str, task: str, created_at: str) -> Skill:
        prof = {}
        try:
            shot = self.device.screenshot()
            prof = {"screen_w": shot["width"], "screen_h": shot["height"]}
        except Exception:
            pass
        sk = Skill(name=name, slug=slugify(name), task=task, created_at=created_at,
                   device_profile=prof, steps=self.steps)
        sk.shortcut = mine_shortcut(self.device, self.steps)
        return sk


def mine_shortcut(device: Device, steps: list[Step]) -> Shortcut | None:
    """Capture the current top activity as an `am start -n` jump that skips
    navigation, landing after the launch_app step (best-effort)."""
    comp = device.foreground_activity()
    if not comp:
        return None
    lands = 0
    for s in steps:  # resume right after the initial app launch the shortcut subsumes
        if s.action == "launch_app":
            lands = s.i + 1
    expect = steps[-1].checkpoint if steps and steps[-1].checkpoint else None
    return Shortcut(component=comp, lands_at_step=lands,
                    expect_text=expect.expect_text if expect else None,
                    expect_id=expect.expect_id if expect else None)


# --------------------------------------------------------------------------- #
# replay (deterministic + self-heal)
# --------------------------------------------------------------------------- #
class SkillRunner:
    def __init__(self, device: Device, skill: Skill, *, brain_factory=None,
                 heal: bool = True, use_shortcut: bool = True, log=print):
        self.device = device
        self.skill = skill
        self.brain_factory = brain_factory
        self.heal = heal and brain_factory is not None
        self.use_shortcut = use_shortcut
        self.log = log
        self.healed = 0

    def run(self) -> str:
        start = self._try_shortcut() if (self.use_shortcut and self.skill.shortcut) else 0
        for step in self.skill.steps[start:]:
            if step.action == "done":
                continue
            self.log(f"   · {step.action} {json.dumps(step.args, separators=(',', ':'))}")
            try:
                self._execute(step)
            except ADBError as e:
                if not self._heal_step(step, reason=f"action failed: {e}"):
                    return f"Stopped at step {step.i} ({step.intent}): {e}"
                continue
            if step.checkpoint and not self._verify(step.checkpoint):
                if not self._heal_step(step, reason="screen didn't match the checkpoint"):
                    return f"Stopped at step {step.i}: screen didn't match and healing failed/disabled."
        return f"Replayed skill '{self.skill.name}' ({len(self.skill.steps)} steps, {self.healed} healed)."

    def _try_shortcut(self) -> int:
        sc = self.skill.shortcut
        try:
            self.device.adb.start_activity(sc.component)
            self.device._settle()
        except ADBError:
            return 0
        cp = Checkpoint(post_sig=None, expect_text=sc.expect_text, expect_id=sc.expect_id)
        if (sc.expect_text or sc.expect_id) and self._verify(cp):
            self.log(f"   · shortcut am-start {sc.component} → resume @ step {sc.lands_at_step}")
            return max(0, sc.lands_at_step)
        return 0  # didn't land — fall through to step-by-step

    def _execute(self, step: Step) -> None:
        d = self.device
        a = step.action
        if a == "tap_element":
            sel = (step.target or {})
            try:
                d.tap_element(text=sel.get("text") or sel.get("id"))
            except ADBError:
                if step.fallback_coord:
                    d.adb.tap(step.fallback_coord["cx"], step.fallback_coord["cy"])
                    d._settle()
                else:
                    raise
        elif a == "tap":
            d.adb.tap(step.args["x"], step.args["y"])
            d._settle()
        elif a == "swipe":
            d.swipe(step.args["x1"], step.args["y1"], step.args["x2"], step.args["y2"],
                    step.args.get("duration_ms", 300))
        elif a == "type_text":
            if not step.replayable or not step.args.get("text"):
                raise ADBError("this step typed a secret and can't be replayed; rerun live")
            d.type_text(step.args["text"])
        elif a == "press_key":
            d.key(step.args["key"])
        elif a == "launch_app":
            d.launch_app(step.args["package"])
        else:
            raise ADBError(f"unknown replay action {a!r}")

    def _verify(self, cp: Checkpoint) -> bool:
        self.device.ui_elements()  # refresh _last_elements
        if cp.post_sig and self.device._screen_sig() == cp.post_sig:
            return True
        if cp.expect_text or cp.expect_id:
            try:
                self.device.resolve_element(text=cp.expect_text or cp.expect_id)
                return True
            except ADBError:
                return False
        return cp.post_sig is None  # nothing to check → pass

    def _heal_step(self, step: Step, reason: str) -> bool:
        if not self.heal:
            return False
        self.log(f"   ↻ healing step {step.i} ({reason})…")
        from .agent import build_system_prompt
        from .tools import TOOLS
        brain = self.brain_factory(build_system_prompt(5, vision=True))
        o = self.device.observe(vision=True)
        brain.start(
            f"You are repairing ONE step of an automation. Goal of this step: "
            f"{step.intent}.\nThe screen no longer matches what was recorded ({reason}).\n"
            f"Current screen:\n{o['text']}\n\nIssue exactly one tool call to accomplish "
            f"the step's goal on THIS screen, then call done.",
            o["image"],
        )
        for _ in range(3):
            res = brain.step()
            calls = [c for c in res.tool_calls if c.name in _REPLAYABLE]
            if not calls:
                if any(c.name == "done" for c in res.tool_calls):
                    break
                continue
            c = calls[0]
            try:
                self._execute_raw(c.name, c.args)
            except ADBError:
                return False
            # adopt the repaired action into the skill and persist
            step.action, step.args = c.name, dict(c.args)
            if c.name == "tap_element":
                try:
                    t = self.device.resolve_element(index=c.args.get("index"), text=c.args.get("text"))
                    step.target = {"text": t.get("label"), "id": t.get("id"), "label": t.get("label")}
                    step.fallback_coord = {"cx": t["cx"], "cy": t["cy"]}
                except ADBError:
                    pass
            step.checkpoint = Checkpoint(post_sig=self.device._screen_sig(),
                                         **self._salient_for(step))
            self.skill.save()
            self.healed += 1
            return True
        return False

    def _execute_raw(self, name: str, args: dict) -> None:
        self._execute(Step(i=-1, intent="heal", action=name, args=dict(args),
                           target={"text": args.get("text")} if name == "tap_element" else None,
                           fallback_coord=None))

    def _salient_for(self, step: Step) -> dict:
        els = self.device._last_elements
        e = next((x for x in els if x.get("id")), None) or next(
            (x for x in els if x.get("label")), None)
        return {"expect_text": (e or {}).get("label") if e and not e.get("id") else None,
                "expect_id": (e or {}).get("id")}


# --------------------------------------------------------------------------- #
# import a claude-cli debug trace (path B) into the same schema (best-effort)
# --------------------------------------------------------------------------- #
class SkillImporter:
    @staticmethod
    def from_jsonl(path: str, name: str | None = None, task: str | None = None) -> Skill:
        steps: list[Step] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "assistant":
                    continue
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") != "tool_use":
                        continue
                    tool = b.get("name", "").split("__")[-1]
                    if tool not in _REPLAYABLE:
                        continue
                    inp = b.get("input", {}) or {}
                    target = None
                    if tool == "tap_element":
                        target = {"text": inp.get("text"), "id": None, "label": inp.get("text")}
                    steps.append(Step(i=len(steps), intent=tool, action=tool, args=inp,
                                      target=target, replayable=tool != "type_text" or bool(inp.get("text"))))
        nm = name or (task or os.path.splitext(os.path.basename(path))[0])
        return Skill(name=nm, slug=slugify(nm), task=task or nm, steps=steps)
