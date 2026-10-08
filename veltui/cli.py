"""The `veltui` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .models import MODELS, find_model


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["doctor"]:
        sys.exit(_doctor(argv[1:]))

    p = argparse.ArgumentParser(
        prog="veltui",
        description="duck.ai in your terminal — private AI chat, no account, no API key",
        epilog="when duck.ai changes and veltui stops working: veltui doctor --send")
    p.add_argument("-m", "--model", metavar="MODEL",
                   help="start with this model: number, name or alias (see --list-models)")
    p.add_argument("--list-models", action="store_true", help="print the models and exit")
    p.add_argument("--clear-history", action="store_true",
                   help="delete every saved chat and exit")
    p.add_argument("--idle", type=float, default=10, metavar="MIN",
                   help="close Firefox after this many idle minutes; it starts again as "
                        "you type (default 10, 0 = keep it running)")
    p.add_argument("--show-browser", action="store_true",
                   help="run Firefox in a visible window (to watch what veltui does)")
    p.add_argument("--version", action="version", version=f"veltui {__version__}")
    args = p.parse_args(argv)

    if args.list_models:
        for i, m in enumerate(MODELS, 1):
            alias = f"-m {m.aliases[0]}" if m.aliases else ""
            print(f"  {i}  {m.name:<18} {m.kind:<6} {alias:<12} {m.id}")
        return
    if args.clear_history:
        from .store import Store
        n = Store().delete_all()
        print(f"deleted {n} saved chat{'s' * (n != 1)}")
        return
    model = None
    if args.model:
        model = find_model(args.model)
        if model is None:
            p.error(f"unknown model {args.model!r} (see --list-models)")

    from .app import Veltui
    Veltui(model, headless=not args.show_browser, idle_minutes=max(0.0, args.idle)).run()


def _doctor(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="veltui doctor",
        description="check every part of duck.ai veltui relies on and write a report")
    p.add_argument("--send", action="store_true",
                   help="also send one test message (\"pong\")")
    p.add_argument("--show", action="store_true", help="run Firefox in a visible window")
    p.add_argument("--out", type=Path, metavar="DIR",
                   help="where the report goes (default ~/.cache/veltui/doctor-…)")
    args = p.parse_args(argv)
    from .duck import doctor
    return doctor(send=args.send, show=args.show, out=args.out)
