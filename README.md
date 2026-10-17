# readalong-video

Narrated audio (mp3) + standard `.md` text → forced alignment → highlight on PDF
pages with page turns → mp4.

- **Does**: text preprocessing (`bleach`), probe/ingest, forced alignment (`alignw`/`align`),
  QC (`verify`), render (`render`), report.
- **Does not**: transcribe audio or synthesize speech. Audio is produced externally
  (e.g. VoiceStudio) from the rinsed text.

Authors: Kimi-K2.7-Code🧙‍♂️, scillidan🤡

## Requirements

- Windows Git Bash / MSYS (bash) — the pipeline shells out to `ffmpeg`/`ffprobe`.
- `ffmpeg` + `ffprobe` on `PATH`.
- `uv`.
- `xelatex` + `pdflatex` (MiKTeX/TeX Live) — only for `gen-a4`/`gen-a5`
  (PDF typesetting via the copys LaTeX templates).
- **No GPU required.** The default align engine is **whisper.cpp on CPU** (ggml),
  stable on this host. `--engine whisperx` (torch + faster-whisper CUDA) is a legacy
  option that hard-hangs this machine — do not use it here.

## Install

```sh
uv tool install .            # global `readalong` command (this repo)
readalong prefetch           # whisper.cpp assets check (+ whisperX weights if the
                             # whisperx extra is installed; prints manual steps on failure)
```

Upgrade after pulling changes: `uv tool install . --force`. Without installing:
`uv run python -m readalong_video ...` from the repo also works.

The torch/whisperX stack is an optional extra (`uv tool install '.[whisperx]'`) and
is **not** needed for the default whisper.cpp engine.

## Usage

```sh
# I already have a PDF:
readalong add <name.md> --media <audio.mp3> --pdf <file.pdf>

# I only have the .md (generate the PDF first, then run):
readalong add <name.md> --media <audio.mp3> --gen-a5
readalong add <name.md> --media <audio.mp3> --gen-a4 --unit line

# a directory of .md (one per chapter) against a directory of mp3:
readalong add <md_dir> --media <audio_dir> --gen-a5

# redirect the final mp4 (--output):
readalong add <name.md> --media <audio.mp3> --pdf <file.pdf> --output <dir>          # directory only
readalong add <name.md> --media <audio.mp3> --pdf <file.pdf> --output renamed.mp4    # file name only
readalong add <name.md> --media <audio.mp3> --pdf <file.pdf> --output D:/vids/x.mp4  # both
```

- `<name.md>` / `<audio.mp3>` are explicit paths; run from any directory.
- `--with-headings`: the narration reads `##` chapter titles aloud — headings
  become anchor paragraphs, so titles align, get highlighted, and page turns
  land on the chapter's first page. Off by default (headings are not narrated).
- `--dwell-s` (default 2, `0` disables): the video opens on page 1 and
  lingers on each unread leading page (copyright / colophon / contents — A5
  columns on 2-up sheets count individually) before the narration starts;
  the audio is delayed by the same total so the reading still starts on the
  first body page.
- `--mid-dwell-s` (default 2, `0` disables): same pass-through for unread
  pages met later or at the end (chapter-end / book-end endnote pages).
  Silence is spliced into the audio at the next paragraph boundary, so the
  pause always lands between paragraphs; trailing pages run after the
  narration ends. Column side is respected: `[endnotes | next chapter]`
  dwells before the chapter, `[chapter end | endnotes]` after it.
- `--skip-pages 1-3,7,auto`: sheets never shown at all. `auto` derives the
  front matter from the PDF outline (the copys `--outline`/`--toc` product):
  every page before the first outline target plus pages whose outline title
  looks like contents/copyright. Use it for 目录 pages that dwell would
  otherwise show; explicit ranges work on any PDF, with or without outline.
- `--turn-dwell-s` (default 2, `0` disables): after a page's last paragraph
  the cleared page lingers this long before the page turns; the next page
  then sits unlit until its audio starts (no more stale highlight frozen
  until the next audio pops).
- `--end-dwell-s` (default 2, `0` disables): the final page lingers this
  long after the narration ends instead of stopping dead.
- Audiobook audio with 开场白/结束语 (unmatched lead/tail): the unmatched
  stretches simply play with no highlight. Trim such audio yourself before
  running the pipeline (the pipeline never modifies media files).
- Every output frame is a keyframe (`-g 1`) so progress-bar seeking lands
  exactly; mp4 is muxed `+faststart`.
- `--media` auto-detects a file or a directory. Directory pairing matches
  `<stem>.mp3` or `<stem>{reader}.mp3`; without `--media` the sibling `<stem>.mp3`
  is used.
- **Reader tag**: if the audio is named `<stem>{reader}.mp3` (e.g.
  `chpt_01{森海飞霞}.mp3`), the reader is carried into the output mp4 name:
  `<stem>{reader}.mp4`.
