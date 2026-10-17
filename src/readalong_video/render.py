"""Stage 05 (render): page highlights + turns -> mp4."""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import shutil
import subprocess
from pathlib import Path

from . import common as C

ZOOM = 2.0
# Front matter of the typeset PDF (copyright page, contents) collides with the
# reference in short fragments: index entries scatter across the whole book,
# single lines collide coincidentally ("...in wonderland"). Such blocks are
# filtered per block so a sheet that mixes front matter with the first body
# page keeps its genuine matches:
#   - blocks covering the same ref span are deduped by match quality (the
#     full body heading beats a contents entry matching only its title part);
#   - a block isolated from its neighbours on the ref axis is an index entry;
#   - (linear only) a block far past the read cursor is not the next page and
#     must not advance the cursor.
GAP_SLACK = 4  # genuine page blocks are contiguous: gaps stay a few chars
LEAD_SLACK = 1.5  # linear: the next page starts within ~1 page of the cursor


def _page_blocks(
    pdf: Path, ref: str, min_match: int = 8, layout: str = "auto"
) -> list[list[dict]]:
    import pymupdf

    doc = pymupdf.open(str(pdf))
    pages, cursor = [], 0
    for page in doc:
        page_norm = C.normalize(page.get_text("text"))
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
                {
                    "rect": [b[0], b[1], b[2], b[3]],
                    "ref": [start, start + m.size],
                    "q": m.size / max(len(norm), 1),
                }
            )
        blocks = _drop_index_blocks(blocks)
        if layout == "linear" and blocks:
            keep = [
                b
                for b in blocks
                if b["ref"][0] - cursor <= LEAD_SLACK * max(len(page_norm), 1)
            ]
            if keep:
                blocks = keep
                cursor = max(b["ref"][1] for b in blocks)
            else:
                blocks = []  # not the next page in reading order
        pages.append(blocks)
    doc.close()
    return pages


def _drop_index_blocks(blocks: list[dict]) -> list[dict]:
    """Drop contents/copyright-style blocks: when several blocks cover the
    same ref span keep the best match (the body heading over its contents
    entry), and drop matches isolated from every other survivor (index
    entries scattered across the book)."""
    if len(blocks) < 2:
        return blocks
    by_start = sorted(blocks, key=lambda b: b["ref"][0])
    kept, group, group_end = [], [by_start[0]], by_start[0]["ref"][1]
    for b in by_start[1:]:
        if b["ref"][0] < group_end:  # strictly overlapping, merely touching is fine
            group.append(b)
            group_end = max(group_end, b["ref"][1])
        else:
            kept.append(max(group, key=lambda b: b["q"]))
            group, group_end = [b], b["ref"][1]
    kept.append(max(group, key=lambda b: b["q"]))
    if len(kept) < 2:
        return kept  # a lone match cannot be an index entry
    out = []
    for i, b in enumerate(kept):
        s, e = b["ref"]
        gap = min(
            max(ps - e, 0) if ps >= e else max(s - pe, 0)
            for j, other in enumerate(kept)
            if j != i
            for ps, pe in (other["ref"],)
        )
        if gap <= GAP_SLACK * (e - s):
            out.append(b)
    return out


def _front_dwell(
    pdf: Path,
    pages_blocks: list[list[dict]],
    read_pages: set,
    dwell_ms: int,
    skip: set[int] | None = None,
) -> list[dict]:
    """Dwell frames for the leading unread pages: the video opens on page 1
    and lingers on each sheet not yet read, so the narration starts after a
    short pass-through. A wide (landscape 2-up) sheet counts its A5 columns
    separately, per the physical page count; the first partially read sheet
    dwells only on its unread columns. Skipped sheets (front matter the user
    does not want shown at all) are passed over silently."""
    import pymupdf

    dwells = []
    if dwell_ms <= 0:
        return dwells
    skip = skip or set()
    doc = pymupdf.open(str(pdf))
    for i, page in enumerate(doc):
        if i in skip:
            continue
        cols = 2 if page.rect.width > page.rect.height else 1
        if i in read_pages:  # first read sheet: dwell its unread columns, then stop
            half = page.rect.width / 2
            read_cols = {
                0 if (b["rect"][0] + b["rect"][2]) / 2 < half else 1
                for b in pages_blocks[i]
            }
            n_unread = cols - len(read_cols)
            if n_unread > 0:
                dwells.append({"page": i, "ms": n_unread * dwell_ms})
            break
        dwells.append({"page": i, "ms": cols * dwell_ms})
    doc.close()
    return dwells


