"""Full pipeline: standard .md (file or dir) + audio via --media -> aligned mp4 on PDF pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C
import align
import gen_pdf
import ingest
import probe
import render
import report
import verify


def default_layout(gen: str, variant: str) -> str:
    """a4/a5 pages are in reading order (linear); imposed booklets and unknown
    PDFs are matched by content (auto)."""
    if gen in ("a4", "a5") or variant in ("a4", "a5"):
        return "linear"
    return "auto"


def process(md: Path, args: argparse.Namespace) -> dict:
    mp3 = C.resolve_media(md, args.media)
    gen = ""
    if args.gen_a4 and args.gen_a5:
        raise SystemExit("--gen-a4 and --gen-a5 are mutually exclusive")
    if args.gen_a4 or args.gen_a5:
        if args.pdf:
            raise SystemExit("--pdf cannot be combined with --gen-a4/--gen-a5")
        gen = "a4" if args.gen_a4 else "a5"
        pdf = gen_pdf.generate(md, gen)
    else:
        pdf = C.resolve_pdf(md, args.pdf, args.pdf_variant, args.pdf_dir)
    layout = args.layout or default_layout(gen, args.pdf_variant)

    run = C.Run(md)
    probe.stage(run, pdf, mp3)
    ingest.stage(run, mp3)
    if args.engine == "whispercpp":
        import align_whispercpp

        align_whispercpp.stage(run, run.dir / "02_ingest")
    else:
        align.stage(run, run.dir / "02_ingest")
    verify.stage(run, run.dir / "03_align", run.dir / "02_ingest", pdf)
    rendered = render.stage(
        run,
        run.dir / "03_align",
        run.dir / "02_ingest",
        pdf,
        args.unit,
        layout,
        args.output,
    )
    report.stage(run)
    return {"md": str(md), "run_id": run.run_id, "mp4": rendered["mp4"]}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        usage=(
            "add.py <md file|dir> --media <audio file|dir> "
            "[--pdf f.pdf | --gen-a4 | --gen-a5] [flags]"
        ),
    )
    ap.add_argument("target", help="standard .md file, or a directory of them")
    ap.add_argument(
        "--media",
        default="",
        help="narrated audio: a file, or a directory paired by <stem>({reader}).mp3",
    )
    ap.add_argument("--pdf", default="", help="use this ready-made PDF")
    ap.add_argument(
        "--gen-a4",
        action="store_true",
        help="generate the A4 2-up reader PDF from the .md (no PDF at hand)",
    )
    ap.add_argument(
        "--gen-a5",
        action="store_true",
        help="generate the single-A5-pages PDF from the .md",
    )
    ap.add_argument(
        "--pdf-variant",
        default="",
        choices=list(C.PDF_VARIANTS),
        help="pick a ready-made PDF variant (copys naming: a4|a4-blt|a5)",
    )
    ap.add_argument("--pdf-dir", default="", help="extra directory to search PDFs in")
    ap.add_argument(
        "--output",
        default="",
        help="redirect the mp4: a directory, a new file name, or an absolute path for both",
    )
    ap.add_argument(
        "--layout",
        default="",
        choices=["auto", "linear"],
        help="page->text matching (default: linear for a4/a5, auto otherwise)",
    )
    ap.add_argument(
        "--engine",
        default="whispercpp",
        choices=["whispercpp", "whisperx"],
        help="align engine: whispercpp (whisper.cpp CPU, stable) or whisperx (torch CUDA)",
    )
    ap.add_argument(
        "--unit",
        default="paragraph",
        choices=["paragraph", "line"],
        help="highlight granularity: whole paragraph, or one PDF text line at a time",
    )
    args = ap.parse_args()

    target = C.resolve_input(args.target)
    if target.is_dir():
        if args.media and Path(args.media).is_file():
            raise SystemExit(
                "directory of .md with a single --media file; pass a media directory"
            )
        mds = []
        for f in sorted(target.glob("*.md")):
            if C.parse_md(f.read_text(encoding="utf-8"))["paragraphs"]:
                mds.append(f)
            else:
                print(f"skip {f.name}: no narratable paragraphs (e.g. meta.md)")
        if not mds:
            raise SystemExit(f"no narratable .md in {target}")
        if args.output and Path(args.output).suffix and not Path(args.output).is_dir():
            raise SystemExit(
                "--output <file> with a directory of .md would collide; pass a directory"
            )
        results, failures = [], []
        for md in mds:
            try:
                results.append(process(md, args))
            except SystemExit as e:
                # one bad chapter (e.g. missing audio) must not kill the rest
                failures.append({"md": md.name, "error": str(e) or "?"})
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if failures:
            print(
                json.dumps({"failed": failures}, ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
            raise SystemExit(f"{len(failures)}/{len(mds)} chapters failed")
    else:
        results = [process(target, args)]
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
