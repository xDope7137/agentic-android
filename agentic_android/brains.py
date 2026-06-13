"""Provider backends ("brains") for the autonomous agent.

A Brain owns the conversation history in its provider's native format and
exposes a small, uniform interface so `agent.py` doesn't care which API is used:

    brain.start(text, image)      seed the first user message
    brain.add_user(text, image)   append a user turn (interactive chat)
    result = brain.step()         call the API; returns text + tool calls
    brain.add_tool_results(rs)    feed tool outputs back (rs: (call_id, text, image|None))

Supported: Anthropic (`messages` API) and OpenAI / OpenAI-compatible
(`chat.completions`, with a custom base_url). The one real difference the brains
hide: Anthropic can return an image inside a tool_result, OpenAI cannot — so the
OpenAI brain sends the post-action screenshot as a follow-up user image message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .tools import TOOLS


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class StepResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


# A tool result: (call_id, text, image_b64 | None)
ToolResult = tuple


def to_openai_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


class Brain:
    label = "brain"

    def start(self, text: str, image_b64: str | None = None) -> None:
        raise NotImplementedError

    def add_user(self, text: str, image_b64: str | None = None) -> None:
        raise NotImplementedError

    def step(self) -> StepResult:
        raise NotImplementedError

    def add_tool_results(self, results: list[ToolResult]) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
class AnthropicBrain(Brain):
    label = "anthropic"

    def __init__(self, model: str, system: str, api_key: str | None = None,
                 base_url: str | None = None, max_tokens: int = 8000, recorder=None,
                 api_timeout: float = 120.0, api_retries: int = 2):
        import anthropic

        kwargs: dict = {"timeout": api_timeout, "max_retries": api_retries}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        self.recorder = recorder
        self.usage = {"input": 0, "output": 0, "cached": 0}
        self.history: list[dict] = []

    @staticmethod
    def _img(b64: str) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}

    def _user(self, text: str, image_b64: str | None) -> dict:
        content = [{"type": "text", "text": text}]
        if image_b64:
            content.append(self._img(image_b64))
        return {"role": "user", "content": content}

    def start(self, text: str, image_b64: str | None = None) -> None:
        self.history = [self._user(text, image_b64)]

    def add_user(self, text: str, image_b64: str | None = None) -> None:
        self.history.append(self._user(text, image_b64))

    def step(self) -> StepResult:
        payload = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            tools=TOOLS,
            thinking={"type": "adaptive"},
            messages=self.history,
        )
        seq = self.recorder.request("anthropic", payload) if self.recorder else None
        resp = self.client.messages.create(**payload)
        if self.recorder:
            self.recorder.response(seq, resp.model_dump())
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.usage["input"] += (u.input_tokens or 0) + cache_read + cache_write
        self.usage["cached"] += cache_read
        self.usage["output"] += u.output_tokens or 0
        self.history.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        return StepResult(text, calls)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        content = []
        for call_id, text, image_b64 in results:
            inner = [{"type": "text", "text": text}]
            if image_b64:
                inner.append(self._img(image_b64))
            content.append({"type": "tool_result", "tool_use_id": call_id, "content": inner})
        self.history.append({"role": "user", "content": content})


# --------------------------------------------------------------------------- #
# OpenAI / OpenAI-compatible
# --------------------------------------------------------------------------- #
class OpenAIBrain(Brain):
    label = "openai"

    def __init__(self, model: str, system: str, api_key: str | None = None,
                 base_url: str | None = None, max_tokens: int = 8000, recorder=None,
                 api_timeout: float = 120.0, api_retries: int = 2):
        from openai import OpenAI

        kwargs: dict = {"timeout": api_timeout, "max_retries": api_retries}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.recorder = recorder
        self.usage = {"input": 0, "output": 0, "cached": 0}
        self.tools = to_openai_tools()
        self.history: list[dict] = [{"role": "system", "content": system}]

    @staticmethod
    def _img_part(b64: str) -> dict:
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    def _user(self, text: str, image_b64: str | None) -> dict:
        parts: list[dict] = [{"type": "text", "text": text}]
        if image_b64:
            parts.append(self._img_part(image_b64))
        return {"role": "user", "content": parts}

    def start(self, text: str, image_b64: str | None = None) -> None:
        self.history = self.history[:1]  # keep system
        self.history.append(self._user(text, image_b64))

    def add_user(self, text: str, image_b64: str | None = None) -> None:
        self.history.append(self._user(text, image_b64))

    def step(self) -> StepResult:
        payload = dict(
            model=self.model,
            messages=self.history,
            tools=self.tools,
            tool_choice="auto",
        )
        if self.max_tokens is not None:  # None = uncapped (local models)
            payload["max_tokens"] = self.max_tokens
        seq = self.recorder.request("openai", payload) if self.recorder else None
        resp = self.client.chat.completions.create(**payload)
        if self.recorder:
            self.recorder.response(seq, resp.model_dump())
        u = resp.usage
        if u:
            self.usage["input"] += u.prompt_tokens or 0
            self.usage["output"] += u.completion_tokens or 0
            details = getattr(u, "prompt_tokens_details", None)
            self.usage["cached"] += (getattr(details, "cached_tokens", 0) or 0) if details else 0
        msg = resp.choices[0].message
        assistant: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        self.history.append(assistant)

        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return StepResult((msg.content or "").strip(), calls)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        images: list[str] = []
        for call_id, text, image_b64 in results:
            # OpenAI tool messages are text-only — one per tool_call_id.
            self.history.append({"role": "tool", "tool_call_id": call_id, "content": text or "ok"})
            if image_b64:
                images.append(image_b64)
        if images:
            # Screenshots can't ride in a tool message; send them as a user image turn.
            parts: list[dict] = [{"type": "text", "text": "Resulting screen after the tool call(s):"}]
            parts += [self._img_part(b) for b in images]
            self.history.append({"role": "user", "content": parts})


# --------------------------------------------------------------------------- #
# Ollama (native /api/chat — lets us set the context window, which the OpenAI
# endpoint ignores; the default 4096 ctx is too small for an agent)
# --------------------------------------------------------------------------- #
class OllamaBrain(Brain):
    label = "ollama"

    def __init__(self, model: str, system: str, base_url: str = "http://localhost:11434",
                 num_ctx: int = 16384, max_tokens: int = 8000, recorder=None,
                 api_timeout: float = 600.0):
        import httpx

        b = (base_url or "http://localhost:11434").rstrip("/")
        if b.endswith("/v1"):  # accept the OpenAI-style URL too
            b = b[:-3]
        self.url = b + "/api/chat"
        self.model = model
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self.recorder = recorder
        self.usage = {"input": 0, "output": 0, "cached": 0}
        self.tools = to_openai_tools()  # Ollama accepts the OpenAI tools schema
        self.system = system
        self.history: list[dict] = [{"role": "system", "content": system}]
        self._client = httpx.Client(timeout=api_timeout)

    @staticmethod
    def _user(text: str, image_b64: str | None) -> dict:
        m = {"role": "user", "content": text}
        if image_b64:
            m["images"] = [image_b64]  # raw base64, no data: prefix
        return m

    def start(self, text: str, image_b64: str | None = None) -> None:
        self.history = [{"role": "system", "content": self.system}, self._user(text, image_b64)]

    def add_user(self, text: str, image_b64: str | None = None) -> None:
        self.history.append(self._user(text, image_b64))

    def step(self) -> StepResult:
        payload = {
            "model": self.model,
            "messages": self.history,
            "tools": self.tools,
            "think": False,  # best-effort; some models honor it to skip reasoning
            "stream": False,
            # num_predict -1 = generate until the context fills or the model stops
            "options": {"num_ctx": self.num_ctx,
                        "num_predict": self.max_tokens if self.max_tokens is not None else -1},
        }
        seq = self.recorder.request("ollama", payload) if self.recorder else None
        data = self._client.post(self.url, json=payload).json()
        if self.recorder:
            self.recorder.response(seq, data)
        if "error" in data:
            raise RuntimeError(f"ollama error: {data['error']}")
        msg = data.get("message", {}) or {}
        self.usage["input"] += data.get("prompt_eval_count", 0) or 0
        self.usage["output"] += data.get("eval_count", 0) or 0

        # Append the assistant turn (drop the `thinking` field to keep context lean).
        tool_calls = msg.get("tool_calls") or []
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        self.history.append(assistant)

        calls = []
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name"), args))
        return StepResult((msg.get("content") or "").strip(), calls)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        images = []
        for _call_id, text, image_b64 in results:
            self.history.append({"role": "tool", "content": text or "ok"})
            if image_b64:
                images.append(image_b64)
        if images:  # tool messages can't carry images; send them as a user turn
            self.history.append({"role": "user", "content": "Resulting screen:", "images": images})


def make_brain(provider: str, *, system: str, model: str, api_key: str | None = None,
               base_url: str | None = None, max_tokens: int = 8000, recorder=None,
               num_ctx: int = 16384, api_timeout: float = 120.0, api_retries: int = 2) -> Brain:
    if provider == "anthropic":
        return AnthropicBrain(model, system, api_key=api_key, base_url=base_url,
                              max_tokens=max_tokens, recorder=recorder,
                              api_timeout=api_timeout, api_retries=api_retries)
    if provider == "ollama":
        # Local inference can be slow; give it a generous floor regardless of the cloud default.
        return OllamaBrain(model, system, base_url=base_url or "http://localhost:11434",
                           num_ctx=num_ctx, max_tokens=max_tokens, recorder=recorder,
                           api_timeout=max(api_timeout, 600.0))
    if provider in ("openai", "lmstudio"):  # lmstudio is OpenAI-compatible (and does tool calls)
        return OpenAIBrain(model, system, api_key=api_key, base_url=base_url,
                           max_tokens=max_tokens, recorder=recorder,
                           api_timeout=api_timeout, api_retries=api_retries)
    raise ValueError(f"unknown provider {provider!r} "
                     "(use 'anthropic', 'openai', 'ollama', or 'lmstudio')")