def _mid_dwells(
    pdf: Path,
    pages_blocks: list[list[dict]],
    units: list[dict],
    dwell_ms: int,
    skip: set[int] | None = None,
) -> tuple[list, list]:
    """Dwells for unread sheets after the first narrated one (chapter-end /
    book-end endnotes, illustrations). Returns (mid, trailing): mid is
    [(at_ms, ms, page)] sorted by at_ms -- silence is spliced into the audio
    at at_ms and frames at/after it are shifted; trailing is [{"page", "ms}]
    appended after the narration ends. Column sides are respected: an unread
    column left of the read one is passed through before the sheet's
    narration, an unread column right of it after. Skipped sheets are never
    shown."""
    import pymupdf

    if dwell_ms <= 0 or not units:
        return [], []
    skip = skip or set()
    first_read = min(u["page"] for u in units)
    last_read = max(u["page"] for u in units)
    starts, ends = {}, {}
    for u in units:
        starts[u["page"]] = min(starts.get(u["page"], u["t0"]), u["t0"])
        ends[u["page"]] = max(ends.get(u["page"], u["t1"]), u["t1"])

    def next_start(after_page: int, floor: int) -> int | None:
        cand = [u["t0"] for u in units if u["page"] > after_page and u["t0"] >= floor]
        return min(cand) if cand else None

    mid, trail, floor = [], [], 0
    doc = pymupdf.open(str(pdf))
    for i in range(first_read + 1, doc.page_count):
        if i in skip:
            continue
        page = doc[i]
        cols = 2 if page.rect.width > page.rect.height else 1
        half = page.rect.width / 2
        read_cols = {
            0 if (b["rect"][0] + b["rect"][2]) / 2 < half else 1
            for b in pages_blocks[i]
        }
        if len(read_cols) == cols:
            continue  # fully read sheet
        ms = (cols - len(read_cols)) * dwell_ms
        if i > last_read:
            trail.append({"page": i, "ms": ms})
            continue
        if not read_cols:
            at = next_start(i, floor)
        else:
            hi = max(read_cols)
            unread = [c for c in range(cols) if c not in read_cols]
            if all(c > hi for c in unread):
                at = ends.get(i)  # pass through after this sheet's narration
            else:
                at = starts.get(i)  # pass through before it
            if at is None:
                # matched blocks but no unit landed here (duplicated text on
                # another sheet won the page): splice before the next one
                at = next_start(i, floor)
        if at is None:
            trail.append({"page": i, "ms": ms})
        else:
            at = max(at, floor)
            mid.append((at, ms, i))
            floor = at
    doc.close()
    return mid, trail


def _splice_plan(
    duration_ms: int, lead_ms: int, mid: list, trail_ms: int
) -> tuple[int, list, int]:
    """Resolve mid-book dwells into a concrete splice plan: folded lead /
    trailing silence and [(at, ms)] points sorted, one entry per unique splice
    instant. Dwells at/before 0 or at/after the audio end fold into the
    lead/trailing silence; dwells sharing one instant (a sheet's narration
    ends exactly where the next begins) merge into a single longer silence,
    so no zero-length cut is ever emitted."""
    points = []  # [(at, ms)], one entry per unique splice instant
    for at, ms, _ in sorted(mid, key=lambda m: m[0]):
        if at <= 0:
            lead_ms += ms
        elif at >= duration_ms:
            trail_ms += ms
        elif points and points[-1][0] == at:
            points[-1] = (at, points[-1][1] + ms)
        else:
            points.append((at, ms))
    return lead_ms, points, trail_ms


