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
from .device import DEFAULT_DESTRUCTIVE, Device
from .ui import make_renderer

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
    # reliability ("Reliable Runs")
    parser.add_argument("--wait-idle", dest="wait_idle", action="store_const", const=True, default=None,
                        help="Wait for the screen to stop changing after each action (default).")
    parser.add_argument("--no-wait-idle", dest="wait_idle", action="store_const", const=False,
                        help="Use a fixed settle delay instead of waiting for the screen to be idle.")
    parser.add_argument("--settle-timeout", type=float, help="Max seconds to wait for the screen to go idle.")
    parser.add_argument("--blank-png-bytes", type=int,
                        help="PNG byte size below which a screenshot counts as blank (0 = disable detection).")
    parser.add_argument("--no-ui-fallback", dest="auto_ui_fallback", action="store_const", const=False, default=None,
                        help="Don't fall back to the UI element list when a screenshot is blank/black.")
    parser.add_argument("--adb-retries", type=int, help="Retries for transient adb failures.")
    parser.add_argument("--api-timeout", type=float, help="Per-LLM-call timeout in seconds (anthropic/openai).")
    parser.add_argument("--api-retries", type=int, help="LLM retry count.")
    parser.add_argument("--confirm-destructive", dest="confirm_destructive", action="store_const", const=True,
                        default=None, help="Pause for confirmation before destructive actions (uninstall/buy/delete/…).")
    parser.add_argument("--no-confirm-destructive", dest="confirm_destructive", action="store_const", const=False,
                        help="Disable the destructive-action confirmation gate.")
    # onboarding (doctor / preflight)
    parser.add_argument("--doctor", action="store_true",
                        help="Run preflight diagnostics (adb, device, screenshot, provider) and exit.")
    parser.add_argument("--no-preflight", dest="preflight", action="store_const", const=False, default=None,
                        help="Skip the automatic pre-run checks.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Non-interactive: never prompt (fail instead of asking, e.g. in scripts/CI).")
    # interactive CLI presentation
    parser.add_argument("--ui", choices=["auto", "rich", "plain"],
                        help="CLI style (default auto: rich if installed and a TTY).")
    parser.add_argument("--inline-screen", dest="inline_screen", choices=["auto", "on", "off"], default=None,
                        help="Show the phone screen inline after each action (kitty/iTerm2).")
    parser.add_argument("--no-inline-screen", dest="inline_screen", action="store_const", const="off",
                        help="Disable inline screenshots.")
    parser.add_argument("--screen-max-cells", type=int, help="Inline screenshot width in terminal cells.")
    # skills (learn once, run free)
    parser.add_argument("--record", metavar="TASK",
                        help="Run TASK with the API agent, then save it as a replayable skill.")
    parser.add_argument("--as", dest="skill_name", metavar="NAME",
                        help="Name for the recorded / imported skill.")
    parser.add_argument("--run-skill", dest="run_skill", metavar="NAME",
                        help="Replay a saved skill deterministically (heals drifted steps with the LLM).")
    parser.add_argument("--skills", action="store_true", help="List saved skills and exit.")
    parser.add_argument("--import-trace", dest="import_trace", metavar="JSONL",
                        help="Build a skill from a claude-cli debug JSONL trace.")
    # notification triggers (wake-on-notification, chat mode)
    parser.add_argument("--triggers", action="store_true",
                        help="List saved notification triggers and exit.")
    parser.add_argument("--triggers-on", dest="triggers_enabled", action="store_const",
                        const=True, default=None, help="Enable the inline notification watcher in chat.")
    parser.add_argument("--no-triggers", dest="triggers_enabled", action="store_const",
                        const=False, help="Disable the inline notification watcher in chat.")
    parser.add_argument("--poll-interval", dest="poll_interval", type=float, metavar="SECONDS",
                        help="How often to poll device notifications for triggers (default 8).")
    # guardrails & verify
    parser.add_argument("--forbid", action="append", metavar="TEXT",
                        help="Forbidden on-screen label/text (repeatable); a violation stops/rewinds.")
    parser.add_argument("--forbid-nl", dest="forbid_nl", action="append", metavar="DESC",
                        help="Forbidden state in plain English, checked by an LLM judge (repeatable).")
    parser.add_argument("--assert", dest="assert_success", metavar="DESC",
                        help="Plain-English success condition, verified at the end.")
    parser.add_argument("--stay-in-app", dest="stay_in_app", metavar="PKG",
                        help="The foreground app must not change (package name).")
    parser.add_argument("--guardrails", metavar="FILE", help="Load guardrails from a TOML/JSON file.")
    parser.add_argument("--on-violation", choices=["stop", "rewind", "ask"],
                        help="What to do on a guardrail violation (default stop).")
    parser.add_argument("--judge-frequency", dest="judge_frequency", type=int, metavar="N",
                        help="Run the NL judge every N steps (0 = only at the end).")
    args = parser.parse_args(argv)

    provider = args.provider or ("claude-cli" if args.chat else cfg.provider)
    serial = args.serial or cfg.serial
    effort = clamp_effort(args.effort if args.effort is not None else cfg.effort)

    # reliability knobs: CLI flag overrides config (None = "not set on the CLI")
    def _pick(flag, conf):
        return flag if flag is not None else conf
    wait_idle = _pick(args.wait_idle, cfg.wait_idle)
    auto_ui_fallback = _pick(args.auto_ui_fallback, cfg.auto_ui_fallback)
    settle_timeout = _pick(args.settle_timeout, cfg.settle_timeout)
    blank_png_bytes = _pick(args.blank_png_bytes, cfg.blank_png_bytes)
    adb_retries = _pick(args.adb_retries, cfg.adb_retries)
    api_timeout = _pick(args.api_timeout, cfg.api_timeout)
    api_retries = _pick(args.api_retries, cfg.api_retries)
    confirm_destructive = _pick(args.confirm_destructive, cfg.confirm_destructive)
    do_preflight = _pick(args.preflight, cfg.preflight)
    destructive_keywords = cfg.destructive_keywords or list(DEFAULT_DESTRUCTIVE)
    ui_mode = args.ui or cfg.ui
    inline_screen = args.inline_screen or cfg.inline_screen
    screen_max_cells = args.screen_max_cells or cfg.screen_max_cells
    triggers_enabled = _pick(args.triggers_enabled, cfg.triggers_enabled)
    triggers_poll_interval_s = _pick(args.poll_interval, cfg.triggers_poll_interval_s)

    # skill listing / import need no device
    if args.skills:
        from .skills import list_skills
        sks = list_skills()
        if not sks:
            print('(no saved skills yet — record one with --record "<task>" --as <name>)')
        for s in sks:
            sc = " · shortcut" if (s.shortcut and s.shortcut.component) else ""
            print(f"{s.slug:24} {len(s.steps):>2} steps{sc}  —  {s.task}")
        return 0
    if args.import_trace:
        from .skills import SkillImporter
        sk = SkillImporter.from_jsonl(args.import_trace, name=args.skill_name)
        print(f"imported {len(sk.steps)} steps → {sk.save()}")
        return 0
    if args.triggers:
        from .triggers import list_triggers
        trigs = list_triggers()
        if not trigs:
            print("(no notification triggers yet - the agent creates these in chat when "
                  "you ask for a reactive task)")
        for t in trigs:
            filt = t.title_contains or t.text_contains or t.pattern
            where = t.package + (f" [{filt}]" if filt else "")
            print(f"{t.slug:20} {'on ' if t.enabled else 'off'}  {where:32}  ->  {t.task}")
        return 0

    # build the guardrail set (file < CLI), shared by both paths
    from .guardrails import from_cli as _gr_cli, from_file as _gr_file, merge as _gr_merge
    gset = _gr_merge(_gr_file(args.guardrails) if args.guardrails else None, _gr_cli(args))
    if args.on_violation:
        gset.on_violation = args.on_violation
    if args.judge_frequency is not None:
        gset.judge_frequency = args.judge_frequency

    debug = args.debug or cfg.debug
    debug_dir = args.debug_dir or cfg.debug_dir
    if not os.path.isabs(debug_dir):
        debug_dir = os.path.join(PROJECT_ROOT, debug_dir)
    debug_path = new_session_path(debug_dir) if debug else None

    adb = ADB(serial=serial, retries=adb_retries, backoff=cfg.adb_backoff, timeout=cfg.adb_timeout)
    if serial and ":" in serial:
        adb.ensure_connected()

    # --doctor: full diagnostics, then exit (works even if `adb devices` errors)
    if args.doctor:
        from .doctor import doctor
        return doctor(cfg, provider=provider, serial=serial, model=args.model,
                      base_url=args.base_url, max_long_edge=cfg.max_long_edge,
                      connect_timeout=cfg.connect_timeout)

    try:
        attached = adb.devices()
    except ADBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_devices:
        print("\n".join(attached) if attached else "(no devices attached)")
        return 0

    from .doctor import preflight as run_preflight, select_device
    serial, err = select_device(adb, serial, attached, auto_select=cfg.auto_select_device,
                                interactive=sys.stdin.isatty(), assume_yes=args.yes)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    where = config_path()
    print(f"provider={provider} · device={serial} · effort={effort}/5"
          + (f" · config={where}" if where else " · (no config file; using defaults)"))

    # auto-preflight: fail fast with actionable messages (runs for ALL providers,
    # including claude-cli which otherwise wouldn't touch the device until later).
    if do_preflight:
        ok, msg = run_preflight(cfg, provider=provider, serial=serial, model=args.model,
                                base_url=args.base_url, max_long_edge=cfg.max_long_edge,
                                include_screenshot=cfg.preflight_screenshot,
                                connect_timeout=cfg.connect_timeout)
        if not ok:
            print(msg, file=sys.stderr)
            return 2

    # ---- claude-cli: spawn the claude agent (chat) ----
    if provider == "claude-cli":
        if args.record:
            print("error: --record needs an API provider (the claude-cli chat loop isn't "
                  "recordable). Try --provider openai|anthropic|ollama|lmstudio.", file=sys.stderr)
            return 2
        if args.run_skill:  # deterministic replay only (no API key → no healing)
            from .skills import SkillRunner, find_skill
            sk = find_skill(args.run_skill)
            if not sk:
                print(f"error: no skill named {args.run_skill!r} (see --skills).", file=sys.stderr)
                return 2
            device = Device(adb=adb, max_long_edge=cfg.max_long_edge, settle=cfg.settle,
                            blank_png_bytes=blank_png_bytes, auto_ui_fallback=auto_ui_fallback,
                            wait_idle=wait_idle, settle_timeout=settle_timeout,
                            destructive_keywords=destructive_keywords)
            print(SkillRunner(device, sk, brain_factory=None, heal=False,
                              use_shortcut=cfg.skill_shortcut).run())
            return 0
        return run_chat(
            serial=serial, adb_path=adb.adb_path,
            model=args.model or cfg.claude_model,
            budget=args.budget if args.budget is not None else cfg.claude_budget_usd,
            max_long_edge=cfg.max_long_edge, effort=effort, debug_path=debug_path,
            blank_png_bytes=blank_png_bytes, auto_ui_fallback=auto_ui_fallback,
            wait_idle=wait_idle, settle_timeout=settle_timeout, adb_retries=adb_retries,
            confirm_destructive=confirm_destructive, destructive_keywords=destructive_keywords,
            ui_mode=ui_mode, inline_screen=inline_screen, screen_max_cells=screen_max_cells,
            guardrails=gset if not gset.is_empty() else None,
            max_output_tokens=cfg.claude_max_output_tokens,
            triggers_enabled=triggers_enabled,
            triggers_poll_interval_s=triggers_poll_interval_s,
            triggers_cooldown_s=cfg.triggers_cooldown_s,
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

    device = Device(adb=adb, max_long_edge=cfg.max_long_edge, settle=cfg.settle,
                    blank_png_bytes=blank_png_bytes, auto_ui_fallback=auto_ui_fallback,
                    wait_idle=wait_idle, settle_timeout=settle_timeout,
                    destructive_keywords=destructive_keywords)
    recorder = SessionRecorder(debug_path) if debug_path else None
    try:
        from .brains import make_brain

        # Local models are free → don't cap output (None = uncapped). Cloud keeps a cap.
        local = provider in ("ollama", "lmstudio")
        brain = make_brain(
            provider, system=build_system_prompt(effort, vision), model=model,
            api_key=api_key, base_url=base_url, recorder=recorder,
            num_ctx=cfg.ollama_num_ctx, max_tokens=None if local else 8000,
            api_timeout=api_timeout, api_retries=api_retries,
        )
    except ImportError as exc:
        pkg = "anthropic" if provider == "anthropic" else "openai"
        print(f"error: the '{provider}' provider needs the {pkg} package: pip install {pkg} ({exc})",
              file=sys.stderr)
        return 2

    print(f"model={model} · screen={'image' if vision else 'text'}"
          + (f" · base_url={base_url}" if base_url else "")
          + (f" · debug={debug_path}" if debug_path else ""))
    renderer = make_renderer(ui_mode=ui_mode, inline_screen=inline_screen,
                             screen_max_cells=screen_max_cells,
                             status_fields={"provider": provider, "model": model,
                                            "device": serial, "effort": f"{effort}/5"})
    def _judge_factory(system_prompt):  # cheap LLM judge for NL guardrails (same provider)
        return make_brain(provider, system=system_prompt, model=model, api_key=api_key,
                          base_url=base_url, num_ctx=cfg.ollama_num_ctx,
                          max_tokens=None if local else 1024,
                          api_timeout=api_timeout, api_retries=api_retries)
    agent = AgenticAndroid(brain=brain, device=device, max_steps=args.max_steps,
                         verbose=not args.quiet, vision=vision, pricing=cfg.pricing,
                         confirm_destructive=confirm_destructive, ui=renderer,
                         guardrails=gset if not gset.is_empty() else None,
                         judge_factory=_judge_factory)

    # ---- skills: replay or record (API path; healing uses this provider's brain) ----
    if args.run_skill:
        from .skills import SkillRunner, find_skill
        sk = find_skill(args.run_skill)
        if not sk:
            print(f"error: no skill named {args.run_skill!r} (see --skills).", file=sys.stderr)
            return 2

        def _factory(system_prompt):
            return make_brain(provider, system=system_prompt, model=model, api_key=api_key,
                              base_url=base_url, num_ctx=cfg.ollama_num_ctx,
                              max_tokens=None if local else 8000,
                              api_timeout=api_timeout, api_retries=api_retries)
        runner = SkillRunner(device, sk, brain_factory=(_factory if cfg.skill_heal else None),
                             heal=cfg.skill_heal, use_shortcut=cfg.skill_shortcut)
        print(runner.run())
        return 0
    if args.record:
        from datetime import datetime
        from .skills import SkillRecorder
        agent.recorder = SkillRecorder(device)
        print(f"\n=== Recording: {args.record} ===")
        result = agent.run(args.record)
        print(f"\n--- Result: {result}\n")
        rec = agent.recorder
        if rec.success and rec.steps:
            name = args.skill_name or args.record
            sk = rec.finalize(name=name, task=args.record,
                              created_at=datetime.now().isoformat(timespec="seconds"))
            print(f"saved skill '{sk.name}' ({len(sk.steps)} steps) → {sk.save()}")
        else:
            print("not saved (run didn't end with done(success=true), or recorded no steps).")
        agent._print_cost()
        return 0

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
    if agent.verdict is not None:
        print("\n" + agent.verdict.report())
        return agent.verdict.exit_code()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
