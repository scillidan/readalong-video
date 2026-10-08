"""Stage 02 (ingest): write rinsed text + anchor segments, convert audio to 16 kHz mono wav."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C


def stage(run: C.Run, mp3: Path) -> dict:
    out = run.stage("02_ingest")
    doc = C.parse_md(run.target.read_text(encoding="utf-8"))

    rinsed_lines = [p["rinsed"] for p in doc["paragraphs"]]
    (out / "rinsed.txt").write_text(
        "\n".join(rinsed_lines) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "segments.json").write_text(
        json.dumps({"file": str(run.target), **doc}, ensure_ascii=False, indent=2),
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
    }
    run.set_stage(
        "ingest",
        {
            "status": "done",
            "dir": str(out),
            "duration_ms": duration_ms,
            "wav": str(wav),
            "segments": str(out / "segments.json"),
            "summary": summary,
        },
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    args = ap.parse_args()
    target = C.resolve_input(args.target)
    run = C.Run(target)
    print(json.dumps(stage(run, Path(str(target) + ".mp3")), ensure_ascii=False))


if __name__ == "__main__":
    main()
