"""Command-line entry point.

Edit agentic-android.toml, then:
  python -m agentic_android                    # uses your config (chat or agent)
  python -m agentic_android "open settings"    # one-shot task (anthropic/openai providers)
  python -m agentic_android --provider openai --base-url http://localhost:11434/v1
"""

from __future__ import annotations

import argparse
import os
import sys

try:  # optional: load a local .env if python-dotenv is installed
    import logging

    from dotenv import load_dotenv

    logging.getLogger("dotenv").setLevel(logging.ERROR)  # quiet "could not parse" noise
    load_dotenv()
except Exception:
    pass

from .adb import ADB, ADBError
from .agent import AgenticAndroid, build_system_prompt
from .chat import run_chat
from .config import PROJECT_ROOT, PROVIDERS, clamp_effort, config_path, load_config
from .debuglog import SessionRecorder, new_session_path
from .device import Device

OFFICIAL_OPENAI = "https://api.openai.com/v1"


def _autodetect_model(base_url: str, api_key: str) -> str:
    """First loaded chat model on a local server (skips embedding models)."""
    from openai import OpenAI

    data = OpenAI(api_key=api_key, base_url=base_url).models.list().data
    ids = [m.id for m in data if "embed" not in m.id.lower()] or [m.id for m in data]
    if not ids:
        raise RuntimeError("no models loaded")
    return ids[0]


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()

    parser = argparse.ArgumentParser(
        prog="agentic-android",
        description="Claude or OpenAI drives an Android device over ADB. Configure agentic-android.toml.",
    )
    parser.add_argument("task", nargs="?", help="One-shot task (anthropic/openai). Omit for interactive.")
    parser.add_argument("--provider", choices=PROVIDERS, help=f"Override config provider {PROVIDERS}.")
    parser.add_argument("--chat", action="store_true", help="Shortcut for --provider claude-cli.")
    parser.add_argument("-s", "--serial", help="ADB serial / IP:port (overrides config / $ANDROID_SERIAL).")
    parser.add_argument("--model", help="Model override for the chosen provider.")
    parser.add_argument("--base-url", help="(openai) API base URL override, e.g. http://localhost:11434/v1.")
    parser.add_argument("--vision", dest="vision", action="store_const", const=True, default=None,
                        help="Force image mode (model sees screenshots).")
    parser.add_argument("--no-vision", dest="vision", action="store_const", const=False,
                        help="Force text mode: feed the screen as a text element list (for models that can't see images).")
    parser.add_argument("--effort", type=int, choices=range(0, 6), metavar="0-5",
                        help="How hard to try before asking (0=ask early/cheapest, 5=most/priciest).")
    parser.add_argument("--budget", type=float, help="(claude-cli) Max USD, via claude --max-budget-usd.")
    parser.add_argument("--max-steps", type=int, default=40, help="(agent) Max action steps (default: 40).")
    parser.add_argument("--debug", action="store_true",
                        help="Save all API requests/responses for the session to a JSONL file under debug/.")
    parser.add_argument("--debug-dir", help="Directory for debug session files (default: debug/).")
    parser.add_argument("--list-devices", action="store_true", help="List attached devices and exit.")
    parser.add_argument("-q", "--quiet", action="store_true", help="(agent) Suppress step logging.")
    args = parser.parse_args(argv)

    provider = args.provider or ("claude-cli" if args.chat else cfg.provider)
    serial = args.serial or cfg.serial
    effort = clamp_effort(args.effort if args.effort is not None else cfg.effort)

    debug = args.debug or cfg.debug
    debug_dir = args.debug_dir or cfg.debug_dir
    if not os.path.isabs(debug_dir):
        debug_dir = os.path.join(PROJECT_ROOT, debug_dir)
    debug_path = new_session_path(debug_dir) if debug else None

    adb = ADB(serial=serial)
    if serial and ":" in serial:
        adb.ensure_connected()

    try:
        attached = adb.devices()
    except ADBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_devices:
        print("\n".join(attached) if attached else "(no devices attached)")
        return 0

    if not attached:
        print("error: no Android device detected. Boot/connect one (network emulator: "
              "`adb connect <ip>:<port>`) and check `python -m agentic_android --list-devices`.", file=sys.stderr)
        return 2
    if not serial and len(attached) > 1:
        print("error: multiple devices attached; set [device].serial or pass --serial. Devices:", file=sys.stderr)
        print("  " + "\n  ".join(attached), file=sys.stderr)
        return 2
    serial = serial or attached[0]

    where = config_path()
    print(f"provider={provider} · device={serial} · effort={effort}/5"
          + (f" · config={where}" if where else " · (no config file; using defaults)"))

    # ---- claude-cli: spawn the claude agent (chat) ----
    if provider == "claude-cli":
        return run_chat(
            serial=serial, adb_path=adb.adb_path,
            model=args.model or cfg.claude_model,
            budget=args.budget if args.budget is not None else cfg.claude_budget_usd,
            max_long_edge=cfg.max_long_edge, effort=effort, debug_path=debug_path,
        )

    # ---- anthropic / openai: API brain ----
    if provider == "anthropic":
        model = args.model or cfg.anthropic_model
        # cfg already reflects env (ANTHROPIC_API_KEY); AUTH_TOKEN is the extra fallback.
        api_key = cfg.anthropic_api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = None
        vision = cfg.anthropic_vision
        if not api_key:
            print("error: provider 'anthropic' needs an API key. Put it in agentic-android.toml "
                  "([anthropic] api_key) or set ANTHROPIC_API_KEY.", file=sys.stderr)
            return 2
    elif provider == "ollama":  # local server, native API (see brains.OllamaBrain)
        base_url = args.base_url or cfg.ollama_base_url
        api_key = "ollama"  # placeholder; the server ignores it
        vision = cfg.ollama_vision
        model = args.model or cfg.ollama_model
        if not model:
            try:
                model = _autodetect_model(base_url, api_key)
                print(f"auto-detected ollama model: {model}")
            except Exception as exc:
                print(f"error: couldn't list models at {base_url} ({exc}). Is `ollama serve` "
                      "running? Set [ollama] model in agentic-android.toml.", file=sys.stderr)
                return 2
    elif provider == "lmstudio":  # LM Studio local server (OpenAI-compatible)
        base_url = args.base_url or cfg.lmstudio_base_url
        api_key = "lm-studio"  # placeholder; the server ignores it
        vision = cfg.lmstudio_vision
        model = args.model or cfg.lmstudio_model
        if not model:
            try:
                model = _autodetect_model(base_url, api_key)
                print(f"auto-detected lmstudio model: {model}")
            except Exception as exc:
                print(f"error: couldn't list models at {base_url} ({exc}). Start the LM Studio "
                      "server (Developer tab) with a model loaded, or set [lmstudio] model.", file=sys.stderr)
                return 2
    else:  # openai — precedence: --base-url flag > env/.env > toml > default
        model = args.model or cfg.openai_model
        base_url = args.base_url or cfg.openai_base_url or OFFICIAL_OPENAI
        api_key = cfg.openai_api_key
        vision = cfg.openai_vision
        if not api_key:
            if base_url.rstrip("/") == OFFICIAL_OPENAI.rstrip("/"):
                print("error: provider 'openai' needs an API key. Put it in agentic-android.toml "
                      "([openai] api_key) or set OPENAI_API_KEY.", file=sys.stderr)
                return 2
            api_key = "not-needed"  # local/compatible servers usually ignore the key
    if args.vision is not None:
        vision = args.vision

    device = Device(adb=adb, max_long_edge=cfg.max_long_edge)
    recorder = SessionRecorder(debug_path) if debug_path else None
    try:
        from .brains import make_brain

        # Local models are free → don't cap output (None = uncapped). Cloud keeps a cap.
        local = provider in ("ollama", "lmstudio")
        brain = make_brain(
            provider, system=build_system_prompt(effort, vision), model=model,
            api_key=api_key, base_url=base_url, recorder=recorder,
            num_ctx=cfg.ollama_num_ctx, max_tokens=None if local else 8000,
        )
    except ImportError as exc:
        pkg = "anthropic" if provider == "anthropic" else "openai"
        print(f"error: the '{provider}' provider needs the {pkg} package: pip install {pkg} ({exc})",
              file=sys.stderr)
        return 2

    print(f"model={model} · screen={'image' if vision else 'text'}"
          + (f" · base_url={base_url}" if base_url else "")
          + (f" · debug={debug_path}" if debug_path else ""))
    agent = AgenticAndroid(brain=brain, device=device, max_steps=args.max_steps,
                         verbose=not args.quiet, vision=vision, pricing=cfg.pricing)

    try:
        if args.task:
            print(f"\n=== Task: {args.task} ===")
            print(f"\n--- Result: {agent.run(args.task)}\n")
        else:
            agent.chat()
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error from {provider} ({model}): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
