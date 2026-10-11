"""Stage 06 (report): aggregate the latest stage manifests into a single report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import common as C

STAGES = [
    ("probe", "01_probe"),
    ("ingest", "02_ingest"),
    ("align", "03_align"),
    ("verify", "04_verify"),
    ("render", "05_render"),
]


def _stage_dir(run: C.Run, name: str) -> Path | None:
    local = run.dir / name
    if local.exists():
        return local
    return C.latest_stage(run.target, name)


def stage(run: C.Run) -> dict:
    out = run.stage("06_report")
    lines = [
        f"# Alignment report — {run.stem}",
        "",
        f"- generated: {run.manifest['created_at']}",
        f"- input: {run.target}",
        "",
        "## Stages",
        "",
    ]
    dirs = {}
    for name, stage_name in STAGES:
        d = _stage_dir(run, stage_name)
        dirs[name] = d
        lines.append(f"- **{name}**: {d.parent.name if d else 'not run'}")

    lines += ["", "## Numbers", ""]
    ingest_d = dirs.get("ingest")
    if ingest_d and (ingest_d / "segments.json").exists():
        seg = json.loads((ingest_d / "segments.json").read_text(encoding="utf-8"))
        lines.append(
            f"- text: {seg['stats']['paragraphs']} paragraphs / {seg['stats']['chapters']} chapters"
        )
    align_d = dirs.get("align")
    if align_d and (align_d / "align_summary.json").exists():
        a = json.loads((align_d / "align_summary.json").read_text(encoding="utf-8"))
        lines.append(
            f"- align: {a['engine']} {a['model']} {a['compute_type']} @ {a['device']}; "
            f"{a['paragraphs_ok']}/{a['paragraphs_total']} timed, {a['paragraphs_gap']} gaps, "
            f"coverage {a['coverage_ms'] // 1000}s"
        )
    render_d = dirs.get("render")
    if render_d and (render_d / "render_summary.json").exists():
        r = json.loads((render_d / "render_summary.json").read_text(encoding="utf-8"))
        lines.append(
            f"- render: {r['frames']} frames, {r['page_size'][0]}x{r['page_size'][1]}, pages {r['pages_used']}"
        )
        lines.append(f"- mp4: {r['mp4']}")
    verify_d = dirs.get("verify")
    if verify_d and (verify_d / "qc_report.md").exists():
        lines += [
            "",
            "## QC",
            "",
            (verify_d / "qc_report.md").read_text(encoding="utf-8"),
        ]

    (out / "report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    summary = {
        "report": str(out / "report.md"),
        "mp4": (
            json.loads((render_d / "render_summary.json").read_text("utf-8"))["mp4"]
            if render_d and (render_d / "render_summary.json").exists()
            else None
        ),
    }
    run.set_stage("report", {"status": "done", "dir": str(out)})
    return summary


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target")


def run(args: argparse.Namespace) -> None:
    run = C.Run(C.require_file(C.resolve_input(args.target), "report"))
    print(json.dumps(stage(run), ensure_ascii=False))
