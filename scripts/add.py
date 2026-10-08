"""Full pipeline: standard .md (+ sibling .mp3) -> aligned mp4 on booklet PDF pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C
import align
import ingest
import probe
import render
import report
import verify


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--pdf", default="")
    ap.add_argument("--pdf-variant", default="")
    ap.add_argument("--pdf-dir", default="")
    ap.add_argument("--layout", default="auto", choices=["auto", "linear"])
    ap.add_argument("--unit", default="paragraph", choices=["paragraph", "chars"])
    ap.add_argument("--chars", type=int, default=20)
    ap.add_argument("--paragraphs", action="store_true")
    ap.add_argument("--words", type=int, default=0)
    args = ap.parse_args()
    if args.paragraphs:
        args.unit = "paragraph"
    if args.words:
        args.unit, args.chars = "chars", args.words

    target = C.resolve_input(args.target)
    pdf = C.resolve_pdf(target, args.pdf, args.pdf_variant, args.pdf_dir)
    run = C.Run(target)

    probe.stage(run, pdf)
    ingest.stage(run, Path(str(target) + ".mp3"))
    align.stage(run, run.dir / "02_ingest")
    verify.stage(run, run.dir / "03_align", run.dir / "02_ingest", pdf)
    rendered = render.stage(
        run,
        run.dir / "03_align",
        run.dir / "02_ingest",
        pdf,
        args.unit,
        args.chars,
        args.layout,
    )
    report.stage(run)
    print(
        json.dumps({"run_id": run.run_id, "mp4": rendered["mp4"]}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
