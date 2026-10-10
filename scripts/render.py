"""Stage 05 (render): page highlights + turns -> mp4."""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C

ZOOM = 2.0
# CJK punctuation plus the ASCII forms that NFKC normalization maps them to
# (，->, 。->. ！->! ？->? ；->; ：->: …->...); 、 keeps its codepoint.
PUNCT = set("。！？；，、…,.!?;:")


def _page_blocks(
    pdf: Path, ref: str, min_match: int = 8, layout: str = "auto"
) -> list[list[dict]]:
    import pymupdf

    doc = pymupdf.open(str(pdf))
    pages, cursor = [], 0
    for page in doc:
        blocks = []
        for b in page.get_text("blocks"):
            norm = C.normalize(b[4])
            if len(norm) < min_match:
                continue
            base = ref[cursor:] if layout == "linear" else ref
            m = difflib.SequenceMatcher(
                None, base, norm, autojunk=False
            ).find_longest_match(0, len(base), 0, len(norm))
            if m.size < min_match:
                continue
            start = cursor + m.a if layout == "linear" else m.a
            blocks.append(
                {"rect": [b[0], b[1], b[2], b[3]], "ref": [start, start + m.size]}
            )
        if layout == "linear" and blocks:
            cursor = max(b["ref"][1] for b in blocks)
        pages.append(blocks)
    doc.close()
    return pages


def _paragraph_page(segments: dict, pages_blocks: list[list[dict]]) -> dict:
    spans, pos = [], 0
    for p in segments["paragraphs"]:
        n = p["normalized"]
        spans.append((p["idx"], pos, pos + len(n)))
        pos += len(n)
    result = {}
    for idx, ps, pe in spans:
        best, best_ov = None, 0
        for pi, blocks in enumerate(pages_blocks):
            ov = sum(
                max(0, min(be, pe) - max(bs, ps))
                for bs, be in (b["ref"] for b in blocks)
            )
            if ov > best_ov:
                best, best_ov = pi, ov
        result[idx] = best
    return result


