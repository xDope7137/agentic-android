"""Session debug recorder: saves every API request/response to a JSONL file.

Enabled with --debug (or [agent] debug = true). One file per session under
debug/session-<timestamp>.jsonl. Base64 image data is redacted to a size marker
so the file stays readable; everything else (incl. the text-mode element list
and token-usage in responses) is kept verbatim.
"""

from __future__ import annotations

import json
import os
from datetime import datetime


def _redact(obj):
    """Deep-copy, replacing only base64/image blobs with a short marker."""
    if isinstance(obj, str):
        if obj.startswith("data:") and len(obj) > 200:
            return f"<image data: {len(obj)} chars, {obj[:24]}…>"
        if len(obj) > 1000 and " " not in obj and "\n" not in obj:
            return f"<base64-ish: {len(obj)} chars>"
        return obj
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def new_session_path(debug_dir: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(debug_dir, f"session-{ts}.jsonl")


class SessionRecorder:
    def __init__(self, path: str):
        self.path = path
        self.seq = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _write(self, rec: dict) -> None:
        rec["time"] = datetime.now().isoformat(timespec="seconds")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def request(self, provider: str, payload: dict) -> int:
        self.seq += 1
        self._write({"seq": self.seq, "type": "request", "provider": provider,
                     "payload": _redact(payload)})
        return self.seq

    def response(self, seq: int, payload) -> None:
        self._write({"seq": seq, "type": "response", "payload": _redact(payload)})

    def event(self, data: str) -> None:
        """Raw line (e.g. claude-cli stream-json)."""
        self._write({"type": "event", "data": data})