- A directory of `.md` is processed per file (files without narratable paragraphs,
  e.g. `meta.md`, are skipped); pass a media directory, not a single file.
- **Paths with `'` (e.g. `alice's-adventures...`)**: supported — the ffmpeg
  concat list uses frame paths relative to the run dir, so apostrophes never
  reach ffmpeg's concat parser.

### Individual stages

```sh
readalong probe   <target>     # stages 01: md segments / audio / PDF
readalong ingest  <target>     # stages 02: rinsed text + 16k mono wav (--media <file/dir>)
readalong alignw  <target>     # stages 03: whisper.cpp forced alignment (default engine)
readalong align   <target>     # stages 03: whisperX forced alignment (legacy engine)
readalong verify  <target>     # stages 04: timeline QC + page mapping
readalong render  <target>     # stages 05: highlights + turns -> mp4
readalong report  <target>     # stages 06: aggregate report
readalong prefetch             # model weights
readalong bleach  <src.txt>    # Gutenberg .txt -> standard .md
readalong gen-a4  <target.md>  # standard .md -> A4 landscape 2-up reader PDF
readalong gen-a5  <target.md>  # standard .md -> single A5 pages PDF
```

Single-stage commands resume the latest run of that `.md` (matched by stem under
the runs root, newest manifest-wins).

### PDFs

The video shows whole PDF pages; page turns follow the narration. Three sources,
checked in this order:

1. `--pdf <file>` — an explicit ready-made PDF.
2. `--gen-a4` / `--gen-a5` — typeset the `.md` on the fly with the copys
   pipeline (xelatex + the bundled `gen_a4_latex.tex`/`gen_copy.tex`, Sarasa Mono SC;
   a4 = pdfpages 2-up of the A5 pages) into
   `<md_dir>/_output/pdfs/<stem>.pdf` / `<stem>.a5.pdf` (copys-style location).
3. `--pdf-variant` — pick a ready-made variant by name (copys naming):
   - `a4` → `<stem>.pdf` (A4 landscape 2-up reader)
   - `a5` → `<stem>.a5.pdf` (single A5 pages)
   - `a4-blt` → `<stem>.booklet.pdf` (saddle-stitch imposition; pages revisit)

   Searched in `--pdf-dir`, then `PDF_SOURCE` (if set), then the md's own
   `_output/pdfs/`, then the md's directory.

Highlighting is layout-agnostic (text blocks are matched to the reference text).
`--layout linear` assumes PDF page order equals reading order; `auto` (the default
for booklets and unknown PDFs) recovers reading order from content. `--gen-a4` /
`--gen-a5` / `--pdf-variant a4|a5` default to `linear`.

**Outline**: reader PDFs from the copys pipeline can carry a nested PDF outline
(`--outline` / `--toc`; probe reports the entry count). readalong uses it for
`--skip-pages auto` (front-matter detection) — no outline means no auto skip,
but explicit `--skip-pages` ranges always work. Illustrated books (whole-sheet
or column artwork) need no extra handling: image-only sheets carry no matched
text, so they are simply unhighlighted, and dwell logic passes through them.

### Highlight unit

- `--unit paragraph` (default): one highlight per paragraph.
- `--unit line`: one highlight per typeset PDF text line — the highlight follows
  the narration line by line, always covering whole lines. (For
  LaTeX-typeset PDFs each matched text block is a single line; lines shorter
  than the 8-character match threshold, e.g. a tiny dialogue line, are not
  matched and simply stay unhighlighted.)

## Output

Everything lands in `<md_dir>/_output/runs/<run_id>/` with per-stage subdirs
(`01_probe` … `06_report`). The mp4 is
`_output/runs/<run_id>/05_render/<stem>{reader}.mp4`; the aggregated report is
`_output/runs/<run_id>/06_report/report.md`. To centralize runs elsewhere, set
`READALONG_RUNS=<dir>` (env or `.env`).

## Config

`.env` files are loaded by `readalong` itself (no `just` needed), earliest wins,
real environment variables beat files:

1. `~/.config/readalong-video/.env` — user-wide (e.g. `PDF_SOURCE`, `READALONG_RUNS`)
2. `.env` found walking up from the current directory — per-project overrides

See `.env.example`.

## Input contract (standard .md)

- Leading metadata fenced code block(s) — **not narrated**.
- `##` headings split chapters (optional for short pieces). With
  `--with-headings` they are also narrated (anchored and highlighted);
  otherwise they are typeset but not read.
- One paragraph per line, two trailing spaces; blank lines between paragraphs.
- Inline footnote markers `[^n]`; definitions `[^n]: …` collected at the end —
  **not narrated** (rendered as endnotes by `gen-a4`/`gen-a5`; endnote pages
  carry no narration and are dropped by render, like the copyright /
  contents front matter — see `--dwell-s` for showing them pre-roll).
- Long works: one `.md` per chapter in a directory; process the directory.