def _split_chars(text: str, target: int) -> list[tuple[int, int]]:
    """Split at the punctuation nearest each target offset (window [target/2, 2*target])."""
    length = len(text)
    out, pos = [], 0
    gap = max(6, target // 2)
    while pos < length:
        if length - pos <= target + gap:
            out.append((pos, length))
            break
        target_pos = pos + target
        lo, hi = pos + gap, min(length - 1, pos + 2 * target)
        best = None
        for i in range(lo, hi + 1):
            if text[i] in PUNCT and (
                best is None or abs(i - target_pos) < abs(best - target_pos)
            ):
                best = i
        cut = best + 1 if best is not None else target_pos
        if cut <= pos:
            cut = min(pos + target, length)
        out.append((pos, cut))
        pos = cut
    return out or [(0, length)]


class _Anchors:
    """Char-level start/end times (ms); index-aligned with the reference stream."""

    def __init__(self, anchors: list):
        self.start = [a[0] if a else None for a in anchors]
        self.end = [a[1] if a else None for a in anchors]
        self.known = [i for i, a in enumerate(anchors) if a]

    def at(self, index: int, fallback: int, forward: bool) -> int:
        if not self.known:
            return fallback
        k = bisect.bisect_left(self.known, index)
        if forward:
            j = self.known[k] if k < len(self.known) else self.known[-1]
        else:
            j = self.known[k - 1] if k - 1 >= 0 else self.known[0]
        return self.start[j] if forward else self.end[j]


def stage(
    run: C.Run,
    align_dir: Path,
    ingest_dir: Path,
    pdf: Path,
    unit: str = "paragraph",
    chars: int = 20,
    layout: str = "auto",
    output: str = "",
) -> dict:
    out = run.stage("05_render")
    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)

    timed = json.loads((align_dir / "segments_timed.json").read_text(encoding="utf-8"))
    segments = json.loads((ingest_dir / "segments.json").read_text(encoding="utf-8"))

    parts, pos, para_off, para_norm = [], 0, {}, {}
    for p in segments["paragraphs"]:
        para_off[p["idx"]] = pos
        para_norm[p["idx"]] = p["normalized"]
        parts.append(p["normalized"])
        pos += len(p["normalized"])
    ref = "".join(parts)

    anchors_file = align_dir / "char_times.json"
    anchors = (
        _Anchors(json.loads(anchors_file.read_text(encoding="utf-8")))
        if anchors_file.exists()
        else None
    )

    pages_blocks = _page_blocks(pdf, ref, layout=layout)
    para_page = _paragraph_page(segments, pages_blocks)

    units = []
    for p in timed["paragraphs"]:
        if p["status"] != "ok" or not para_norm.get(p["idx"]):
            continue
        text = para_norm[p["idx"]]
        base = para_off[p["idx"]]
        cuts = [(0, len(text))] if unit == "paragraph" else _split_chars(text, chars)
        for rs, re_ in cuts:
            if anchors:
                t0 = anchors.at(base + rs, p["start_ms"], True)
                t1 = anchors.at(base + re_ - 1, p["end_ms"], False)
            else:
                span = p["end_ms"] - p["start_ms"]
                t0 = round(p["start_ms"] + rs / len(text) * span)
                t1 = round(p["start_ms"] + re_ / len(text) * span)
            units.append(
                {
                    "page": para_page.get(p["idx"]),
                    "t0": t0,
                    "t1": max(t1, t0 + 80),
                    "g0": base + rs,
                    "g1": base + re_ - 1,
                }
            )

    units = sorted((u for u in units if u["page"] is not None), key=lambda u: u["t0"])
    if not units:
        raise SystemExit("no highlight units")

    def rects_for(page_idx: int, g0: int, g1: int) -> list[list[int]]:
        return [
            [
                round(b["rect"][0] * ZOOM),
                round(b["rect"][1] * ZOOM),
                round(b["rect"][2] * ZOOM),
                round(b["rect"][3] * ZOOM),
            ]
            for b in pages_blocks[page_idx]
            if b["ref"][1] > g0 and b["ref"][0] <= g1
        ]

    frames = []
    for u in units:
        rects = rects_for(u["page"], u["g0"], u["g1"])
        if frames:
            prev = frames[-1]
            if prev["page"] == u["page"] and prev["rects"] == rects:
                prev["t1"] = max(prev["t1"], u["t1"])
                continue
            if u["t0"] > prev["t1"]:
                prev["t1"] = u["t0"]
        frames.append({"page": u["page"], "t0": u["t0"], "t1": u["t1"], "rects": rects})

    duration = timed["audio_duration_ms"]
    if frames[0]["t0"] > 0:
        frames.insert(0, {**frames[0], "t0": 0, "t1": frames[0]["t0"]})
    if frames[-1]["t1"] < duration:
        frames.append({**frames[-1], "t0": frames[-1]["t1"], "t1": duration})

    import pymupdf
    from PIL import Image, ImageDraw

    doc = pymupdf.open(str(pdf))
    page_png, page_size = {}, None
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
        fp = out / "pages" / f"page{i + 1}.png"
        fp.parent.mkdir(exist_ok=True)
        pix.save(str(fp))
        page_png[i] = fp
        page_size = page_size or (pix.width, pix.height)
    doc.close()

    concat, cache, last = [], {}, None
    for n, fr in enumerate(frames):
        base_img = cache.setdefault(
            fr["page"], Image.open(page_png[fr["page"]]).convert("RGBA")
        )
        img = base_img.copy()
        if fr["rects"]:
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            for x0, y0, x1, y1 in fr["rects"]:
                draw.rectangle([x0, y0, x1, y1], fill=(255, 235, 0, 90))
            img = Image.alpha_composite(img, overlay)
        fp = frames_dir / f"{n:05d}.png"
        img.convert("RGB").save(fp)
        concat.append(
            f"file '{fp.as_posix()}'\nduration {max(1, fr['t1'] - fr['t0']) / 1000:.3f}"
        )
        last = fp
    concat.append(f"file '{last.as_posix()}'")
    (out / "concat.txt").write_text(
        "\n".join(concat) + "\n", encoding="utf-8", newline="\n"
    )

    reader = C.reader_of(Path(segments.get("audio", "")).stem)
    stem = f"{run.stem}{{{reader}}}" if reader else run.stem
    mp4 = C.resolve_output(output, out, f"{stem}.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(out / "concat.txt"),
            "-i",
            str(ingest_dir / "audio_16k_mono.wav"),
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(mp4),
        ],
        check=True,
    )

    summary = {
        "unit": unit,
        "chars": chars if unit == "chars" else None,
        "layout": layout,
        "timing": "char-anchors" if anchors else "linear-interp",
        "audio_duration_ms": duration,
        "frames": len(frames),
        "page_size": page_size,
        "pages_used": sorted({f["page"] + 1 for f in frames}),
        "mp4": str(mp4),
    }
    (out / "render_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    run.set_stage(
        "render",
        {"status": "done", "dir": str(out), "mp4": str(mp4), "summary": summary},
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--pdf", default="")
    ap.add_argument("--pdf-variant", default="")
    ap.add_argument("--pdf-dir", default="")
    ap.add_argument("--layout", default="auto", choices=["auto", "linear"])
    ap.add_argument(
        "--output",
        default="",
        help="redirect the mp4: a directory, a new file name, or an absolute path for both",
    )
    ap.add_argument("--unit", default="paragraph", choices=["paragraph", "chars"])
    ap.add_argument("--chars", type=int, default=0, help="implies --unit chars")
    ap.add_argument("--paragraphs", action="store_true")
    ap.add_argument("--words", type=int, default=0, help="alias of --chars")
    args = ap.parse_args()
    if args.paragraphs:
        args.unit = "paragraph"
    if args.words:
        args.chars = args.words
    if args.chars:
        args.unit = "chars"

    target = C.require_md(C.resolve_input(args.target), "render")
    align = C.latest_stage(C.stem_of(target), "03_align", ready="align")
    if not align:
        raise SystemExit("no align run found; run `just align <target>` first")
    ingest_name = json.loads(
        (align.parent / "manifest.json").read_text(encoding="utf-8")
    )["stages"]["align"]["parent_ingest_run"]
    ingest = C.OUTPUT / ingest_name / "02_ingest"
    pdf = C.resolve_pdf(target, args.pdf, args.pdf_variant, args.pdf_dir)
    run = C.Run(target)
    print(
        json.dumps(
            stage(
                run, align, ingest, pdf, args.unit, args.chars, args.layout, args.output
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
