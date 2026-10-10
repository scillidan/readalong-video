"""Generate a reading PDF (A5 pages, or A4 landscape 2-up reader) from a standard .md.

Used by `add --gen-a4 / --gen-a5` when there is no ready-made PDF yet. Output
naming and location follow the copys convention: `<md_dir>/_output/pdfs/<stem>.pdf`
(a4) or `<stem>.a5.pdf` (a5), so later runs resolve them via --pdf-variant.

Typesetting is ported from the copys pipeline (projs-site/copys, scripts
/gen_a4_latex.{py,tex} + gen_copy.{py,tex}): xelatex + the markdown package
with the shared post template (Sarasa Mono SC, xeCJK punctuation, endnotes).
The A5 sequential pages are typeset once; a5 keeps them, a4 imposes them 2-up
on landscape A4 via pdfpages (the copys "reader"). The .tex templates are
copied verbatim; keep them in sync with copys manually.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MEDIA_EXTS = {".gif", ".mp4", ".mov", ".webm"}
AVIF_EXTS = {".avif"}
WEBP_EXTS = {".webp"}
SVG_EXTS = {".svg"}
CONVERT_EXTS = MEDIA_EXTS | AVIF_EXTS | WEBP_EXTS
# xelatex cannot include SVG directly, so rasterise it alongside other formats.
LATEX_CONVERT_EXTS = CONVERT_EXTS | SVG_EXTS
SUPPORTED_EXTS = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif"]


# --- small helpers (ported from copys scripts/_common.py) ---------------------


def shell_quote(p: str) -> str:
    """Quote a path for use in a shell command template ($1/$2 placeholder)."""
    return '"' + p + '"' if os.name == "nt" else shlex.quote(p)


def safe_staging_dir(parent: Path, stem: str) -> Path:
    """Return ``parent/stem``, shortened with a hash suffix when the stem is
    so long that files inside the directory would exceed Windows' 260-char
    path limit."""
    candidate = parent / stem
    # Budget: dir + "/images/<stem>_01.jpg" must stay well under 260 chars.
    if len(str(candidate)) + len(stem) > 225:
        import hashlib

        digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
        short = f"{stem[:24]}-{digest}"
        print(f"  ⚠ long stem shortened for staging dir: {short}")
        return parent / short
    return candidate


def find_imagemagick_cli() -> str | None:
    """Return the ImageMagick CLI binary name/path, or None if not found."""
    if shutil.which("magick"):
        return "magick"
    if sys.platform != "win32" and shutil.which("convert"):
        return "convert"
    return None


# --- markdown preprocessing (ported from copys scripts/gen_a4_latex.py) --------


def tex_escape(text):
    return (
        text.replace("\\", "\\textbackslash{}")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("$", "\\$")
        .replace("&", "\\&")
        .replace("#", "\\#")
        .replace("^", "\\^{}")
        .replace("_", "\\_")
        .replace("%", "\\%")
        .replace("~", "\\textasciitilde{}")
    )


def render_metadata_line(line):
    r"""Escape one metadata line for the postmetadata environment.

    URLs become \url{...} (clickable, breakable anywhere via xurl) no matter
    which key they follow; everything else is TeX-escaped. Applied after
    html.unescape so entity-encoded characters are handled like the body.
    """
    line = html.unescape(line)
    url_re = re.compile(r"https?://\S+")
    segs = []
    pos = 0
    for m in url_re.finditer(line):
        segs.append(tex_escape(line[pos : m.start()]))
        segs.append(f"\\url{{{m.group(0)}}}")
        pos = m.end()
    segs.append(tex_escape(line[pos:]))
    return "".join(segs)


def extract_metadata(md_content):
    """Parse all leading ``` metadata blocks.

    Each block is replaced with a ```{=tex} raw block holding a `postmetadata`
    environment (rewritten by gen_copy.tex for this pipeline: frameless).
    """
    lines = md_content.splitlines()
    meta = {}
    raw_blocks = []
    pos = 0
    n = len(lines)

    # Skip leading blank lines, then consume every consecutive ``` block.
    while pos < n and lines[pos].strip() == "":
        pos += 1

    while pos < n and lines[pos].strip().startswith("```"):
        end = None
        for i in range(pos + 1, n):
            if lines[i].strip() == "```":
                end = i
                break
        if end is None:
            break

        meta_lines = lines[pos + 1 : end]
        for line in meta_lines:
            m = re.match(r"^([A-Za-z][A-Za-z0-9_ ]*)\s*:\s*(.*)$", line)
            if m:
                key = m.group(1).strip().lower().replace(" ", "")
                meta[key] = m.group(2).strip()

        block_raw = ["```{=tex}", "\\begin{postmetadata}"]
        for line in meta_lines:
            rendered = render_metadata_line(line) or "\\mbox{}"
            # Each entry is its own paragraph so wrapped lines hang-indent.
            block_raw.append(rendered + "\\par")
        block_raw += ["\\end{postmetadata}", "```"]
        raw_blocks.extend(block_raw)

        pos = end + 1
        # Allow blank lines between consecutive metadata blocks.
        while pos < n and lines[pos].strip() == "":
            pos += 1

    md_content = "\n".join(raw_blocks + lines[pos:])
    return meta, md_content


def clean_markdown(md_content):
    # Remove cmarker raw-typst comments.
    md_content = re.sub(r"<!--raw-typst.*?-->", "", md_content, flags=re.DOTALL)

    # Convert broken image lines (alt text without a path) to italic text.
    def fix_broken_image(m):
        alt = m.group(1)
        return f"*{alt}*" if alt else ""

    md_content = re.sub(
        r"^!\[([^\]]*)\]$",
        fix_broken_image,
        md_content,
        flags=re.MULTILINE,
    )

    # Collapse runs of blank lines.
    md_content = re.sub(r"\n{3,}", "\n\n", md_content)
    return md_content


def apply_smart_quotes(md_content):
    r"""Convert straight ASCII double quotes in text to curly quotes.

    Fenced code blocks, inline code spans, and markdown links/images are
    protected so their quotes stay verbatim.
    """
    FENCE_DELIM = "FENCE"
    INLINE_DELIM = "INLINE"
    LINK_DELIM = "LINK"
    END_DELIM = ""

    protected = []

    def protect(pattern, delim):
        def repl(m):
            idx = len(protected)
            protected.append(m.group(0))
            return f"{delim}{idx}{END_DELIM}"

        nonlocal md_content
        md_content = pattern.sub(repl, md_content)

    # 1. Protect fenced code blocks (``` ... ``` or ~~~ ... ~~~).
    fence_re = re.compile(
        r"^(```+|~~~+)[^\n]*\n.*?\n\1[ \t]*$", re.DOTALL | re.MULTILINE
    )
    protect(fence_re, FENCE_DELIM)

    # 2. Protect inline code spans (handling matching backtick runs).
    inline_re = re.compile(r"(`+)(.+?)\1", re.DOTALL)
    protect(inline_re, INLINE_DELIM)

    # 3. Protect markdown links/images so link titles/URLs stay intact.
    link_re = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
    protect(link_re, LINK_DELIM)

    # 4. Convert straight double quotes that are clearly opening/closing.
    md_content = re.sub(
        r"([\s\(\[\{—–\-]|^)\"([^\"\n]*?)\"([\s\.\,\;\:\!\?\)\]\}—–\-]|$)",
        r"\1“\2”\3",
        md_content,
    )

    # 5. Restore protected content in reverse order of protection.
    md_content = re.sub(
        re.escape(LINK_DELIM) + r"(\d+)" + re.escape(END_DELIM),
        lambda m: protected[int(m.group(1))],
        md_content,
    )
    md_content = re.sub(
        re.escape(INLINE_DELIM) + r"(\d+)" + re.escape(END_DELIM),
        lambda m: protected[int(m.group(1))],
        md_content,
    )
    md_content = re.sub(
        re.escape(FENCE_DELIM) + r"(\d+)" + re.escape(END_DELIM),
        lambda m: protected[int(m.group(1))],
        md_content,
    )

    return md_content


def convert_with_magick(src, dest):
    cli = find_imagemagick_cli()
    if not cli:
        print(
            f"  ✗ conversion failed for {src.name}: ImageMagick (magick/convert) not found"
        )
        return False

    src_ext = src.suffix.lower()

    # Animated / multi-frame sources: ensure we emit a single output file.
    # ImageMagick's default behaviour for GIF/video is to write a numbered
    # sequence (foo-0.jpg, foo-1.jpg, ...), so xelatex never finds foo.jpg.
    if src_ext == ".gif":
        # [0] selects the first frame and forces a single output file.
        magick_spec = f"{src}[0]"
    elif src_ext in MEDIA_EXTS:
        # Video formats convert more reliably with ffmpeg on CI images.
        if shutil.which("ffmpeg"):
            r = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(src),
                    "-ss",
                    "00:00:00",
                    "-vframes",
                    "1",
                    str(dest),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if r.returncode == 0 and dest.exists():
                return True
            print(f"  ✗ ffmpeg conversion failed for {src.name}: {r.stderr}")
            return False
        magick_spec = f"{src}[0]"
    else:
        magick_spec = str(src)

    try:
        r = subprocess.run(
            [cli, magick_spec, str(dest)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if r.returncode == 0:
            return True
        # ImageMagick's SVG delegate is sometimes missing on CI images.
        # Fall back to rsvg-convert (PNG) + ImageMagick (PNG -> JPG) when possible.
        if src_ext == ".svg" and shutil.which("rsvg-convert"):
            png = dest.with_suffix(".png")
            r2 = subprocess.run(
                ["rsvg-convert", "--format=png", "-o", str(png), str(src)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if r2.returncode == 0 and png.exists():
                r3 = subprocess.run(
                    [cli, str(png), str(dest)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                png.unlink(missing_ok=True)
                if r3.returncode == 0:
                    return True
                print(f"  ✗ conversion failed for {src.name}: {r3.stderr}")
                return False
            print(f"  ✗ conversion failed for {src.name} (rsvg-convert): {r2.stderr}")
            return False
        print(f"  ✗ conversion failed for {src.name}: {r.stderr}")
        return False
    except Exception as e:  # noqa: BLE001 - conversion can fail in many ways; log and skip
        print(f"  ✗ conversion error for {src.name}: {e}")
        return False


def protect_pipe_tables(md_content):
    """Convert markdown pipe-table rows into bulleted list items.

    The `markdown` package trips over the HTML entities (``&nbsp;`` etc.) that
    frequently appear inside such tables, and long fenced-code rows overflow.
    Emitting each row as a bullet list item keeps the text wrapping and lets
    the package handle the (already unescaped) entities normally.
    """
    lines = md_content.splitlines()
    out = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            for row in rows:
                content = row.strip().strip("|")
                if re.fullmatch(r":?-+:?", content.strip()):
                    continue
                content = " — ".join(cell.strip() for cell in content.split("|"))
                if content:
                    out.append(f"- {content}")
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def unwrap_md_code_blocks(md_content):
    r"""Strip ````md` fenced code blocks and render their contents as markdown.

    Some articles use ````md` fences for styled callout blocks. The markdown
    package would otherwise typeset the inner content as a verbatim code block
    that does not wrap, causing overfull \hbox lines that spill across columns.
    """
    fence_re = re.compile(
        r"^(```+)md[ \t]*\n(.*?)\n\1[ \t]*$", re.DOTALL | re.MULTILINE
    )
    return fence_re.sub(lambda m: m.group(2), md_content)


def escape_underscores_in_urls(md_content):
    """Escape underscores in markdown image/link URLs.

    The markdown package is loaded with ``underscores=true`` so that ``_*_``
    emphasis works. That also makes underscores inside URLs emphasis markers,
    which breaks filenames such as ``image_01.jpg``. Escaping them keeps the
    URL intact while allowing emphasis syntax elsewhere.
    """

    def esc(url):
        return url.replace("_", r"\_")

    # Links that wrap an image: [![alt](inner)](outer)
    def link_with_image_repl(m):
        inner_alt, inner_url, outer_url = m.group(1), m.group(2), m.group(3)
        return f"[![{inner_alt}]({esc(inner_url)})]({esc(outer_url)})"

    md_content = re.sub(
        r"\[!\[([^\]]*)\]\(([^\s\)]+)\)\]\(([^\s\)]+)\)",
        link_with_image_repl,
        md_content,
    )

    # Standalone images and regular links. Skip URLs that already contain an
    # escaped underscore so that nested image/link combinations are not double
    # escaped.
    def repl(m):
        url = m.group(2)
        if r"\_" in url:
            return m.group(0)
        return m.group(1) + esc(url) + m.group(3)

    md_content = re.sub(r"(!?\[[^\]]*\]\()([^\s\)]+)(\))", repl, md_content)
    return md_content


def separate_footnote_definitions(md_content):
    """Ensure consecutive footnote definitions are separated by blank lines.

    The markdown package only recognises footnote definitions that are
    separated by at least one blank line. Many source files list them
    back-to-back, which causes "Undefined note reference" warnings.
    """
    return re.sub(
        r"(\[\^[^\]]+\]:.*)\n(?=\[\^[^\]]+\]:)",
        r"\1\n\n",
        md_content,
    )


def normalize_whitespace(md_content, hard_breaks=True):
    """Clean up stray whitespace.

    With ``hard_breaks=True`` (this pipeline), lines ending in 2+ spaces
    keep exactly two trailing spaces: the standard .md contract uses
    markdown hard line breaks for 换行, while blank lines remain paragraph
    breaks. A hard break at the very end of a paragraph is redundant and
    stripped there.
    """

    def keep_breaks(part):
        lines = part.split("\n")
        out = []
        for idx, line in enumerate(lines):
            m = re.search(r" {2,}$", line)
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if m and nxt.strip():
                core, trail = line[: m.start()], "  "
            else:
                core, trail = re.sub(r"[ \t]+$", "", line), ""
            core = re.sub(r"([^\s])  +", r"\1 ", core)
            out.append(core + trail)
        return "\n".join(out)

    # Preserve fenced code blocks while normalizing the rest.
    fence_re = re.compile(r"^(```+)[^\n]*\n.*?\n\1[ \t]*$", re.DOTALL | re.MULTILINE)
    parts = []
    last = 0
    for m in fence_re.finditer(md_content):
        parts.append(md_content[last : m.start()])
        parts.append(m.group(0))
        last = m.end()
    parts.append(md_content[last:])

    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            if hard_breaks:
                part = keep_breaks(part)
            else:
                part = re.sub(r"[ \t]+$", "", part, flags=re.MULTILINE)
                part = re.sub(r"([^\s])  +", r"\1 ", part)
        out.append(part)
    return "".join(out)


def process_figures(md_content):
    """Normalise image blocks.

    * Image + caption in the same paragraph become an image whose alt text is
      the caption (the template renders it as a narrow caption block).
    * Standalone images get their alt cleared, so the template never prints
      descriptive alt text as a caption.
    """
    img_re = re.compile(r"^!\[[^\]]*\]\(([^\s\)\\]+)\)[ \t]*$")

    blocks = re.split(r"\n{2,}", md_content)
    out = []
    pending = []

    def flush_images():
        if not pending:
            return
        for f in pending:
            out.append(f"![]({f})")
        pending.clear()

    for block in blocks:
        lines = block.splitlines()
        if not lines:
            flush_images()
            continue
        first = lines[0].rstrip()
        m = img_re.match(first)
        if m is None:
            flush_images()
            out.append(block)
            continue
        filename = m.group(1)
        caption = " ".join(ln.strip() for ln in lines[1:] if ln.strip())
        if caption:
            flush_images()
            out.append(f"![{caption}]({filename})")
        else:
            pending.append(filename)
    flush_images()
    return "\n\n".join(out)


def get_image_source(content_dir):
    env_path = os.environ.get("POST_SOURCE")
    if env_path:
        path = Path(os.path.expandvars(env_path))
        if path.exists():
            return path
        print(f"✗ POST_SOURCE: {env_path} (not found)")
    local_assets = content_dir / "assets"
    if local_assets.exists():
        return local_assets
    return None


def process_markdown(md_path, output_dir):
    """Port of copys gen_a4_latex.process_markdown (hard_breaks=True, no dither).

    Writes body.md / meta.tex / images/ into output_dir and returns meta.
    """
    content_dir = md_path.parent
    md_content = md_path.read_text(encoding="utf-8")

    meta, md_content = extract_metadata(md_content)
    md_content = clean_markdown(md_content)

    # Sanitize HTML leftovers so the markdown package + xelatex can handle them.
    md_content = protect_pipe_tables(md_content)
    md_content = re.sub(r"<br\s*/?>", " ", md_content, flags=re.IGNORECASE)
    md_content = html.unescape(md_content)

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = content_dir / "assets"

    cdn_re = re.compile(
        r"(!\[[^\]]*\]\()https://scillidan\.github\.io/cdn_image_post/([^\s\)\\]+)(\))"
    )
    assets_re = re.compile(r"(!\[[^\]]*\]\()assets/([^\s\)\\]+)(\))")
    any_img_re = re.compile(r"(!\[[^\]]*\]\()([^\s\)\\]+)(\))")

    cdn_names = sorted({m.group(2) for m in cdn_re.finditer(md_content)})
    local_map = {}
    local_names = set()

    for m in assets_re.finditer(md_content):
        name = m.group(2)
        local_map[f"assets/{name}"] = name
        local_names.add(name)

    for m in any_img_re.finditer(md_content):
        ref = m.group(2)
        if ref.startswith(("http://", "https://", "assets/")):
            continue
        local_map[ref] = ref
        local_names.add(ref)

    conv_map = {}
    media_note = {}
    found_count = 0
    missing = []

    def record_conversion(full, jpg_name, ext):
        conv_map[full] = jpg_name
        if ext in MEDIA_EXTS:
            media_note[full] = ext.lstrip(".")

    cdn_map = {}
    if cdn_names:
        image_source = get_image_source(content_dir)
        if not image_source:
            sys.exit("\nSet POST_SOURCE in .env or place images in post/assets/\n")
        for base in cdn_names:
            resolved = None
            for ext in SUPPORTED_EXTS:
                src = image_source / f"{base}{ext}"
                if src.exists():
                    resolved = src
                    break
            if not resolved:
                missing.append(f"{base}.*")
                print(f"  ✗ {base}.* (not found)")
                continue

            src_ext = resolved.suffix.lower()
            if src_ext in LATEX_CONVERT_EXTS:
                jpg = images_dir / f"{resolved.stem}.jpg"
                if convert_with_magick(resolved, jpg):
                    cdn_map[base] = jpg.name
                    found_count += 1
                    print(f"  ✓ {jpg.name} (from {resolved.name})")
                else:
                    missing.append(f"{base}.*")
            else:
                dest_name = resolved.name
                dest = images_dir / dest_name
                if not dest.exists():
                    shutil.copy2(resolved, dest)
                cdn_map[base] = dest_name
                found_count += 1
                print(f"  ✓ {dest_name}")

    for name in sorted(local_names):
        src = assets_dir / name
        if not src.exists():
            missing.append(name)
            print(f"  ✗ {name} (not found)")
            continue

        src_ext = src.suffix.lower()
        if src_ext in LATEX_CONVERT_EXTS:
            jpg = images_dir / f"{src.stem}.jpg"
            if convert_with_magick(src, jpg):
                for full, fname in local_map.items():
                    if fname == name:
                        record_conversion(full, jpg.name, src_ext)
                found_count += 1
                print(f"  ✓ {jpg.name} (from {src.name})")
            else:
                missing.append(name)
        else:
            dest = images_dir / name
            if not dest.exists():
                shutil.copy2(src, dest)
            found_count += 1
            print(f"  ✓ {name}")

    if missing:
        print(f"\n✗ Images: {found_count} found, {len(missing)} missing")
        for m in missing:
            print(f"    - {m}")
        sys.exit(1)
    else:
        print(f"✓ Images: all {found_count} found")

    media_img_re = re.compile(r"(!\[[^\]]*\]\()([^\s\)\\]+)(\))([ \t]*\n)([^\n]*)")

    def rewrite_media(m):
        full = m.group(2)
        if full not in conv_map:
            return m.group(0)
        jpg = conv_map[full]
        sep = m.group(4)
        caption = m.group(5)
        if full in media_note and caption and not caption.lstrip().startswith("!["):
            caption = f"{caption} [Original Format: {media_note[full]}]"
        return f"{m.group(1)}{jpg}{m.group(3)}{sep}{caption}"

    md_content = media_img_re.sub(rewrite_media, md_content)

    def rewrite_cdn(m):
        base = m.group(2)
        if base in cdn_map:
            return f"{m.group(1)}{cdn_map[base]}{m.group(3)}"
        return m.group(0)

    md_content = cdn_re.sub(rewrite_cdn, md_content)

    def rewrite_local(m):
        full = m.group(2)
        if full in local_map:
            return f"{m.group(1)}{local_map[full]}{m.group(3)}"
        return m.group(0)

    md_content = any_img_re.sub(rewrite_local, md_content)

    md_content = unwrap_md_code_blocks(md_content)
    md_content = process_figures(md_content)
    md_content = escape_underscores_in_urls(md_content)
    md_content = separate_footnote_definitions(md_content)
    md_content = normalize_whitespace(md_content, hard_breaks=True)
    md_content = re.sub(r"\n{3,}", "\n\n", md_content)
    md_content = apply_smart_quotes(md_content)

    meta_path = output_dir / "meta.tex"
    # Keep the first H1 in the markdown body; the wrapper no longer prints a
    # separate title block.
    if "title" not in meta:
        m = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
        if m:
            meta["title"] = m.group(1).strip()

    body_path = output_dir / "body.md"
    body_path.write_text(md_content, encoding="utf-8")
    print(f"Created: {body_path}")

    meta_lines = ["% Auto-generated post metadata"]
    for key, val in meta.items():
        # Wrapper checks \posttitle, \postauthor, \postdate, etc. (all lowercase).
        cmd = "post" + key.lower()
        meta_lines.append(f"\\def\\{cmd}{{{tex_escape(val)}}}")
    meta_path.write_text("\n".join(meta_lines), encoding="utf-8")
    print(f"Created: {meta_path}")

    return meta


# --- LaTeX wrappers + compile (ported from copys scripts/gen_copy.py) ----------


def compile_tex(tex_path, engine):
    """Compile a LaTeX wrapper; returns the page count from the engine log."""
    r = subprocess.run(
        [
            engine,
            "-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            str(tex_path.name),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(tex_path.parent),
    )
    if r.returncode != 0:
        sys.exit(
            f"✗ {engine} compile error ({tex_path.name}):\n{(r.stderr or r.stdout)[-4000:]}"
        )
    staged_pdf = tex_path.with_suffix(".pdf")
    if not staged_pdf.exists():
        sys.exit(f"✗ {engine} produced no PDF ({tex_path.name})")
    m = re.search(r"Output written on .+ \((\d+) pages?", r.stdout)
    return int(m.group(1)) if m else 0


def generate_seq_wrapper(md_path, latex_dir, project_root, tex_stem):
    """A5 sequential-pages wrapper (copys gen_copy.generate_seq_wrapper)."""
    stem = md_path.stem
    rel_root = os.path.relpath(project_root, latex_dir).replace("\\", "/")
    wrapper = rf"""% !TeX program = xelatex
