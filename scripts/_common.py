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
    """Resolve an explicit path (file or directory); no cwd/env search fallbacks."""
    path = Path(target)
    if path.exists():
        return path.resolve()
    raise SystemExit(f"input not found: {target}")


def require_file(target: Path, stage: str) -> Path:
    """Single-file stages reject directories (use add for directories)."""
    if target.is_dir():
        raise SystemExit(f"{stage} takes a single file; `just add` handles directories")
    return target


def stem_of(target: Path) -> str:
    """`laika-come-home.zh-cn.md` -> `laika-come-home.zh-cn`."""
    return target.name.rsplit(".", 1)[0] if "." in target.name else target.name


AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg")
READER_RE = re.compile(r"^(?P<stem>.*?)\{(?P<reader>[^}]+)\}$")


def reader_of(audio_stem: str) -> str | None:
    """`chpt_01{reader-name}` -> `reader-name`; plain names -> None."""
    m = READER_RE.match(audio_stem)
    return m.group("reader") if m else None


def _audio_hits(directory: Path, stem: str) -> list[Path]:
    """`<stem>.mp3` or `<stem>{reader}.mp3` in `directory`."""
    return sorted(
        f
        for f in directory.iterdir()
        if f.is_file()
        and f.suffix.lower() in AUDIO_EXTS
        and re.fullmatch(
            re.escape(stem) + r"(\{[^}]+\})?" + re.escape(f.suffix),
            f.name,
            re.IGNORECASE,
        )
    )


def resolve_media(md: Path, media: str, strict: bool = True) -> Path | None:
    """Pair an .md with its audio. `--media` may be a file or a directory
    (auto-detected). Directory/sibling pairing matches `<stem>.mp3` or
    `<stem>{reader}.mp3`."""
    stem = stem_of(md)
    if media:
        p = Path(media)
        if not p.exists():
            raise SystemExit(f"media not found: {media}")
        if p.is_file():
            if md.is_dir():
                raise SystemExit(
                    "--media file with a directory of .md; pass a media directory"
                )
            return p.resolve()
        hits = _audio_hits(p, stem)
        where = str(p)
    else:
        hits = _audio_hits(md.parent, stem)
        where = "the .md's directory (pass --media <file/dir>)"
    if len(hits) == 1:
        return hits[0].resolve()
    if len(hits) > 1:
        raise SystemExit(f"multiple audios match {stem}: {[h.name for h in hits]}")
    if strict:
        raise SystemExit(
            f"no audio matching '{stem}.mp3' or '{stem}{{reader}}.mp3' in {where}"
        )
    return None


PDF_VARIANTS = {"a4": ".pdf", "a5": ".a5.pdf", "a4-blt": ".booklet.pdf"}


def resolve_pdf(
    anchor: Path, explicit: str = "", variant: str = "", pdf_dir: str = ""
) -> Path:
    """Find the PDF for `anchor`: explicit > variant > PDF_SOURCE / _output/pdfs > sibling.

    Variant names and file suffixes follow the copys pipeline: a4 -> `<stem>.pdf`
    (A4 2-up reader), a5 -> `<stem>.a5.pdf`, a4-blt -> `<stem>.booklet.pdf`."""
    stem = stem_of(anchor)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = anchor.parent / explicit
        if p.exists():
            return p.resolve()
        raise SystemExit(f"pdf not found: {explicit}")

    if variant and variant not in PDF_VARIANTS:
        raise SystemExit(f"unknown --pdf-variant: {variant} ({'|'.join(PDF_VARIANTS)})")

    dirs = [Path(pdf_dir)] if pdf_dir else []
    pdf_source = env_dir("PDF_SOURCE")
    if pdf_source:
        dirs.append(pdf_source)
    dirs.append(anchor.parent / "_output" / "pdfs")
    dirs.append(anchor.parent)

    names = (
        [f"{stem}{PDF_VARIANTS[variant]}"]
        if variant
        else [f"{stem}{suffix}" for suffix in PDF_VARIANTS.values()]
    )
    for d in dirs:
        for name in names:
            if (d / name).exists():
                return (d / name).resolve()
    raise SystemExit(
        f"pdf not found for {stem} (use --pdf / --gen-a4 / --gen-a5 / --pdf-variant)"
    )


def resolve_output(spec: str, default_dir: Path, default_name: str) -> Path:
    """Resolve `--output` against the default mp4 location.

    - existing directory or trailing separator or extension-less path -> a directory
      (the mp4 keeps `default_name` inside it);
    - path with a filename suffix -> that exact file (absolute paths redirect both
      directory and name; bare names redirect only the name, into `default_dir`).
    Missing directories are created.
    """
    if not spec:
        return default_dir / default_name
    p = Path(spec)
    if (p.exists() and p.is_dir()) or spec.endswith(("/", "\\")) or not p.suffix:
        p.mkdir(parents=True, exist_ok=True)
        return (p / default_name).resolve()
    if p.parent == Path("."):
        return default_dir / p.name  # bare name: rename only
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.resolve()


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


def latest_stage(stem: str, stage: str, ready: str = "") -> Path | None:
    """Newest `_output/<stem>_*/<stage>` directory, if any.

    With `ready`, only runs whose manifest has that stage recorded (e.g. an
    in-flight run directory whose stage folder already exists but has not
    finished is skipped)."""
    candidates = sorted(
        OUTPUT.glob(f"{stem}_*/{stage}"), key=lambda p: p.stat().st_mtime
    )
    if not ready:
        return candidates[-1] if candidates else None
    for d in reversed(candidates):
        manifest = d.parent / "manifest.json"
        if manifest.exists():
            try:
                stages = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "stages", {}
                )
            except json.JSONDecodeError:
                continue  # mid-write
            if ready in stages:
                return d
    return None


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
