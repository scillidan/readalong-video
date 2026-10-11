"""readalong: narrated audio (mp3) + standard .md -> forced alignment -> read-along mp4.

Subcommands map 1:1 to the pipeline stages:
  add       full pipeline (probe..report) for a .md file or a directory of them
  probe     01: md segments / audio / PDF
  ingest    02: rinsed text + 16k mono wav (--media <file|dir>)
  alignw    03: whisper.cpp forced alignment (default stable CPU engine)
  align     03: whisperX forced alignment (legacy torch CUDA engine)
  verify    04: timeline QC + page mapping
  render    05: highlights + page turns -> mp4
  report    06: aggregate report
  prefetch  model weights
  bleach    Gutenberg .txt -> standard .md
  gen-a4    standard .md -> A4 landscape 2-up reader PDF
  gen-a5    standard .md -> single-A5-pages PDF

Config (.env, earliest value wins; env vars beat files):
  ~/.config/readalong-video/.env   user-wide (e.g. PDF_SOURCE, READALONG_RUNS)
  ./.env (cwd upward)              per-project overrides
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    # Windows consoles default to a legacy codepage; force UTF-8 so progress
    # markers (✓/✗) and CJK text do not raise UnicodeEncodeError.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import find_dotenv, load_dotenv

    user_env = Path.home() / ".config" / "readalong-video" / ".env"
    if user_env.exists():
        load_dotenv(user_env, override=False)
    load_dotenv(dotenv_path=find_dotenv(usecwd=True), override=False)

    from . import (
        add,
        align,
        align_whispercpp,
        bleach,
        gen_pdf,
        ingest,
        prefetch,
        probe,
        render,
        report,
        verify,
    )

    modules = {
        "add": add,
        "probe": probe,
        "ingest": ingest,
        "alignw": align_whispercpp,
        "align": align,
        "verify": verify,
        "render": render,
        "report": report,
        "prefetch": prefetch,
        "bleach": bleach,
        "gen-a4": gen_pdf,
        "gen-a5": gen_pdf,
    }

    ap = argparse.ArgumentParser(
        prog="readalong",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, mod in modules.items():
        help_line = (mod.__doc__ or "").strip().splitlines()[0]
        p = sub.add_parser(name, help=help_line, description=mod.__doc__)
        mod.add_args(p)
        p.set_defaults(func=mod.run)
        if name == "gen-a4":
            p.set_defaults(variant="a4")
        if name == "gen-a5":
            p.set_defaults(variant="a5")

    args = ap.parse_args()
    args.func(args)
