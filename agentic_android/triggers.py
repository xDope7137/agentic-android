"""Notification triggers: wake the agent when the phone gets a notification.

A **trigger** binds a notification source (an app package, optionally narrowed by
keyword/regex on the title/text) to a **task** the agent should perform. Triggers
are saved as JSON under `data/triggers/<slug>.json` (mirroring `skills.py`).

While an interactive chat session is open, a `NotificationWatcher` polls the
device's posted notifications on an interval; when one matches an enabled trigger
it calls `on_fire(trigger, notif)` — chat.py wires that to inject the task into
the live agent, exactly like a typed message. The watcher de-dupes so the same
notification never fires twice (even across restarts) and primes itself on the
first poll so a backlog of old notifications doesn't cause a wake-up storm.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import PROJECT_ROOT, load_config

_FIRED_KEYS_CAP = 200  # how many recent fire-identities to remember per trigger


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    name: str
    slug: str
    task: str                       # what the agent should do when this fires
    package: str                    # source app, e.g. com.whatsapp
    title_contains: str = ""        # case-insensitive substring on the title
    text_contains: str = ""         # case-insensitive substring on text/big_text
    pattern: str = ""               # optional regex over "title\ntext"
    enabled: bool = False           # user must confirm before it goes live
    created_at: str = ""
    last_fired_at: float = 0.0
    min_interval_s: float = 30.0    # never fire more often than this, per trigger
    fired_keys: list[str] = field(default_factory=list)  # dedupe ledger (capped)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trigger":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: str | None = None) -> str:
        if len(self.fired_keys) > _FIRED_KEYS_CAP:
            self.fired_keys = self.fired_keys[-_FIRED_KEYS_CAP:]
        path = path or os.path.join(triggers_dir(), f"{self.slug}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "Trigger":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def triggers_dir() -> str:
    """Directory holding trigger JSON files (configurable via [triggers] dir)."""
    d = load_config().triggers_dir
    if not os.path.isabs(d):
        d = os.path.join(PROJECT_ROOT, d)
    os.makedirs(d, exist_ok=True)
    return d


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "trigger"


def list_triggers() -> list[Trigger]:
    out: list[Trigger] = []
    d = triggers_dir()
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            try:
                out.append(Trigger.load(os.path.join(d, fn)))
            except Exception:
                pass
    return out


def find_trigger(name: str) -> Trigger | None:
    p = os.path.join(triggers_dir(), f"{slugify(name)}.json")
    if os.path.isfile(p):
        return Trigger.load(p)
    for t in list_triggers():  # fall back to name/task substring
        if name.lower() in t.name.lower() or name.lower() in t.task.lower():
            return t
    return None


def delete_trigger(name: str) -> bool:
    t = find_trigger(name)
    if not t:
        return False
    p = os.path.join(triggers_dir(), f"{t.slug}.json")
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def matches(trigger: Trigger, notif: dict) -> bool:
    """True if `notif` should fire `trigger`. Requires the package to match; then
    ANDs whichever of title_contains / text_contains / pattern are set. With none
    set, any notification from the package matches."""
    if (notif.get("package") or "") != trigger.package:
        return False
    title = notif.get("title") or ""
    text = notif.get("text") or ""
    big = notif.get("big_text") or ""
    body = f"{text}\n{big}"
    if trigger.title_contains and trigger.title_contains.lower() not in title.lower():
        return False
    if trigger.text_contains and trigger.text_contains.lower() not in body.lower():
        return False
    if trigger.pattern:
        try:
            if not re.search(trigger.pattern, f"{title}\n{body}", re.I):
                return False
        except re.error:
            return False  # a bad regex never matches (and never crashes the poll)
    return True


def _fire_identity(notif: dict) -> str:
    """A stable id for one delivered notification: its key plus a content hash, so
    an UPDATED notification under a reused key can fire again, but the identical
    one re-seen on every poll cannot. Only the hash is persisted — never raw text
    (notifications may carry OTPs/secrets)."""
    content = f"{notif.get('title', '')}\x00{notif.get('text', '')}\x00{notif.get('big_text', '')}"
    h = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{notif.get('key', '')}:{h}"


# --------------------------------------------------------------------------- #
# watcher
# --------------------------------------------------------------------------- #
class NotificationWatcher:
    """Polls device notifications and fires matching, enabled triggers.

    `on_fire(trigger, notif)` is called once per new matching notification.
    `triggers=None` reloads enabled triggers from disk every poll, so a trigger
    the live agent just created is picked up without a restart.
    """

    def __init__(self, adb, on_fire, triggers: list[Trigger] | None = None,
                 poll_interval_s: float = 8.0, cooldown_s: float = 15.0, log=print):
        self.adb = adb
        self.on_fire = on_fire
        self._triggers = triggers
        self.poll_interval_s = max(1.0, float(poll_interval_s))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._primed: set[str] = set()  # trigger slugs whose current backlog was seeded
        self._suppress_until = 0.0  # after a fire, swallow the burst it may cause

    def _enabled_triggers(self) -> list[Trigger]:
        src = self._triggers if self._triggers is not None else list_triggers()
        return [t for t in src if t.enabled]

    def poll_once(self) -> int:
        """One polling pass. Returns the number of triggers fired this pass."""
        triggers = self._enabled_triggers()
        if not triggers:
            return 0
        try:
            notifs = self.adb.notifications()
        except Exception as exc:  # never let a flaky poll kill the thread
            self.log(f"[triggers] poll failed: {exc}")
            return 0

        now = time.time()
        suppressing = now < self._suppress_until
        fired = 0
        for t in triggers:
            # Prime each trigger the first time we see it (existing ones on the
            # first poll, a freshly-created one on the poll after): seed its current
            # backlog as "seen" so only FUTURE notifications wake the agent. The
            # currently-showing notification is handled by the agent directly.
            priming = t.slug not in self._primed
            dirty = False
            for n in notifs:
                if not matches(t, n):
                    continue
                ident = _fire_identity(n)
                if ident in t.fired_keys:
                    continue
                if priming or suppressing:
                    t.fired_keys.append(ident)  # mark seen, don't fire
                    dirty = True
                    continue
                if now - t.last_fired_at < t.min_interval_s:
                    continue
                t.fired_keys.append(ident)
                t.last_fired_at = now
                dirty = True
                self._suppress_until = now + self.cooldown_s
                fired += 1
                try:
                    self.on_fire(t, n)
                except Exception as exc:
                    self.log(f"[triggers] on_fire error for {t.name!r}: {exc}")
            self._primed.add(t.slug)
            if dirty:
                t.save()
        return fired

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self.log(f"[triggers] watcher cycle error: {exc}")
            self._stop.wait(self.poll_interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
