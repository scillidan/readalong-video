"""Shared helpers: input resolution, run directories, manifest, markdown parsing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "_output"

FENCE_RE = re.compile(r"^\s*```")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
INLINE_MARKER_RE = re.compile(r"\[\^(\d+)\]")


def env_dir(name: str) -> Path | None:
    # just's dotenv-load passes ${VAR} through literally; expand here.
    value = os.path.expandvars(os.environ.get(name, "")).strip()
    return Path(value) if value else None


def resolve_input(target: str) -> Path:
    """Resolve a bare name against cwd, MD_SOURCE, then TXT_SOURCE."""
    path = Path(target)
    if path.exists():
        return path.resolve()
    for var in ("MD_SOURCE", "TXT_SOURCE"):
        base = env_dir(var)
        if base and (base / target).exists():
            return (base / target).resolve()
    raise SystemExit(f"input not found: {target} (checked cwd, MD_SOURCE, TXT_SOURCE)")


def stem_of(target: Path) -> str:
    """`laika-come-home.zh-cn.md` -> `laika-come-home.zh-cn`."""
    return target.name.rsplit(".", 1)[0] if "." in target.name else target.name


def resolve_pdf(
    anchor: Path, explicit: str = "", variant: str = "", pdf_dir: str = ""
) -> Path:
    """Find the booklet PDF for `anchor`: explicit > variant > PDF_SOURCE > sibling."""
    stem = stem_of(anchor)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = anchor.parent / explicit
        if p.exists():
            return p.resolve()
        raise SystemExit(f"pdf not found: {explicit}")

    suffixes = {"screen": ".pdf", "a5": ".a5.pdf", "booklet": ".booklet.pdf"}
    if variant and variant not in suffixes:
        raise SystemExit(f"unknown --pdf-variant: {variant} (screen|a5|booklet)")

    dirs = [Path(pdf_dir)] if pdf_dir else []
    pdf_source = env_dir("PDF_SOURCE")
    if pdf_source:
        dirs.append(pdf_source)
    dirs.append(anchor.parent)

    names = (
        [f"{stem}{suffixes[variant]}"]
        if variant
        else [
            f"{stem}.pdf",
            f"{stem}.a5.pdf",
            f"{stem}.booklet.pdf",
        ]
    )
    for d in dirs:
        for name in names:
            if (d / name).exists():
                return (d / name).resolve()
    raise SystemExit(
        f"pdf not found for {stem} (use --pdf / --pdf-variant / --pdf-dir)"
    )


def sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- markdown contract -------------------------------------------------------


def normalize(text: str) -> str:
    """Match-oriented normalization: NFKC, quotes/dashes/ellipsis unified, no spaces, lowercased."""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in (
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2018", '"'),
        ("\u2019", '"'),
        ("\u2014", "-"),
        ("\u2013", "-"),
        ("\u2010", "-"),
        ("\u2011", "-"),
    ):
        text = text.replace(src, dst)
    text = text.replace("\u2026", "...")
    return re.sub(r"\s+", "", text).lower()


def parse_md(text: str) -> dict:
    """Parse the standard .md contract into chapters / paragraphs / ignored ranges.

    Ignored (not narrated): leading metadata fenced blocks, footnote definitions.
    Paragraph display keeps inline `[^n]` markers; normalized strips them.
    """
    lines = text.split("\n")
    total = len(lines)
    i = 0
    ignored: list[dict] = []

    while i < total:  # metadata fences at file head
        if FENCE_RE.match(lines[i]):
            start = i
            i += 1
            while i < total and not FENCE_RE.match(lines[i]):
                i += 1
            end = i if i < total else total - 1
            if i < total:
                i += 1
            ignored.append(
                {"kind": "metadata_fence", "start_line": start + 1, "end_line": end + 1}
            )
        elif lines[i].strip() == "":
            i += 1
        else:
            break

    chapters, paragraphs, footnote_defs = [], [], []
    chapter_idx = -1
    while i < total:
        line = lines[i].rstrip("\n")
        i += 1
        if line.strip() == "":
            continue
        heading = HEADING_RE.match(line.strip())
        if heading:
            chapter_idx += 1
            chapters.append(
                {
                    "idx": chapter_idx,
                    "title": heading.group(2).strip(),
                    "first_para": len(paragraphs),
                }
            )
            continue
        fn_def = FOOTNOTE_DEF_RE.match(line.strip())
        if fn_def:
            footnote_defs.append(
                {"n": int(fn_def.group(1)), "line": i, "text": fn_def.group(2).strip()}
            )
            ignored.append({"kind": "footnote_def", "start_line": i, "end_line": i})
            continue
        markers = [
            {"marker": m.group(0), "col": m.start()}
            for m in INLINE_MARKER_RE.finditer(line)
        ]
        rinsed = INLINE_MARKER_RE.sub("", line)
        paragraphs.append(
            {
                "idx": len(paragraphs),
                "chapter": chapter_idx,
                "src_line": i,
                "display": line.strip(),
                "rinsed": rinsed.strip(),
                "normalized": normalize(rinsed),
                "inline_markers": markers,
            }
        )

    if not chapters:
        chapters = [{"idx": 0, "title": None, "first_para": 0}]

    return {
        "chapters": chapters,
        "paragraphs": paragraphs,
        "ignored_ranges": ignored,
        "footnote_defs": footnote_defs,
        "stats": {
            "paragraphs": len(paragraphs),
            "chapters": len(chapters),
            "inline_markers": sum(len(p["inline_markers"]) for p in paragraphs),
            "footnote_defs": len(footnote_defs),
            "chars_display": sum(len(p["display"]) for p in paragraphs),
            "chars_normalized": sum(len(p["normalized"]) for p in paragraphs),
        },
    }


# --- run context -------------------------------------------------------------


def latest_stage(stem: str, stage: str) -> Path | None:
    """Newest `_output/<stem>_*/<stage>` directory, if any."""
    candidates = sorted(
        OUTPUT.glob(f"{stem}_*/{stage}"), key=lambda p: p.stat().st_mtime
    )
    return candidates[-1] if candidates else None


@dataclass
class Run:
    target: Path
    run_id: str = ""
    manifest: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stem = stem_of(self.target)
        self.run_id = self.run_id or f"{self.stem}_{time.strftime('%Y%m%d-%H%M%S')}"
        self.dir = OUTPUT / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            self.manifest = {
                "run_id": self.run_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "input": {"path": str(self.target), "sha256": sha256(self.target)},
                "stages": {},
                "models": [],
                "licenses": [],
                "edits": [],
            }

    def stage(self, name: str) -> Path:
        path = self.dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def patch(self, **sections) -> None:
        for key, value in sections.items():
            self.manifest[key] = value
        self.save()

    def set_stage(self, name: str, data: dict) -> None:
        self.manifest.setdefault("stages", {})[name] = data
        self.save()

    def save(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )


# --- process helpers ---------------------------------------------------------


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def ffprobe_duration_ms(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return round(float(out) * 1000)


def audio_to_16k_mono(src: Path, dst: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
