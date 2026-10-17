"""Stage 02 (ingest): write rinsed text + anchor segments, convert audio to 16 kHz mono wav."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import common as C


def stage(run: C.Run, mp3: Path, with_headings: bool = False) -> dict:
    out = run.stage("02_ingest")
    doc = C.parse_md(run.target.read_text(encoding="utf-8"), with_headings)

    rinsed_lines = [p["rinsed"] for p in doc["paragraphs"]]
    (out / "rinsed.txt").write_text(
        "\n".join(rinsed_lines) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "segments.json").write_text(
        json.dumps(
            {"file": str(run.target), "audio": str(mp3), **doc},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )

    if not mp3.exists():
        raise SystemExit(
            f"audio missing: {mp3} (generate it from rinsed.txt, e.g. VoiceStudio)"
        )
    wav = out / "audio_16k_mono.wav"
    C.audio_to_16k_mono(mp3, wav)
    duration_ms = C.ffprobe_duration_ms(wav)

    ignored = doc["ignored_ranges"]
    summary = {
        "rinsed_chars": sum(len(x) for x in rinsed_lines),
        "segments": doc["stats"],
        "ignored": {
            "metadata_fence_lines": sum(
                r["end_line"] - r["start_line"] + 1
                for r in ignored
                if r["kind"] == "metadata_fence"
            ),
            "footnote_defs": len(doc["footnote_defs"]),
        },
        "duration_ms": duration_ms,
        "with_headings": with_headings,
    }
    run.set_stage(
        "ingest",
        {
            "status": "done",
            "dir": str(out),
            "duration_ms": duration_ms,
            "mp3": str(mp3),
            "wav": str(wav),
            "segments": str(out / "segments.json"),
            "with_headings": with_headings,
            "summary": summary,
        },
    )
    return summary


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target")
    ap.add_argument("--media", default="", help="audio file or directory (see add)")
    ap.add_argument(
        "--with-headings",
        action="store_true",
        help="narration reads ## chapter titles aloud: anchor them too",
    )


def run(args: argparse.Namespace) -> None:
    target = C.require_file(C.resolve_input(args.target), "ingest")
    mp3 = C.resolve_media(target, args.media)
    run = C.Run(target)
    print(json.dumps(stage(run, mp3, args.with_headings), ensure_ascii=False))
