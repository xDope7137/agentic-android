"""Guardrails & self-verifying tasks.

A task can carry GUARDRAILS — forbidden states and success assertions — expressed
as structured rules and/or plain English. The monitor checks them after every
screen-changing action (cheap, from the UI tree) and, for natural-language rules,
with an occasional LLM "judge". On a violation it stops, rewinds toward the last
good screen, or asks. At the end it verifies the success assertion and returns a
Verdict (with an exit code for scripting).

Deterministic checks are authoritative and free; the NL judge is advisory and
cost-bounded (off by default except a single check at `done`). This generalizes
the destructive-action gate (device.is_destructive) from "risky taps" to arbitrary
user-defined invariants.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .adb import ADBError
from .device import Device


# --------------------------------------------------------------------------- #
# spec
# --------------------------------------------------------------------------- #
@dataclass
class NumericRule:
    label: str
    op: str          # <= < >= > == !=
    value: float


@dataclass
class GuardrailSet:
    forbid: list[str] = field(default_factory=list)          # forbidden label/id substrings
    stay_in_app: str | None = None                            # foreground package must not change
    numeric: list[NumericRule] = field(default_factory=list)  # labelled-number thresholds (stretch)
    assert_success: str | None = None                         # NL success condition (judged at done)
    forbid_nl: list[str] = field(default_factory=list)        # NL forbidden states (judged)
    on_violation: str = "stop"                                # stop | rewind | ask
    judge_frequency: int = 0                                  # 0 = NL judge only at done
    rewind_budget: int = 3

    def is_empty(self) -> bool:
        return not (self.forbid or self.stay_in_app or self.numeric
                    or self.assert_success or self.forbid_nl)

    def system_prompt_block(self) -> str:
        """A natural-language description for the claude-cli path (advisory)."""
        lines = ["Guardrails for this task — you MUST respect these:"]
        if self.stay_in_app:
            lines.append(f"- Stay inside the app {self.stay_in_app}; do not leave it.")
        for f in self.forbid:
            lines.append(f"- Never tap or trigger anything matching: \"{f}\".")
        for f in self.forbid_nl:
            lines.append(f"- Never reach this state: {f}.")
        for n in self.numeric:
            lines.append(f"- Keep {n.label} {n.op} {n.value}.")
        if self.assert_success:
            lines.append(f"- The task is only successful if: {self.assert_success}.")
        lines.append("Device tools may return a 'GUARDRAIL VIOLATED' message; if so, stop and "
                     "ask the user before doing anything further.")
        return "\n".join(lines)


@dataclass
class Violation:
    kind: str        # forbid | stay_in_app | numeric | forbid_nl
    rule: str
    detail: str


@dataclass
class Verdict:
    success: bool | None
    passed: list[str] = field(default_factory=list)
    failed: list[tuple] = field(default_factory=list)     # (rule, reason)
    violations: list[Violation] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.failed and not self.violations and self.success is not False

    def exit_code(self) -> int:
        return 0 if self.ok() else 3

    def report(self) -> str:
        out = ["=== Guardrail verdict ==="]
        for p in self.passed:
            out.append(f"  PASS  {p}")
        for rule, reason in self.failed:
            out.append(f"  FAIL  {rule}: {reason}")
        for v in self.violations:
            out.append(f"  FAIL  {v.kind}: {v.detail}")
        if self.success is True and not self.failed and not self.violations:
            out.append("  All guardrails held and success was verified.")
        elif not self.passed and not self.failed and not self.violations:
            out.append("  (no guardrails set)")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# deterministic checks (no LLM)
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _to_number(text: str) -> float | None:
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cmp(a: float, op: str, b: float) -> bool:
    return {"<=": a <= b, "<": a < b, ">=": a >= b, ">": a > b,
            "==": a == b, "!=": a != b}.get(op, True)


def check_deterministic(gs: GuardrailSet, elements: list[dict],
                        foreground: str | None) -> list[Violation]:
    vios: list[Violation] = []
    blob = [(e.get("label") or "") + " " + (e.get("id") or "") for e in elements]
    for f in gs.forbid:
        fl = f.lower()
        if any(fl in b.lower() for b in blob):
            vios.append(Violation("forbid", f, f"'{f}' is present on screen"))
    if gs.stay_in_app and foreground and foreground != gs.stay_in_app:
        vios.append(Violation("stay_in_app", gs.stay_in_app,
                              f"left {gs.stay_in_app} (now in {foreground})"))
    for nr in gs.numeric:
        ll = nr.label.lower()
        for e in elements:
            if ll in (e.get("label") or "").lower():
                val = _to_number(e.get("label") or "")
                if val is not None and not _cmp(val, nr.op, nr.value):
                    vios.append(Violation("numeric", f"{nr.label} {nr.op} {nr.value}",
                                          f"{nr.label} = {val}"))
                break
    return vios


# --------------------------------------------------------------------------- #
# NL judge (LLM, cost-bounded)
# --------------------------------------------------------------------------- #
_JUDGE_SYS = ("You are a strict QA judge for a phone automation. You are given the "
              "current screen as a text element list and a list of checks. Answer ONLY "
              "with JSON, no prose, no tool calls: "
              '{"results":[{"id":<int>,"holds":<bool>,"reason":"<short>"}]}. '
              "'holds' = true means the check is satisfied.")


def judge(judge_factory, gs: GuardrailSet, screen_text: str, mode: str):
    """Return (success|None, violations, passed). mode 'done' also checks assert_success."""
    if judge_factory is None:
        return None, [], []
    checks = []  # (id, kind, text)
    if mode == "done" and gs.assert_success:
        checks.append((len(checks), "assert", gs.assert_success))
    for f in gs.forbid_nl:
        checks.append((len(checks), "forbid_nl", f))
    if not checks:
        return None, [], []
    listing = "\n".join(
        f"{i}. {'MUST BE TRUE: ' + t if k == 'assert' else 'MUST BE FALSE: ' + t}"
        for i, k, t in checks)
    prompt = (f"Checks:\n{listing}\n\nCurrent screen:\n{screen_text}\n\n"
              "Return the JSON verdict now.")
    try:
        brain = judge_factory(_JUDGE_SYS)
        brain.start(prompt, None)
        res = brain.step()
        data = _parse_json(res.text)
    except Exception:
        return None, [], []
    if not data:
        return None, [], []
    by_id = {r.get("id"): r for r in data.get("results", []) if isinstance(r, dict)}
    success, vios, passed = None, [], []
    for i, kind, text in checks:
        r = by_id.get(i, {})
        holds = bool(r.get("holds"))
        reason = str(r.get("reason", ""))[:120]
        if kind == "assert":
            success = holds
            (passed if holds else None)
            if holds:
                passed.append(f"assert: {text}")
            else:
                vios.append(Violation("assert", text, reason or "success condition not met"))
        else:  # forbid_nl: holds==True means the forbidden state IS present → violation
            if holds:
                vios.append(Violation("forbid_nl", text, reason or "forbidden state present"))
            else:
                passed.append(f"forbid_nl ok: {text}")
    return success, vios, passed


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# rewind
# --------------------------------------------------------------------------- #
@dataclass
class _Checkpoint:
    sig: str | None
    activity: str | None


class RewindManager:
    """Best-effort 'rewind': press BACK toward the last good screen signature, else
    relaunch its activity. Restores NAVIGATION position only — never undoes a
    committed side effect (so prefer preventing the bad tap in the first place)."""

    def __init__(self, device: Device, budget: int = 3):
        self.device = device
        self.budget = budget
        self._cp: _Checkpoint | None = None

    def capture(self) -> None:
        self._cp = _Checkpoint(sig=self.device._screen_sig(),
                               activity=self.device.foreground_activity())

    def rewind(self) -> bool:
        if self.budget <= 0 or self._cp is None:
            return False
        self.budget -= 1
        for _ in range(6):
            if self.device._screen_sig() == self._cp.sig:
                return True
            try:
                self.device.key("BACK")
            except ADBError:
                break
        if self._cp.activity:
            try:
                self.device.adb.start_activity(self._cp.activity)
                self.device._settle()
            except ADBError:
                pass
        return self.device._screen_sig() == self._cp.sig


# --------------------------------------------------------------------------- #
# monitor (drives the checks from the agent loop)
# --------------------------------------------------------------------------- #
class GuardrailMonitor:
    def __init__(self, gs: GuardrailSet, device: Device, *, judge_factory=None, confirm=None):
        self.gs = gs
        self.device = device
        self.judge_factory = judge_factory
        self.confirm = confirm
        self.rewind_mgr = RewindManager(device, gs.rewind_budget) if gs.on_violation == "rewind" else None
        self.steps = 0
        self.violations: list[Violation] = []
        self.passed: list[str] = []
        if self.rewind_mgr:
            self.rewind_mgr.capture()

    def after_step(self) -> tuple[bool, str | None]:
        """Run checks after one screen-changing action. Returns (stop, note).
        `note` (if any) is fed back to the model; `stop` ends the run."""
        self.steps += 1
        vios = check_deterministic(self.gs, self.device._last_elements,
                                   self.device.foreground_app())
        if self.gs.judge_frequency and self.steps % self.gs.judge_frequency == 0:
            _, nlv, ok = judge(self.judge_factory, self.gs, self.device.ui_elements(), "periodic")
            vios += nlv
            self.passed += ok
        if not vios:
            if self.rewind_mgr:  # this screen is good → make it the new checkpoint
                self.rewind_mgr.capture()
            return False, None
        self.violations += vios
        detail = "; ".join(v.detail for v in vios)
        if self.gs.on_violation == "rewind" and self.rewind_mgr and self.rewind_mgr.rewind():
            return False, (f"GUARDRAIL: {detail}. Rewound to the last good screen — "
                           "take a different path that respects the guardrails.")
        if self.gs.on_violation == "ask" and self.confirm:
            if self.confirm("continue despite guardrail", detail, "guardrail"):
                return False, f"GUARDRAIL: {detail}. The user allowed continuing."
        return True, f"GUARDRAIL VIOLATED: {detail}. Stopping."

    def at_done(self) -> Verdict:
        success, vios, ok = judge(self.judge_factory, self.gs,
                                  self.device.ui_elements(), "done")
        self.violations += vios
        self.passed += ok
        # deterministic rules that never tripped are reported as passed
        if self.gs.stay_in_app and not any(v.kind == "stay_in_app" for v in self.violations):
            self.passed.append(f"stay_in_app: stayed in {self.gs.stay_in_app}")
        for f in self.gs.forbid:
            if not any(v.rule == f for v in self.violations):
                self.passed.append(f"forbid ok: never saw \"{f}\"")
        return Verdict(success=success, passed=self.passed, violations=self.violations)


# --------------------------------------------------------------------------- #
# spec building (CLI / file / config, precedence config < file < CLI)
# --------------------------------------------------------------------------- #
def from_cli(args) -> GuardrailSet:
    gs = GuardrailSet()
    gs.forbid = list(getattr(args, "forbid", None) or [])
    gs.forbid_nl = list(getattr(args, "forbid_nl", None) or [])
    if getattr(args, "assert_success", None):
        gs.assert_success = args.assert_success
    if getattr(args, "stay_in_app", None):
        gs.stay_in_app = args.stay_in_app
    return gs


def from_file(path: str) -> GuardrailSet:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None
    data = {}
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    elif tomllib:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    g = data.get("guardrails", data)
    gs = GuardrailSet()
    gs.forbid = list(g.get("forbid", []) or [])
    gs.forbid_nl = list(g.get("forbid_nl", []) or [])
    gs.assert_success = g.get("assert_success")
    gs.stay_in_app = g.get("stay_in_app")
    gs.numeric = [NumericRule(**n) for n in g.get("numeric", []) if isinstance(n, dict)]
    if g.get("on_violation") in ("stop", "rewind", "ask"):
        gs.on_violation = g["on_violation"]
    if g.get("judge_frequency") is not None:
        gs.judge_frequency = int(g["judge_frequency"])
    if g.get("rewind_budget") is not None:
        gs.rewind_budget = int(g["rewind_budget"])
    return gs


def merge(*sets: GuardrailSet) -> GuardrailSet:
    """Later sets win for scalars; lists accumulate. Pass in precedence order."""
    out = GuardrailSet()
    for s in sets:
        if s is None:
            continue
        out.forbid += [x for x in s.forbid if x not in out.forbid]
        out.forbid_nl += [x for x in s.forbid_nl if x not in out.forbid_nl]
        out.numeric += s.numeric
        out.stay_in_app = s.stay_in_app or out.stay_in_app
        out.assert_success = s.assert_success or out.assert_success
        if s.on_violation != "stop":
            out.on_violation = s.on_violation
        if s.judge_frequency:
            out.judge_frequency = s.judge_frequency
        if s.rewind_budget != 3:
            out.rewind_budget = s.rewind_budget
    return out