% Auto-generated LaTeX wrapper for {md_path.name} -- do not edit.
% Regenerate with: python scripts/gen_pdf.py {md_path.as_posix()}

\documentclass{{article}}
\usepackage[a5paper, margin=1.2cm]{{geometry}}

\def\poststem{{{stem}}}
\def\postlayout{{a5}}

\input{{{rel_root}/scripts/gen_a4_latex}}
\input{{{rel_root}/scripts/gen_copy}}

\def\postimagedir{{images/}}

\begin{{document}}

\input{{meta.tex}}

\postmarkdowninput{{body.md}}

\postprintendnotes

\end{{document}}
"""
    wrapper_path = latex_dir / f"{tex_stem}.tex"
    wrapper_path.write_text(wrapper, encoding="utf-8")
    print(f"Created: {wrapper_path}")
    return wrapper_path


def extract_pax(tex_stem, latex_dir):
    # pdfpages drops embedded-page links; pax re-inserts them from <stem>.pax
    if not shutil.which("java"):
        print("  ⚠ java not found; endnote links will not survive imposition")
        return
    r = subprocess.run(
        ["kpsewhich", "pax.sty"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    jar = None
    if r.returncode == 0 and r.stdout.strip():
        # <texmf>/tex/latex/pax/pax.sty -> <texmf>/scripts/pax/pax.jar
        texmf = Path(r.stdout.strip().splitlines()[0]).parents[3]
        candidate = texmf / "scripts" / "pax" / "pax.jar"
        if candidate.exists():
            jar = str(candidate)
    if not jar:
        print("  ⚠ pax.jar not found (install the pax package); links skipped")
        return
    r = subprocess.run(
        ["java", "-jar", jar, f"{tex_stem}.pdf"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=str(latex_dir),
    )
    if r.returncode != 0 or not (latex_dir / f"{tex_stem}.pax").exists():
        print(f"  ⚠ pax extraction failed: {(r.stderr or r.stdout)[-500:]}")


def _bp(value: float) -> str:
    """Format a bp length with a reasonable number of decimals."""
    return f"{value:.2f}bp" if value != int(value) else f"{int(value)}bp"


def normalize_paper(paper: str) -> str:
    """Convert a user-friendly paper size to a geometry package option."""
    p = paper.strip().lower()
    if "paper" not in p:
        p += "paper"
    return p


def generate_reader_wrapper(tex_stem, latex_dir, source_pdf, paper="a4"):
    """Generate the pdfpages 2-up reader wrapper (copys reader branch)."""
    opts = "pages=-, nup=2x1"
    wrapper = f"""% !TeX program = pdflatex
