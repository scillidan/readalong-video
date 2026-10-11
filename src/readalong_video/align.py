"""Stage 03 (align): whisperX forced alignment -> paragraph timestamps + char-level anchors."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import common as C

# The host environment sets HF_HUB_ENABLE_HF_TRANSFER=1 without the package installed; disable it.
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
# Weights are pre-fetched; stay offline so hub revision checks cannot fail on a flaky mirror.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _log(out: Path, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (out / "_status.txt").open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def _build_asr_chars(words: list[dict]) -> list[tuple[str, float, float]]:
    """Word items -> per-character (char, start, end), spreading each word's span over its chars."""
    chars = []
    for w in words:
        word = C.normalize(w["word"])
        if not word:
            continue
        span = max(w["end"] - w["start"], 0.001)
        step = span / len(word)
        for i, ch in enumerate(word):
            chars.append(
                (ch, w["start"] + i * step, min(w["start"] + (i + 1) * step, w["end"]))
            )
    return chars


def _match(ref: list, asr_chars: list, log) -> list:
    import difflib

    ref_str = "".join(c for c, _, _ in ref)
    asr_str = "".join(c for c, _, _ in asr_chars)
    log(f"matching: ref {len(ref_str)} vs asr {len(asr_str)} chars")
    matcher = difflib.SequenceMatcher(None, ref_str, asr_str, autojunk=False)
    anchors: dict[int, tuple[float, float]] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                anchors[i1 + k] = (asr_chars[j1 + k][1], asr_chars[j1 + k][2])
    log(
        f"anchored {len(anchors)}/{len(ref_str)} chars ({100 * len(anchors) / max(len(ref_str), 1):.1f}%)"
    )
    return [anchors.get(i) for i in range(len(ref))]


def stage(
    run: C.Run, ingest_dir: Path, model: str = "large-v3", compute_type: str = "float16"
) -> dict:
    out = run.stage("03_align")
    log = lambda m: _log(out, m)

    import torch
    import whisperx

    segments = json.loads((ingest_dir / "segments.json").read_text(encoding="utf-8"))
    paragraphs = segments["paragraphs"]
    ref = [
        (ch, p["idx"], ci) for p in paragraphs for ci, ch in enumerate(p["normalized"])
    ]

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu" and compute_type == "float16":
        compute_type = "int8"
    log(f"device={device} compute_type={compute_type} model={model}")

    log("loading whisper model...")
    asr = whisperx.load_model(model, device, compute_type=compute_type, language="zh")
    audio = whisperx.load_audio(str(ingest_dir / "audio_16k_mono.wav"))

    fallback = None
    log("transcribe...")
    try:
        result = asr.transcribe(audio, batch_size=16, language="zh")
    except Exception as exc:  # noqa: BLE001 - fall back to silero VAD on any VAD/model-load failure
        log(f"transcribe failed ({type(exc).__name__}); fallback to silero VAD")
        fallback = f"vad->silero ({type(exc).__name__})"
        result = asr.transcribe(
            audio, batch_size=16, language="zh", vad_method="silero"
        )

    log(f"ASR segments: {len(result['segments'])}; loading align model...")
    align_model, metadata = whisperx.load_align_model(language_code="zh", device=device)
    log("forced alignment...")
    result = whisperx.align(
        result["segments"],
        align_model,
        metadata,
        audio,
        device,
        return_char_alignments=False,
    )
    (out / "raw.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    words = [
        w
        for seg in result["segments"]
        for w in seg.get("words", [])
        if "start" in w and "end" in w
    ]
    with (out / "ctm.tsv").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("start_ms\tend_ms\tword\n")
        for w in words:
            fh.write(
                f"{round(w['start'] * 1000)}\t{round(w['end'] * 1000)}\t{w['word'].strip()}\n"
            )

    char_times = _match(ref, _build_asr_chars(words), log)
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

    duration_ms = round(len(audio) / 16)
    return _finalize(
        run,
        out,
        ingest_dir,
        paragraphs,
        ref,
        char_times,
        duration_ms,
        len(words),
        {
            "engine": "whisperx",
            "model": model,
            "compute_type": compute_type,
            "device": device,
            "fallback": fallback,
        },
        [
            {"name": f"faster-whisper-{model}", "role": "asr", "sha": "TODO:verify"},
            {"name": "wav2vec2-zh-align", "role": "forced-align", "sha": "TODO:verify"},
        ],
        log,
    )


def _finalize(
    run,
    out,
    ingest_dir,
    paragraphs,
    ref,
    char_times,
    duration_ms,
    n_words,
    engine_meta,
    models,
    log,
):
    """Shared tail for both align engines: per-paragraph timing, artifacts, manifest."""
    by_para: dict[int, list] = {}
    for i, t in enumerate(char_times):
        if t is not None:
            by_para.setdefault(ref[i][1], []).append(t)

    timed, gaps = [], 0
    for p in paragraphs:
        ts = by_para.get(p["idx"], [])
        if not ts:
            gaps += 1
            timed.append({**p, "start_ms": None, "end_ms": None, "status": "gap"})
        else:
            timed.append(
                {
                    **p,
                    "start_ms": round(ts[0][0] * 1000),
                    "end_ms": round(ts[-1][1] * 1000),
                    "status": "ok",
                }
            )

    (out / "segments_timed.json").write_text(
        json.dumps(
            {"paragraphs": timed, "audio_duration_ms": duration_ms},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )

    ok = len(paragraphs) - gaps
    coverage = sum(p["end_ms"] - p["start_ms"] for p in timed if p["status"] == "ok")
    summary = {
        **engine_meta,
        "paragraphs_total": len(paragraphs),
        "paragraphs_ok": ok,
        "paragraphs_gap": gaps,
        "coverage_ms": coverage,
        "audio_duration_ms": duration_ms,
        "words": n_words,
    }
    (out / "align_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    run.set_stage(
        "align",
        {
            "status": "done",
            "dir": str(out),
            "summary": summary,
            "parent_ingest_run": ingest_dir.parent.name,
        },
    )
    run.manifest.setdefault("models", []).extend(models)
    run.save()
    log(f"DONE ok={ok} gap={gaps} coverage={coverage / 1000:.1f}s")
    return summary


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--compute-type", default="float16")


def run(args: argparse.Namespace) -> None:
    target = C.require_file(C.resolve_input(args.target), "align")
    ingest = C.latest_stage(target, "02_ingest")
    if not ingest:
        raise SystemExit("no ingest run found; run `readalong ingest <target>` first")
    run = C.Run(target)
    print(
        json.dumps(
            stage(run, ingest, args.model, args.compute_type), ensure_ascii=False
        )
    )
