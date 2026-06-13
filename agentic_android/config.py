"""Owner configuration, loaded from `agentic-android.toml` (project root) or
`~/.config/agentic-android/config.toml`. Everything a newbie needs to edit lives in
that one file; CLI flags override it.

Two things matter most:
  * provider  — which brain drives the phone (claude-cli / anthropic / openai)
  * effort    — how hard it tries before asking you a question (0-5; cost knob)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EFFORT = 3
PROVIDERS = ("claude-cli", "anthropic", "openai", "ollama", "lmstudio")

# USD per 1 MILLION tokens, used for the cost estimate printed after a run.
# Override or extend in agentic-android.toml under [pricing].
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.0, "cached_input": 1.25},
    "gpt-4.1": {"input": 2.00, "output": 8.00, "cached_input": 0.50},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60, "cached_input": 0.10},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cached_input": 0.3},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cached_input": 0.1},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int, pricing: dict) -> float | None:
    """USD cost for a model's token usage, or None if the model isn't priced.
    `input_tokens` is the TOTAL prompt tokens (including cached)."""
    p = pricing.get(model)
    if p is None:  # prefix match, e.g. "gpt-4o-mini-2024-07-18"
        for name, val in pricing.items():
            if model.startswith(name):
                p = val
                break
    if p is None:
        return None
    uncached = max(0, input_tokens - cached_tokens)
    cached_rate = p.get("cached_input", p["input"])
    return (uncached * p["input"] + cached_tokens * cached_rate
            + output_tokens * p["output"]) / 1_000_000

# Per-level persistence policy injected into the agent's system prompt.
EFFORT_GUIDANCE: dict[int, str] = {
    0: "Persistence 0/5 — ask-first. At the first ambiguity, missing piece of "
       "information, or any tool failure, stop and ask the user. Do essentially "
       "no self-recovery.",
    1: "Persistence 1/5 — make at most ONE quick retry (e.g. re-screenshot or one "
       "alternate tap). If that doesn't work, ask.",
    2: "Persistence 2/5 — try a couple of recovery attempts (re-screenshot, scroll "
       "to find the element, one alternate path) before asking.",
    3: "Persistence 3/5 (default) — try several distinct strategies before asking: "
       "re-screenshot, dump_ui to locate elements precisely, scroll, back out and "
       "try an alternate path. Ask once you've genuinely run out of reasonable "
       "approaches.",
    4: "Persistence 4/5 — be persistent: exhaust visual and UI-tree approaches with "
       "multiple retries and alternate navigation paths. Ask only when truly blocked "
       "— e.g. you need a credential or 2FA code, hit a paywall, or the user's intent "
       "is ambiguous in a way you cannot infer.",
    5: "Persistence 5/5 — maximum effort: try every reasonable avenue, patiently and "
       "repeatedly, before asking. Only ask for things you physically cannot resolve "
       "yourself (passwords, OTP/2FA codes, a purchase/payment confirmation, or a "
       "genuinely ambiguous high-stakes choice).",
}

_PROTOCOL_CHAT = (
    "When you need to ask (per your persistence level), write ONE short question "
    "as a normal message, STOP calling tools, and wait. Offer 2-4 concrete numbered "
    "options when you can, plus an 'other' for a custom answer. The user replies in "
    "chat and you continue from there."
)
_PROTOCOL_TOOL = (
    "When you need to ask (per your persistence level), call the `ask_user` tool with "
    "ONE short question and 2-4 concrete options. It returns the user's answer; then "
    "continue. Do not end the task with an unanswered question — use ask_user instead."
)

COST_NOTE = (
    "Persistence level sets how hard the agent works before pausing to ask you. "
    "Higher = more screenshots, tool calls and model turns = higher API/usage cost; "
    "lower = it checks in with you sooner and spends less."
)


def clamp_effort(n: int) -> int:
    return max(0, min(5, int(n)))


def persistence_block(effort: int, mode: str = "chat") -> str:
    effort = clamp_effort(effort)
    protocol = _PROTOCOL_CHAT if mode == "chat" else _PROTOCOL_TOOL
    return f"{EFFORT_GUIDANCE[effort]}\n{protocol}"


@dataclass
class Config:
    provider: str = "claude-cli"
    serial: str | None = None
    effort: int = DEFAULT_EFFORT
    max_long_edge: int = 1568
    debug: bool = False
    debug_dir: str = "debug"
    # claude-cli (chat mode — your logged-in `claude`, no API key)
    claude_model: str = "sonnet"
    claude_budget_usd: float | None = None
    # anthropic API
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    anthropic_vision: bool = True
    # openai API (also any OpenAI-compatible endpoint)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    openai_vision: bool = True
    # ollama — a local server (model auto-detected if blank)
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = ""
    ollama_vision: bool = False
    ollama_num_ctx: int = 32768  # context window; Ollama defaults to 4096 (too small for an agent)
    # lmstudio — a local OpenAI-compatible server (model auto-detected if blank)
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = ""
    lmstudio_vision: bool = False
    pricing: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PRICING.items()})


def _config_paths() -> list[str]:
    return [
        os.path.join(PROJECT_ROOT, "agentic-android.toml"),
        os.path.join(os.path.expanduser("~"), ".config", "agentic_android", "config.toml"),
    ]


def config_path() -> str | None:
    for path in _config_paths():
        if os.path.isfile(path):
            return path
    return None


def load_config() -> Config:
    cfg = Config()
    data: dict = {}
    path = config_path()
    if path and tomllib is not None:
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            data = {}

    if data.get("provider") in PROVIDERS:
        cfg.provider = data["provider"]

    device = data.get("device", {})
    if device.get("serial"):
        cfg.serial = str(device["serial"])

    agent = data.get("agent", {})
    if "effort" in agent:
        cfg.effort = clamp_effort(agent["effort"])
    if agent.get("max_long_edge"):
        cfg.max_long_edge = int(agent["max_long_edge"])
    if "debug" in agent:
        cfg.debug = bool(agent["debug"])
    if agent.get("debug_dir"):
        cfg.debug_dir = str(agent["debug_dir"])

    cc = data.get("claude_cli", {})
    if cc.get("model"):
        cfg.claude_model = str(cc["model"])
    if cc.get("budget_usd"):
        cfg.claude_budget_usd = float(cc["budget_usd"])

    an = data.get("anthropic", {})
    if an.get("api_key"):
        cfg.anthropic_api_key = str(an["api_key"])
    if an.get("model"):
        cfg.anthropic_model = str(an["model"])
    if "vision" in an:
        cfg.anthropic_vision = bool(an["vision"])

    oa = data.get("openai", {})
    if oa.get("api_key"):
        cfg.openai_api_key = str(oa["api_key"])
    if oa.get("base_url"):
        cfg.openai_base_url = str(oa["base_url"])
    if oa.get("model"):
        cfg.openai_model = str(oa["model"])
    if "vision" in oa:
        cfg.openai_vision = bool(oa["vision"])

    ol = data.get("ollama", {})
    if ol.get("base_url"):
        cfg.ollama_base_url = str(ol["base_url"])
    if ol.get("model"):
        cfg.ollama_model = str(ol["model"])
    if "vision" in ol:
        cfg.ollama_vision = bool(ol["vision"])
    if ol.get("num_ctx"):
        cfg.ollama_num_ctx = int(ol["num_ctx"])

    lm = data.get("lmstudio", {})
    if lm.get("base_url"):
        cfg.lmstudio_base_url = str(lm["base_url"])
    if lm.get("model"):
        cfg.lmstudio_model = str(lm["model"])
    if "vision" in lm:
        cfg.lmstudio_vision = bool(lm["vision"])

    for model, prices in (data.get("pricing", {}) or {}).items():
        if isinstance(prices, dict):
            cfg.pricing[model] = {**cfg.pricing.get(model, {}), **prices}

    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: Config) -> None:
    """Environment variables (e.g. from .env) override the config file.
    CLI flags, applied later, override these."""
    env = os.environ
    if env.get("AGENTIC_ANDROID_PROVIDER") in PROVIDERS:
        cfg.provider = env["AGENTIC_ANDROID_PROVIDER"]
    if env.get("ANDROID_SERIAL"):
        cfg.serial = env["ANDROID_SERIAL"]
    if env.get("OPENAI_API_KEY"):
        cfg.openai_api_key = env["OPENAI_API_KEY"]
    if env.get("OPENAI_BASE_URL"):
        cfg.openai_base_url = env["OPENAI_BASE_URL"]
    if env.get("OPENAI_MODEL"):
        cfg.openai_model = env["OPENAI_MODEL"]
    if env.get("OLLAMA_BASE_URL"):
        cfg.ollama_base_url = env["OLLAMA_BASE_URL"]
    if env.get("OLLAMA_MODEL"):
        cfg.ollama_model = env["OLLAMA_MODEL"]
    if env.get("LMSTUDIO_BASE_URL"):
        cfg.lmstudio_base_url = env["LMSTUDIO_BASE_URL"]
    if env.get("LMSTUDIO_MODEL"):
        cfg.lmstudio_model = env["LMSTUDIO_MODEL"]
    if env.get("ANTHROPIC_API_KEY"):
        cfg.anthropic_api_key = env["ANTHROPIC_API_KEY"]
    if env.get("ANTHROPIC_MODEL"):
        cfg.anthropic_model = env["ANTHROPIC_MODEL"]