% Auto-generated reader imposition wrapper -- do not edit.

\\documentclass{{article}}
\\usepackage[{normalize_paper(paper)}, landscape, margin=0pt]{{geometry}}
\\usepackage{{pdfpages}}
\\usepackage{{pdftexcmds}}
\\makeatletter
\\ifx\\pdfstrcmp\\undefined\\let\\pdfstrcmp\\pdf@strcmp\\fi
\\makeatother
\\usepackage{{pax}}

\\begin{{document}}

\\includepdf[{opts}]{{{source_pdf.name}}}

\\end{{document}}
"""
    wrapper_path = latex_dir / f"{tex_stem}.reader.tex"
    wrapper_path.write_text(wrapper, encoding="utf-8")
    print(f"Created: {wrapper_path}")
    return wrapper_path


# --- pipeline ------------------------------------------------------------------


def tex_stem_of(md: Path) -> str:
    """Sanitize the jobname: spaces and '%' confuse the TeX command line,
    and dots break the markdown package's texlua bridge (copys gen_copy.py)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", md.stem).strip("-")


def generate(md: Path, variant: str) -> Path:
    """Typeset `md` and write the PDF next to copys-style outputs; returns the path."""
    if variant not in ("a4", "a5"):
        raise SystemExit(f"unknown variant: {variant} (a4|a5)")
    if not shutil.which("xelatex"):
        raise SystemExit("xelatex not found (MiKTeX/TeX Live required)")

    project_root = Path(__file__).resolve().parent.parent
    pdfs_dir = md.parent / "_output" / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    latex_dir = safe_staging_dir(md.parent / "_output" / "latex", tex_stem_of(md))
    latex_dir.mkdir(parents=True, exist_ok=True)

    process_markdown(md, latex_dir)
    seq_path = generate_seq_wrapper(md, latex_dir, project_root, tex_stem_of(md))
    npages = compile_tex(seq_path, "xelatex")
    print(f"Sequential A5 document: {npages} pages")

    if variant == "a5":
        out = pdfs_dir / f"{C.stem_of(md)}.a5.pdf"
        shutil.copy2(str(latex_dir / f"{tex_stem_of(md)}.pdf"), str(out))
    else:
        extract_pax(tex_stem_of(md), latex_dir)
        reader_path = generate_reader_wrapper(
            tex_stem_of(md), latex_dir, latex_dir / f"{tex_stem_of(md)}.pdf"
        )
        compile_tex(reader_path, "pdflatex")
        out = pdfs_dir / f"{C.stem_of(md)}.pdf"
        shutil.move(str(reader_path.with_suffix(".pdf")), str(out))

    print(f"Created: {out}")
    print(
        json.dumps(
            {"md": str(md), "variant": variant, "pdf": str(out)}, ensure_ascii=False
        )
    )
    return out.resolve()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="standard .md file, or directory of them")
    ap.add_argument(
        "--a4", action="store_true", help="A4 landscape 2-up reader (default)"
    )
    ap.add_argument("--a5", action="store_true", help="single A5 pages")
    ap.add_argument("--out", default="", help="output path (single-file input only)")
    args = ap.parse_args()
    variant = "a5" if args.a5 else "a4"
    if args.a4 and args.a5:
        raise SystemExit("--a4 and --a5 are mutually exclusive")

    target = C.resolve_input(args.target)
    if target.is_dir():
        if args.out:
            raise SystemExit("--out requires a single .md input")
        for md in sorted(target.glob("*.md")):
            if C.parse_md(md.read_text(encoding="utf-8"))["paragraphs"]:
                generate(md, variant)
            else:
                print(f"skip {md.name}: no narratable paragraphs")
    else:
        pdf = generate(target, variant)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), args.out)


if __name__ == "__main__":
    main()
