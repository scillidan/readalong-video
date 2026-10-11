"""Convert a Project Gutenberg .txt into the standard .md contract."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import common as C

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
START_RE = re.compile(r"\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG", re.I)
END_RE = re.compile(r"\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG", re.I)
# Chinese chapter headings and illustration markers are also recognized.
CHAPTER_RE = re.compile(
    r"^(?:chapter\s+[\divxlcdm]+|section\s+[\divxlcdm]+|part\s+[\divxlcdm]+|"
    r"第[一二三四五六七八九十百千0-9]+[章节回卷部]|卷[一二三四五六七八九十0-9]+)$",
    re.I,
)
FN_HEAD_RE = re.compile(r"^\s*footnotes?:?\s*$", re.I)
FN_DEF_RE = re.compile(r"^\s*\[(\d+)\]\s*(.*)$")
ILLUS_RE = re.compile(r"^\s*\[(Illustration|插图)[:：]", re.I)
TRANSCRIBER_RE = re.compile(r"\[(?:transcriber|translator)[^\]]*note", re.I)
FN_INLINE_RE = re.compile(r"\[(\d{1,3})\]")
FN_MARK_RE = re.compile(r"\[\^(\d{1,3})\]")
META_RE = re.compile(
    r"^(Title|Author|Release date|Language|Original publication)\s*[:：]\s*(.+)$", re.I
)


def _join(lines: list[str]) -> str:
    if any(CJK_RE.search(l) for l in lines):
        joined = "".join(l.strip() for l in lines)
    else:
        parts: list[str] = []
        for line in lines:
            s = line.strip()
            if parts and parts[-1].endswith("-") and s[:1].islower():
                parts[-1] = parts[-1][:-1] + s  # repair a hyphenated line break
            else:
                parts.append(s)
        joined = " ".join(parts)
    joined = re.sub(r"_([^_\n]+)_", r"*\1*", joined)
    return FN_INLINE_RE.sub(lambda m: f"[^{m.group(1)}]", joined)


def parse_gutenberg(text: str) -> dict:
    start = START_RE.search(text)
    end = END_RE.search(text)
    if not start:
        raise SystemExit("no Gutenberg START marker found")
    head = text[: start.start()]
    line_end = text.find("\n", start.end())
    body = text[line_end + 1 : end.start() if end else len(text)]

    meta = {}
    for line in head.splitlines():
        m = META_RE.match(line.strip())
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()

    decor = re.compile(r"^\s*[\s*·.]{3,}[\s*·.]*$")
    body_lines = body.splitlines()
    while body_lines and (
        decor.match(body_lines[0].strip()) or body_lines[0].strip() == ""
    ):
        body_lines.pop(0)

    stripped = {"license_blocks": 2 if end else 1, "transcriber_notes": 0}
    lines, skipping = [], False
    for line in body_lines:
        if TRANSCRIBER_RE.search(line):
            stripped["transcriber_notes"] += 1
            skipping = "]" not in line
            continue
        if skipping:
            if "]" in line:
                skipping = False
            continue
        lines.append(line.rstrip())

    blocks, cur = [], []
    for line in lines:
        if line.strip() == "":
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    items: list[tuple[str, str]] = []
    footnotes: dict[int, str] = {}
    in_footnotes, moved = False, 0
    for block in blocks:
        first = block.splitlines()[0].strip()
        if FN_HEAD_RE.match(first):
            in_footnotes = True
            continue
        if in_footnotes:
            m = FN_DEF_RE.match(first)
            if m:
                whole = " ".join(l.strip() for l in block.splitlines())
                footnotes[int(m.group(1))] = whole[whole.index("]") + 1 :].strip()
                continue
            in_footnotes = False
        if len(block.splitlines()) == 1 and CHAPTER_RE.match(first):
            items.append(("chapter", first))
            continue
        if ILLUS_RE.match(first) and len(block.splitlines()) == 1:
            block = re.sub(r"^\s*\[[^\]]*[:：]\s*", "", block).rstrip(" ]")
        para = _join(block.splitlines())
        m = FN_DEF_RE.match(para)
        if m and not FN_MARK_RE.search(para):
            footnotes[int(m.group(1))] = m.group(2).strip()
            moved += 1
            continue
        items.append(("para", para))

    return {
        "meta": meta,
        "items": items,
        "footnotes": footnotes,
        "stripped": stripped,
        "moved_fn_blocks": moved,
    }


def render(parsed: dict) -> tuple[str, dict]:
    out = ["```"]
    for key in ("title", "author", "release date", "language", "original publication"):
        if key in parsed["meta"]:
            out.append(f"{key.capitalize()}: {parsed['meta'][key]}")
    if len(out) == 1:
        out.append("Title: (TODO)")
    out += ["```", ""]

    chapters = paragraphs = 0
    for kind, content in parsed["items"]:
        if kind == "chapter":
            out += [f"## {content}", ""]
            chapters += 1
        else:
            out += [f"{content}  ", ""]
            paragraphs += 1
    text = "\n".join(out).rstrip("\n") + "\n"
    if parsed["footnotes"]:
        text += "\n"
        for n in sorted(parsed["footnotes"]):
            text += f"[^{n}]: {parsed['footnotes'][n]}\n"
    summary = {
        "paragraphs": paragraphs,
        "chapters": chapters,
        "footnotes": len(parsed["footnotes"]),
        "stripped": parsed["stripped"],
        "moved_fn_blocks": parsed["moved_fn_blocks"],
    }
    return text, summary


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target")
    ap.add_argument("--style", default="gutenberg")
    ap.add_argument("--out", default="")
    ap.add_argument("--force", action="store_true")


def run(args: argparse.Namespace) -> None:
    src = C.require_file(C.resolve_input(args.target), "bleach")
    if args.style != "gutenberg":
        raise SystemExit(f"unsupported style: {args.style}")
    if not START_RE.search(src.read_text(encoding="utf-8", errors="replace")):
        raise SystemExit("not a Gutenberg text (no START marker)")

    text, summary = render(
        parse_gutenberg(src.read_text(encoding="utf-8", errors="replace"))
    )
    dst = Path(args.out) if args.out else src.with_suffix(".md")
    if dst.exists() and not args.force:
        raise SystemExit(f"output exists (use --force): {dst}")
    dst.write_text(text, encoding="utf-8", newline="\n")
    summary["out"] = str(dst)
    print(json.dumps(summary, ensure_ascii=False))