def _audio_concat(
    out: Path,
    wav: Path,
    lead_ms: int,
    points: list,
    trail_ms: int,
) -> Path:
    """concat list that splices silence into the narration wav: the front
    lead, a gap at each dwell point, trailing silence. Plain stream concat on
    purpose -- a filter_complex graph misbehaves here (its anullsrc segments
    terminate early once -t/-shortest and the video encode join the command).
    One generated silence wav is cut by duration directives; narration
    segments are addressed by inpoint, exact at 16 kHz (ms -> whole samples)."""
    sec = lambda ms: f"{ms / 1000:.3f}"  # noqa: E731

    longest = max([lead_ms, trail_ms, *(ms for _, ms in points)])
    sil = out / "silence.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            sec(longest),
            str(sil),
        ],
        check=True,
    )
    # relative name on purpose: the demuxer resolves it next to the list and
    # special characters in the real path stay out of the file tokens
    narration = out / "narration.wav"
    shutil.copyfile(wav, narration)
    lines, prev = [], 0
    if lead_ms:
        lines += [f"file '{sil.name}'", f"duration {sec(lead_ms)}"]
    for at, ms in points:
        lines += [
            f"file '{narration.name}'",
            f"inpoint {sec(prev)}",
            f"duration {sec(at - prev)}",
            f"file '{sil.name}'",
            f"duration {sec(ms)}",
        ]
        prev = at
    lines.append(f"file '{narration.name}'")
    if prev:
        lines.append(f"inpoint {sec(prev)}")
    if trail_ms:
        lines += [f"file '{sil.name}'", f"duration {sec(trail_ms)}"]
    lst = out / "audio_concat.txt"
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return lst


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
    layout: str = "auto",
    output: str = "",
    dwell_s: float = 2.0,
    mid_dwell_s: float = 2.0,
    skip_pages: set[int] | None = None,
    turn_dwell_s: float = 2.0,
    end_dwell_s: float = 2.0,
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

    ok_spans = []  # (g_start, g_end, start_ms, end_ms) per timed-ok paragraph
    for p in timed["paragraphs"]:
        if p["status"] != "ok" or not para_norm.get(p["idx"]):
            continue
        g0 = para_off[p["idx"]]
        ok_spans.append((g0, g0 + len(para_norm[p["idx"]]), p["start_ms"], p["end_ms"]))

    def interp(pos: int) -> int:
        """Linear-interp time (ms) at a global ref position (no char anchors)."""
        for g0, g1, t0, t1 in ok_spans:
            if g0 <= pos <= g1:
                n = g1 - g0
                return round(t0 + (0 if n == 0 else (pos - g0) / n) * (t1 - t0))
        if not ok_spans:
            return 0
        if pos < ok_spans[0][0]:
            return ok_spans[0][2]
        return ok_spans[-1][3]

    units = []
    if unit == "line":
        # one unit per matched PDF text line (blocks are single lines for
        # LaTeX-typeset PDFs): uniform whole-line highlights that track the
        # audio line by line
        for pi, blocks in enumerate(pages_blocks):
            for b in blocks:
                rs, re_ = b["ref"]
                t0 = anchors.at(rs, interp(rs), True) if anchors else interp(rs)
                t1 = (
                    anchors.at(re_ - 1, interp(re_ - 1), False)
                    if anchors
                    else interp(re_ - 1)
                )
                units.append(
                    {
                        "page": pi,
                        "t0": t0,
                        "t1": max(t1, t0 + 80),
                        "g0": rs,
                        "g1": re_ - 1,
                    }
                )
    else:
        for p in timed["paragraphs"]:
            if p["status"] != "ok" or not para_norm.get(p["idx"]):
                continue
            text = para_norm[p["idx"]]
            base = para_off[p["idx"]]
            rs, re_ = 0, len(text)
            if anchors:
                t0 = anchors.at(base + rs, p["start_ms"], True)
                t1 = anchors.at(base + re_ - 1, p["end_ms"], False)
                # char anchors may leak into a neighbouring paragraph (equal
                # runs wandering across short/shared passages): keep the whole
                # highlight within this paragraph's own timed span (+-1 s)
                lo, hi = p["start_ms"] - 1000, p["end_ms"] + 1000
                t0 = min(max(t0, lo), hi)
                t1 = min(max(t1, lo), hi)
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
                prev["narr_end"] = max(prev["narr_end"], u["t1"])
                continue
            if u["t0"] > prev["t1"]:
                prev["t1"] = u["t0"]
        frames.append(
            {
                "page": u["page"],
                "t0": u["t0"],
                "t1": u["t1"],
                "rects": rects,
                "narr_end": u["t1"],
            }
        )

    # page-turn dwell: after a page's last paragraph, linger ~1s on the cleared
    # page, then turn; the next page sits unlit until its audio starts --
    # instead of holding the old highlight until the next audio appears
    if turn_dwell_s > 0:
        turn_ms = round(turn_dwell_s * 1000)
        out_frames = []
        for fr in frames:
            if out_frames:
                prev = out_frames[-1]
                gap = fr["t0"] - prev["narr_end"]
                if prev["rects"] and fr["page"] != prev["page"]:
                    end = prev["narr_end"]
                    prev["t1"] = end  # drop the freeze-extension; turn here
                    turn_at = end + turn_ms if gap >= turn_ms else end
                    out_frames.append({**prev, "rects": [], "t0": end, "t1": turn_at})
                    if turn_at < fr["t0"]:
                        out_frames.append(
                            {**fr, "rects": [], "t0": turn_at, "t1": fr["t0"]}
                        )
            out_frames.append(fr)
        for fr in out_frames:
            fr.pop("narr_end", None)
        frames = out_frames

    duration = timed["audio_duration_ms"]
    if frames[0]["t0"] > 0:
        # lead-in: nothing is read yet (e.g. a narrated chapter title that is
        # not part of the reference text) -- show the page without lighting
        # the first unit ahead of time
        frames.insert(0, {**frames[0], "rects": [], "t0": 0, "t1": frames[0]["t0"]})
    if frames[-1]["t1"] < duration:
        # tail: narration is over (e.g. an outro with no text counterpart) --
        # show the page but light nothing, like the lead-in
        frames.append(
            {**frames[-1], "rects": [], "t0": frames[-1]["t1"], "t1": duration}
        )
    if end_dwell_s > 0:
        # let the final page linger a moment instead of stopping dead at the
        # last audio sample
        frames.append(
            {
                **frames[-1],
                "rects": [],
                "t0": frames[-1]["t1"],
                "t1": frames[-1]["t1"] + round(end_dwell_s * 1000),
            }
        )

    # pass-through dwells: front-matter sheets open the video before the
    # narration; mid-book unread sheets (chapter-end endnotes etc.) splice
    # silence into the audio at the next paragraph boundary; trailing sheets
    # run after the narration ends. Frame times shift by the accumulated
    # silence; a frame ending exactly at a splice keeps the pre-splice time.
    # Skipped sheets (1-based page numbers) never appear in any dwell list.
    skip0 = {p - 1 for p in (skip_pages or set())}
    front = (
        _front_dwell(
            pdf,
            pages_blocks,
            {f["page"] for f in frames},
            round(dwell_s * 1000),
            skip0,
        )
        if dwell_s > 0
        else []
    )
    mid, trail = (
        _mid_dwells(pdf, pages_blocks, units, round(mid_dwell_s * 1000), skip0)
        if mid_dwell_s > 0
        else ([], [])
    )
    mid = sorted(mid, key=lambda m: m[0])
    lead_ms = sum(d["ms"] for d in front)
    trail_ms = sum(d["ms"] for d in trail)
    lead_ms, points, trail_ms = _splice_plan(duration, lead_ms, mid, trail_ms)
    # dwell frames show every in-range mid dwell (two dwells sharing one
    # splice instant play back to back); narration frames shift by the merged
    # plan totals, which is what the audio splice inserts
    vis_mid = [(at, ms, page) for at, ms, page in mid if 0 < at < duration]

    def cum_start(t: int) -> int:
        return lead_ms + sum(ms for at, ms in points if t >= at)

    def cum_end(t: int) -> int:
        return lead_ms + sum(ms for at, ms in points if t > at)

    extra, acc = [], 0
    for d in front:
        extra.append({"page": d["page"], "rects": [], "t0": acc, "t1": acc + d["ms"]})
        acc += d["ms"]
    run_shift = lead_ms
    for at, ms, page in vis_mid:
        extra.append(
            {"page": page, "rects": [], "t0": at + run_shift, "t1": at + run_shift + ms}
        )
        run_shift += ms
    for fr in frames:
        fr["t0"] += cum_start(fr["t0"])
        fr["t1"] += cum_end(fr["t1"])
    end = frames[-1]["t1"]
    for d in trail:
        extra.append({"page": d["page"], "rects": [], "t0": end, "t1": end + d["ms"]})
        end += d["ms"]
    if extra:
        frames = sorted(extra + frames, key=lambda f: (f["t0"], f["t1"]))

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
        # Relative paths on purpose: the concat demuxer parses ' as a quote
        # delimiter, so an absolute path containing ' (alice's-...) breaks
        # the token. Relative names keep special chars out of concat.txt;
        # the demuxer resolves them against the concat file's directory.
        concat.append(
            f"file 'frames/{fp.name}'\nduration {max(1, fr['t1'] - fr['t0']) / 1000:.3f}"
        )
        last = fp
    concat.append(f"file 'frames/{last.name}'")
    (out / "concat.txt").write_text(
        "\n".join(concat) + "\n", encoding="utf-8", newline="\n"
    )

    reader = C.reader_of(Path(segments.get("audio", "")).stem)
    stem = f"{run.stem}{{{reader}}}" if reader else run.stem
    mp4 = C.resolve_output(output, out, f"{stem}.mp4")
    cmd = [
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
    ]
    if points or lead_ms or trail_ms or end_dwell_s > 0:
        # dwells exist: splice silence into the audio via a concat list. A
        # filter_complex graph misbehaves here -- its anullsrc segments
        # terminate early once -t/-shortest and the video encode join the
        # command -- while the plain stream concat is exact (verified). The
        # video concat loops its last frame, so -t pins the output length;
        # the audio list itself ends exactly at total_ms.
        trail_ms += round(end_dwell_s * 1000)  # end dwell = trailing silence,
        # otherwise the muxer stops at the audio EOF and cuts the dwell frames
        total_ms = duration + lead_ms + sum(ms for _, ms in points) + trail_ms
        audio = _audio_concat(
            out, ingest_dir / "audio_16k_mono.wav", lead_ms, points, trail_ms
        )
        cmd += ["-f", "concat", "-safe", "0", "-i", str(audio)]
        end_args = ["-t", f"{total_ms / 1000:.3f}"]
    else:
        cmd += ["-i", str(ingest_dir / "audio_16k_mono.wav")]
        end_args = ["-shortest"]
    cmd += (
        [
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
            "-g",
            "1",  # the concat input is a ~0.05fps slideshow: a time-based keyint
            # yields a single keyframe at t=0 and seeking snaps back to start
            "-x264-params",
            "keyint=1:min-keyint=1",
            "-movflags",
            "+faststart",  # moov at the head: progress-bar dragging works before full download
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            "-maxrate",
            "192k",
            "-minrate",
            "64k",
        ]
        + end_args
    )
    cmd.append(str(mp4))
    subprocess.run(cmd, check=True)

    summary = {
        "unit": unit,
        "layout": layout,
        "timing": "char-anchors" if anchors else "linear-interp",
        "audio_duration_ms": duration,
        "front_dwell_ms": lead_ms,
        "mid_dwells": len(vis_mid),
        "trailing_dwell_ms": trail_ms,
        "skipped_pages": sorted(p for p in (skip_pages or set())),
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


def add_args(ap: argparse.ArgumentParser) -> None:
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
    ap.add_argument(
        "--unit",
        default="paragraph",
        choices=["paragraph", "line"],
        help="highlight granularity: whole paragraph, or one PDF text line at a time",
    )
    ap.add_argument(
        "--dwell-s",
        type=float,
        default=2.0,
        help="seconds per unread A5 page shown before the narration starts "
        "(front-matter pass-through; 0 disables)",
    )
    ap.add_argument(
        "--mid-dwell-s",
        type=float,
        default=2.0,
        help="seconds per unread A5 page passed through mid-book or after "
        "the narration (chapter-end/book-end endnotes; 0 disables)",
    )
    ap.add_argument(
        "--skip-pages",
        default="",
        metavar="RANGES|auto",
        help="sheets never shown in the video: comma ranges like '1-3,7,12-' "
        "(1-based), or 'auto' to derive front matter from the PDF outline "
        "(needs a copys --outline/--toc PDF: pages before the first outline "
        "target plus contents/copyright-titled entries)",
    )
    ap.add_argument(
        "--turn-dwell-s",
        type=float,
        default=2.0,
        help="seconds a cleared page lingers after its last paragraph before "
        "the page turns (0 disables)",
    )
    ap.add_argument(
        "--end-dwell-s",
        type=float,
        default=2.0,
        help="seconds the final page lingers after the narration ends (0 disables)",
    )


def run(args: argparse.Namespace) -> None:
    target = C.require_file(C.resolve_input(args.target), "render")
    align = C.latest_stage(target, "03_align", ready="align")
    if not align:
        raise SystemExit("no align run found; run `readalong align <target>` first")
    ingest_name = json.loads(
        (align.parent / "manifest.json").read_text(encoding="utf-8")
    )["stages"]["align"]["parent_ingest_run"]
    ingest = C.runs_root(target) / ingest_name / "02_ingest"
    pdf = C.resolve_pdf(target, args.pdf, args.pdf_variant, args.pdf_dir)
    skip_pages: set[int] = set()
    if args.skip_pages:
        if args.skip_pages.strip().lower() == "auto":
            skip_pages, reasons = C.auto_skip_pages(pdf)
            for r in reasons:
                print(f"skip-pages auto: {r}")
        else:
            skip_pages = C.parse_page_ranges(args.skip_pages)
    run = C.Run(target)
    print(
        json.dumps(
            stage(
                run,
                align,
                ingest,
                pdf,
                args.unit,
                args.layout,
                args.output,
                args.dwell_s,
                args.mid_dwell_s,
                skip_pages,
                args.turn_dwell_s,
                args.end_dwell_s,
            ),
            ensure_ascii=False,
        )
    )
