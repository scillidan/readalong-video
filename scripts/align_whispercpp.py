"""Stage 03 (align) via whisper.cpp (ggml, CPU): token timestamps -> char anchors.

Stable-engine replacement for the whisperX/torch/CTranslate2 CUDA stack, which hard-hangs
this host (no dump, only EventLog 6008). whisper.cpp is the same ggml engine family as
llama.cpp — proven stable here — and the CPU-only build never touches the GPU driver.

Produces the same artifacts as the whisperX engine (raw.json, ctm.tsv, char_times.json,
segments_timed.json, align_summary.json) so verify/render are unchanged.

Assets (override via env):
  WHISPER_CPP_BIN    default ~/.cache/whisper.cpp/bin/x64/Release/whisper-cli.exe
  WHISPER_CPP_MODEL  default ~/Local/Model/whisper-cpp/ggml-large-v3-turbo.bin
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C
import align as A

DEFAULT_BIN = Path(
    os.environ.get(
        "WHISPER_CPP_BIN",
        Path.home()
        / ".cache"
        / "whisper.cpp"
        / "bin"
        / "x64"
        / "Release"
        / "whisper-cli.exe",
    )
)
DEFAULT_MODEL = Path(
    os.environ.get(
        "WHISPER_CPP_MODEL",
        Path.home() / "Local" / "Model" / "whisper-cpp" / "ggml-large-v3-turbo.bin",
    )
)


def _tok_span_ms(t: dict) -> tuple[float, float] | None:
    """Token timestamp as (start_ms, end_ms); tolerates both whisper.cpp JSON shapes."""
    off = t.get("offsets")
    if isinstance(off, dict) and "from" in off and "to" in off:
        return float(off["from"]), float(off["to"])
    ts = t.get("timestamps")
    if isinstance(ts, dict) and "from" in ts and "to" in ts:

        def _ms(s: str) -> float:
            h, m, rest = str(s).replace(",", ".").split(":")
            return (int(h) * 3600 + int(m) * 60 + float(rest)) * 1000

        try:
            return _ms(ts["from"]), _ms(ts["to"])
        except (TypeError, ValueError):  # unexpected timestamp shape: skip token
            return None
    return None


SPECIAL_RE = re.compile(r"^\[_.*\]$")  # e.g. [_BEG_] control tokens


def _is_special(t: dict) -> bool:
    text = t.get("text", "").strip()
    return bool(SPECIAL_RE.match(text)) or int(t.get("id", 0)) >= 50257


def _token_chars(tokens: list[dict]) -> list[tuple[str, float, float]]:
    """whisper.cpp JSON tokens -> per-character (char, start_s, end_s), spreading each token's span."""
    chars = []
    for t in tokens:
        if _is_special(t):
            continue
        text = C.normalize(t.get("text", ""))
        span = _tok_span_ms(t)
        if not text or span is None:
            continue
        start, end = span[0] / 1000.0, span[1] / 1000.0
        step = max(end - start, 0.001) / len(text)
        for i, ch in enumerate(text):
            chars.append((ch, start + i * step, min(start + (i + 1) * step, end)))
    return chars


def stage(
    run: C.Run,
    ingest_dir: Path,
    model: str = "",
    language: str = "zh",
    threads: int = 0,
    vad: bool = False,
    reuse: bool = False,
) -> dict:
    out = run.stage("03_align")
    log = lambda m: A._log(out, m)

    bin_ = DEFAULT_BIN
    model_p = Path(model) if model else DEFAULT_MODEL
    if not bin_.exists():
        raise SystemExit(f"whisper-cli not found: {bin_} (set WHISPER_CPP_BIN)")
    if not model_p.exists():
        raise SystemExit(
            f"whisper.cpp model not found: {model_p} (set WHISPER_CPP_MODEL)"
        )

    segments = json.loads((ingest_dir / "segments.json").read_text(encoding="utf-8"))
    paragraphs = segments["paragraphs"]
    ref = [
        (ch, p["idx"], ci) for p in paragraphs for ci, ch in enumerate(p["normalized"])
    ]
    wav = ingest_dir / "audio_16k_mono.wav"

    json_path = out / "whisper.json"
    if reuse and json_path.exists():
        log("reuse existing whisper.json (skip transcription)")
    else:
        threads = threads or (os.cpu_count() or 8)
        cmd = [
            str(bin_),
            "-m",
            str(model_p),
            "-f",
            str(wav),
            "-l",
            language,
            "-ojf",
            "-of",
            str(out / "whisper"),
            "-np",
            "-t",
            str(threads),
        ]
        if vad:
            cmd.append("--vad")
        log(
            f"whisper.cpp: bin={bin_.name} model={model_p.name} threads={threads} lang={language}"
        )
        C.run(cmd, cwd=str(out))

    data = json.loads(json_path.read_text(encoding="utf-8"))
    (out / "raw.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    segs = data.get("transcription") or data.get("result", {}).get("segments", [])
    tokens = [t for seg in segs for t in seg.get("tokens", [])]
    log(f"tokens: {len(tokens)} (incl. control)")

    with (out / "ctm.tsv").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("start_ms\tend_ms\tword\n")
        for t in tokens:
            if _is_special(t):
                continue
            span = _tok_span_ms(t)
            word = t.get("text", "").strip()
            if span and word:
                fh.write(f"{round(span[0])}\t{round(span[1])}\t{word}\n")

    char_times = A._match(ref, _token_chars(tokens), log)
    (out / "char_times.json").write_text(
        json.dumps(
            [
                None if t is None else [round(t[0] * 1000), round(t[1] * 1000)]
                for t in char_times
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    duration_ms = C.ffprobe_duration_ms(wav)
    n_words = len(
        [t for t in tokens if t.get("text", "").strip() and not _is_special(t)]
    )
    return A._finalize(
        run,
        out,
        ingest_dir,
        paragraphs,
        ref,
        char_times,
        duration_ms,
        n_words,
        {
            "engine": "whisper.cpp",
            "model": model_p.name,
            "compute_type": "ggml-q8_0-cpu",
            "device": "cpu",
            "fallback": None,
        },
        [
            {
                "name": f"whisper.cpp-{model_p.name}",
                "role": "asr+align",
                "sha": "TODO:verify",
            }
        ],
        log,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--model", default="")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--vad", action="store_true")
    ap.add_argument(
        "--reuse", action="store_true", help="skip transcription if whisper.json exists"
    )
    args = ap.parse_args()
    target = C.require_file(C.resolve_input(args.target), "alignw")
    ingest = C.latest_stage(C.stem_of(target), "02_ingest")
    if not ingest:
        raise SystemExit("no ingest run found; run `just ingest <target>` first")
    run = C.Run(target)
    print(
        json.dumps(
            stage(
                run,
                ingest,
                args.model,
                args.language,
                args.threads,
                args.vad,
                args.reuse,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
