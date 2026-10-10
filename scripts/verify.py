"""Stage 04 (verify): timeline QC + per-page text mapping."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C


def timeline_qc(seg: dict) -> dict:
    paras = seg["paragraphs"]
    duration = seg["audio_duration_ms"]
    ok = [p for p in paras if p["status"] == "ok"]
    gaps = [
        {"idx": p["idx"], "src_line": p["src_line"], "text_head": p["display"][:30]}
        for p in paras
        if p["status"] != "ok"
    ]
    negative = [p["idx"] for p in ok if p["end_ms"] < p["start_ms"]]
    overlaps = [
        {"prev": a["idx"], "next": b["idx"], "overlap_ms": a["end_ms"] - b["start_ms"]}
        for a, b in zip(ok, ok[1:])
        if b["start_ms"] < a["end_ms"]
    ]
    out_of_range = [p["idx"] for p in ok if p["start_ms"] < 0 or p["end_ms"] > duration]
    durations = sorted((p["end_ms"] - p["start_ms"], p["idx"]) for p in ok)
    return {
        "duration_ms": duration,
        "paragraphs_total": len(paras),
        "paragraphs_ok": len(ok),
        "gaps": gaps,
        "negative": negative,
        "overlaps": overlaps,
        "out_of_range": out_of_range,
        "unmapped_lead_ms": ok[0]["start_ms"] if ok else None,
        "unmapped_tail_ms": (duration - ok[-1]["end_ms"]) if ok else None,
        "shortest": durations[:3],
        "longest": durations[-3:][::-1],
        "coverage_ms": sum(p["end_ms"] - p["start_ms"] for p in ok),
    }


def build_ref(segments: dict) -> tuple[str, list[tuple[int, int, int]]]:
    parts, spans, pos = [], [], 0
    for p in segments["paragraphs"]:
        n = p["normalized"]
        parts.append(n)
        spans.append((p["idx"], pos, pos + len(n)))
        pos += len(n)
    return "".join(parts), spans


def page_mapping(pdf: Path, ref: str, spans: list, seg: dict) -> list[dict]:
    import difflib

    import pymupdf

    doc = pymupdf.open(str(pdf))
    ok_paras = [p for p in seg["paragraphs"] if p["status"] == "ok"]
    pages, cursor = [], 0
    for i, page in enumerate(doc):
        norm = C.normalize(page.get_text("text"))
        start = ref.find(norm, cursor) if norm else -1
        if start < 0 and norm:
            m = difflib.SequenceMatcher(
                None, ref[cursor:], norm, autojunk=False
            ).find_longest_match(0, len(ref) - cursor, 0, len(norm))
            if m.size >= 10:
                start = cursor + m.a
        if start < 0:
            pages.append(
                {
                    "page": i + 1,
                    "norm_chars": len(norm),
                    "match": None,
                    "ratio": 0.0,
                    "audio_ms": 0,
                    "status": "unmatched",
                    "paras": [],
                }
            )
            continue
        end = min(start + len(norm), len(ref))
        cursor = max(cursor, end - 200)
        ratio = (
            difflib.SequenceMatcher(None, ref[start:end], norm, autojunk=False).ratio()
            if norm
            else 0.0
        )
        included = [
            p["idx"]
            for p in ok_paras
            if spans[p["idx"]][2] > start and spans[p["idx"]][1] < end
        ]
        pages.append(
            {
                "page": i + 1,
                "norm_chars": len(norm),
                "match": [start, end],
                "ratio": round(ratio, 3),
                "audio_ms": sum(
                    p["end_ms"] - p["start_ms"]
                    for p in ok_paras
                    if p["idx"] in included
                ),
                "status": "ok" if included else "zero_duration",
                "paras": included,
            }
        )
    doc.close()
    return pages


def stage(run: C.Run, align_dir: Path, ingest_dir: Path, pdf: Path | None) -> dict:
    out = run.stage("04_verify")
    seg = json.loads((align_dir / "segments_timed.json").read_text(encoding="utf-8"))
    segments = json.loads((ingest_dir / "segments.json").read_text(encoding="utf-8"))

    qc = {"timeline": timeline_qc(seg)}
    if pdf and pdf.exists():
        pages = page_mapping(pdf, *build_ref(segments), seg)
        (out / "page_map.json").write_text(
            json.dumps(pages, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        qc["pages"] = {
            "total": len(pages),
            "zero_duration": [
                p["page"] for p in pages if p["status"] == "zero_duration"
            ],
            "unmatched": [p["page"] for p in pages if p["status"] == "unmatched"],
            "low_match": [
                p["page"] for p in pages if p["status"] == "ok" and p["ratio"] < 0.6
            ],
        }
    else:
        qc["pages"] = {"error": "no pdf"}

    (out / "qc.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    tl = qc["timeline"]
    lines = [
        "# QC report",
        "",
        "## Timeline",
        "",
        f"- duration {tl['duration_ms']} ms; {tl['paragraphs_ok']}/{tl['paragraphs_total']} timed, {len(tl['gaps'])} gaps",
        f"- negative {len(tl['negative'])}; overlaps {len(tl['overlaps'])}; out-of-range {len(tl['out_of_range'])}",
        f"- unmapped audio: lead {tl['unmapped_lead_ms']} ms, tail {tl['unmapped_tail_ms']} ms",
        "",
    ]
    for g in tl["gaps"]:
        lines.append(f"- gap #{g['idx']} (line {g['src_line']}): {g['text_head']}")
    lines += ["", "## Pages", ""]
    pg = qc["pages"]
    lines.append(
        f"- total {pg.get('total', '-')}; zero-duration {pg.get('zero_duration', '-')}; "
        f"unmatched {pg.get('unmatched', '-')}; low-match {pg.get('low_match', '-')}"
    )
    (out / "qc_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )

    summary = {
        "timeline": {
            k: tl[k]
            for k in (
                "paragraphs_ok",
                "paragraphs_total",
                "coverage_ms",
                "unmapped_lead_ms",
                "unmapped_tail_ms",
            )
        },
        "gaps": len(tl["gaps"]),
        "pages": qc["pages"],
    }
    run.set_stage("verify", {"status": "done", "dir": str(out), "summary": summary})
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--pdf", default="")
    ap.add_argument("--pdf-variant", default="")
    ap.add_argument("--pdf-dir", default="")
    args = ap.parse_args()
    target = C.require_md(C.resolve_input(args.target), "verify")
    align = C.latest_stage(C.stem_of(target), "03_align", ready="align")
    if not align:
        raise SystemExit("no align run found; run `just align <target>` first")
    ingest = Path(
        json.loads((align.parent / "manifest.json").read_text(encoding="utf-8"))[
            "stages"
        ]["align"]["parent_ingest_run"]
    )
    ingest = C.OUTPUT / ingest.name / "02_ingest"
    pdf = C.resolve_pdf(target, args.pdf, args.pdf_variant, args.pdf_dir)
    run = C.Run(target)
    print(json.dumps(stage(run, align, ingest, pdf), ensure_ascii=False))


if __name__ == "__main__":
    main()
