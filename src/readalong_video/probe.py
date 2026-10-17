"""Stage 01 (probe): inspect md segments, accompanying audio, and PDF text extractability."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from . import common as C


def probe_audio(mp3: Path | None) -> dict:
    if not mp3 or not mp3.exists():
        return {"status": "missing", "expected": str(mp3) if mp3 else None}
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(mp3),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        return {"status": "error", "stderr": out.stderr[-500:]}
    info = json.loads(out.stdout)
    stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {}
    )
    return {
        "status": "ok",
        "file": str(mp3),
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate", 0)),
        "channels": stream.get("channels"),
        "duration_ms": round(float(info.get("format", {}).get("duration", 0)) * 1000),
    }


def probe_pdf(pdf: Path | None) -> dict:
    if not pdf or not pdf.exists():
        return {"status": "missing", "expected": str(pdf) if pdf else None}
    import pymupdf

    doc = pymupdf.open(str(pdf))
    pages, empty = [], []
    for i, page in enumerate(doc):
        chars = len(page.get_text("text").strip())
        pages.append(
            {
                "page": i + 1,
                "chars": chars,
                "size": [round(page.rect.width), round(page.rect.height)],
            }
        )
        if chars < 5:
            empty.append(i + 1)
    fonts = sorted({f[3] for f in doc[0].get_fonts()}) if doc.page_count else []
    doc.close()
    return {
        "status": "ok",
        "file": str(pdf),
        "page_count": len(pages),
        "pages": pages,
        "page1_fonts": fonts,
        "near_empty_pages": empty,
        "text_extractable": not empty,
    }


def stage(
    run: C.Run, pdf: Path | None, mp3: Path | None, with_headings: bool = False
) -> dict:
    out = run.stage("01_probe")
    text_info = C.parse_md(run.target.read_text(encoding="utf-8"), with_headings)
    audio = probe_audio(mp3)
    pdf_info = probe_pdf(pdf)

    for name, data in (
        ("text_segments", text_info),
        ("audio_info", audio),
        ("pdf_info", pdf_info),
    ):
        (out / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )

    warning = []
    markers = {
        m["marker"] for p in text_info["paragraphs"] for m in p["inline_markers"]
    }
    defs = {f"[^{d['n']}]" for d in text_info["footnote_defs"]}
    if markers - defs:
        warning.append(f"inline markers without definition: {sorted(markers - defs)}")
    if defs - markers:
        warning.append(f"definitions never referenced: {sorted(defs - markers)}")

    summary = {
        "text": text_info["stats"],
        "audio": audio["status"],
        "pdf": {
            "status": pdf_info["status"],
            "page_count": pdf_info.get("page_count"),
            "text_extractable": pdf_info.get("text_extractable"),
        },
        "warnings": warning,
    }
    run.set_stage(
        "probe",
        {
            "status": "done",
            "dir": str(out),
            "audio_status": audio["status"],
            "pdf_status": pdf_info["status"],
            "pdf_path": str(pdf) if pdf else None,
            "with_headings": with_headings,
        },
    )
    return summary


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target")
    ap.add_argument("--media", default="", help="audio file or directory (see add)")
    ap.add_argument("--pdf", default="")
    ap.add_argument("--pdf-variant", default="")
    ap.add_argument("--pdf-dir", default="")
    ap.add_argument(
        "--with-headings",
        action="store_true",
        help="narration reads ## chapter titles aloud: anchor them too",
    )


def run(args: argparse.Namespace) -> None:
    target = C.require_file(C.resolve_input(args.target), "probe")
    mp3 = C.resolve_media(target, args.media, strict=False)
    pdf = (
        C.resolve_pdf(target, args.pdf, args.pdf_variant, args.pdf_dir)
        if (args.pdf or args.pdf_variant or C.env_dir("PDF_SOURCE") or args.pdf_dir)
        else None
    )
    r = C.Run(target)
    summary = stage(r, pdf, mp3, args.with_headings)
    print(json.dumps(summary, ensure_ascii=False))
