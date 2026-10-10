# readalong-video

Narrated audio (mp3) + standard `.md` text → forced alignment → highlight on PDF
pages with page turns → mp4.

- **Does**: text preprocessing (`bleach`), probe/ingest, forced alignment (`align`),
  QC (`verify`), render (`render`), report.
- **Does not**: transcribe audio or synthesize speech. Audio is produced externally
  (e.g. VoiceStudio) from the rinsed text.

Authors: Kimi-K2.7-Code🧙‍♂️, scillidan🤡

## Requirements

- Windows Git Bash / MSYS (bash) — the pipeline shells out to `ffmpeg`/`ffprobe`.
- `ffmpeg` + `ffprobe` on `PATH`.
- `just` and `uv`.
- `xelatex` + `pdflatex` (MiKTeX/TeX Live) — only for `--gen-a4`/`--gen-a5`
  (PDF typesetting via the copys LaTeX templates).
- **No GPU required.** The default align engine is **whisper.cpp on CPU** (ggml),
  stable on this host. `--engine whisperx` (torch + faster-whisper CUDA) is a legacy
  option that hard-hangs this machine — do not use it here.

## Initialize

```sh
cp .env.example .env      # optional: PDF_SOURCE (extra PDF search dir)
uv sync
just prefetch             # whisper.cpp assets (+ whisperX weights; prints manual steps on failure)
```

## Usage

```sh
# I already have a PDF:
just add <name.md> --media <audio.mp3> --pdf <file.pdf>

# I only have the .md (generate the PDF first, then run):
just add <name.md> --media <audio.mp3> --gen-a5
just add <name.md> --media <audio.mp3> --gen-a4 --chars 20

# a directory of .md (one per chapter) against a directory of mp3:
just add <md_dir> --media <audio_dir> --gen-a5

# redirect the final mp4 (--output):
just add <name.md> --media <audio.mp3> --pdf <file.pdf> --output <dir>          # directory only
just add <name.md> --media <audio.mp3> --pdf <file.pdf> --output renamed.mp4    # file name only
just add <name.md> --media <audio.mp3> --pdf <file.pdf> --output D:/vids/x.mp4  # both
```

- `<name.md>` / `<audio.mp3>` are explicit paths (no cwd/library search).
- `--media` auto-detects a file or a directory. Directory pairing matches
  `<stem>.mp3` or `<stem>{reader}.mp3`; without `--media` the sibling `<stem>.mp3`
  is used.
- **Reader tag**: if the audio is named `<stem>{reader}.mp3` (e.g.
  `chpt_01{森海飞霞}.mp3`), the reader is carried into the output mp4 name:
  `<stem>{reader}.mp4`.
- A directory of `.md` is processed per file (files without narratable paragraphs,
  e.g. `meta.md`, are skipped); pass a media directory, not a single file.
- `--output` redirects only the final mp4 (stage outputs stay in the run dir):
  an existing directory, a trailing separator, or an extension-less path is taken
  as a directory (the mp4 keeps its default name); a path with a file suffix
  redirects the file name — bare names stay in the default render dir, relative
  paths resolve from the cwd, absolute paths redirect both directory and name.
  With a directory of `.md`, pass a directory, not a file name.

> **Paths with `'` (e.g. `alice's-adventures...`)**: `just` 1.58 on Windows
> misparses apostrophes in recipe arguments, so `just add ...` breaks. Run the
> script directly instead:
> `uv run scripts/add.py "<md>" --media "<audio file|dir>" [--gen-a5|--pdf ...]`

### Individual stages

```sh
just probe   <target>     # stages 01: md segments / audio / PDF
just ingest  <target>     # stages 02: rinsed text + 16k mono wav (--media <file/dir>)
just alignw  <target>     # stages 03: whisper.cpp forced alignment (default engine)
just align   <target>     # stages 03: whisperX forced alignment (legacy engine)
just verify  <target>     # stages 04: timeline QC + page mapping
just render  <target>     # stages 05: highlights + turns -> mp4
just report  <target>     # stages 06: aggregate report
just prefetch             # model weights
just bleach  <src.txt>    # Gutenberg .txt -> standard .md
just gen-a4  <target.md>  # standard .md -> A4 landscape 2-up reader PDF
just gen-a5  <target.md>  # standard .md -> single A5 pages PDF
```

### PDFs

The video shows whole PDF pages; page turns follow the narration. Three sources,
checked in this order:

1. `--pdf <file>` — an explicit ready-made PDF.
2. `--gen-a4` / `--gen-a5` — typeset the `.md` on the fly with the copys
   pipeline (xelatex + `scripts/gen_a4_latex.tex`/`gen_copy.tex`, Sarasa Mono SC;
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

### Highlight unit

- `--unit paragraph` (default): one highlight per paragraph.
- `--chars N` (alias `--words N`): implies `--unit chars`; split each paragraph
  every ~N characters at the nearest punctuation. Sentence times are anchored to
  word-level timestamps, so `--chars 20` tracks speech more tightly than
  paragraph mode.

## Output

Everything lands in `_output/<run_id>/` with per-stage subdirs (`01_probe` …
`06_report`). The mp4 is `_output/<run_id>/05_render/<stem>{reader}.mp4`; the
aggregated report is `_output/<run_id>/06_report/report.md`.

## Input contract (standard .md)

- Leading metadata fenced code block(s) — **not narrated**.
- `##` headings split chapters (optional for short pieces).
- One paragraph per line, two trailing spaces; blank lines between paragraphs.
- Inline footnote markers `[^n]`; definitions `[^n]: …` collected at the end —
  **not narrated** (rendered as endnotes by `gen-a4`/`gen-a5`).
- Long works: one `.md` per chapter in a directory; process the directory.
