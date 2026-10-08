# readalong-video

Narrated audio (mp3) + standard `.md` text → forced alignment → highlight on the original
booklet PDF pages with page turns → mp4.

- **Does**: text preprocessing (`bleach`), probe/ingest, forced alignment (`align`), QC (`verify`),
  render (`render`), report.
- **Does not**: transcribe audio or synthesize speech. Audio is produced externally (e.g. VoiceStudio)
  from the rinsed text.

Authors: Kimi-K2.7-Code🧙‍♂️, DeepSeek-V4.1-Flash🧙‍♂️, scillidan🤡

## Requirements

- Windows Git Bash / MSYS (bash) — the pipeline shells out to `ffmpeg`/`ffprobe`.
- `ffmpeg` + `ffprobe` on `PATH`.
- `just` and `uv`.
- NVIDIA GPU (optional but recommended): `large-v3` fp16 needs ~8 GB; otherwise it degrades to CPU/int8.

## Initialize

```sh
cp .env.example .env      # then edit the source dirs (MD_SOURCE / TXT_SOURCE / PDF_SOURCE)
uv sync                   # creates .venv and installs deps; torch is pulled from the cu128 index
just prefetch             # fetch whisperX model weights (prints manual steps if the network fails)
```

`pyproject.toml` routes `torch`/`torchaudio`/`torchvision` to the pytorch cu128 index, so `uv sync`
does not pick up the CPU-only wheels from PyPI.

## Usage

```sh
just add <name.md>                       # full pipeline -> mp4
just add laika-come-home.zh-cn.md --pdf-variant screen --chars 20
just add <name.md> --pdf /path/to/x.pdf --layout linear
```

`<name.md>` is resolved against the cwd, then `MD_SOURCE` (`TXT_SOURCE` for `.txt`).
Audio must sit next to the md as `<name.md>.mp3`.

### Individual stages

```sh
just probe   <target>     # stages 01: md segments / audio / PDF
just ingest  <target>     # stages 02: rinsed text + 16k mono wav
just align   <target>     # stages 03: whisperX forced alignment (+ char anchors)
just verify  <target>     # stages 04: timeline QC + page mapping
just render  <target>     # stages 05: highlights + turns -> mp4
just report  <target>     # stages 06: aggregate report
just prefetch             # model weights
just bleach  <src.txt>    # Gutenberg .txt -> standard .md
```

### PDF variants

The video shows whole PDF pages; page turns follow the narration. Highlighting is layout-agnostic
(text blocks are matched to the reference text), so any of these work:

- `--pdf-variant screen` → `<stem>.pdf` (2-up, reading order)
- `--pdf-variant a5` → `<stem>.a5.pdf` (single A5 pages)
- `--pdf-variant booklet` → `<stem>.booklet.pdf` (imposed; pages revisit)

Search order: `--pdf` > `--pdf-variant` (in `--pdf-dir`, else `PDF_SOURCE`, else the md's directory)
> `PDF_SOURCE`. `--layout linear` assumes PDF page order equals reading order (fast, safe for
screen/a5); `auto` (default) recovers reading order from content (needed for imposed booklets).

### Highlight unit

- `--unit paragraph` (default): one highlight per paragraph.
- `--chars N` (alias `--words N`): split each paragraph every ~N characters at the nearest
  punctuation. Sentence times are anchored to word-level timestamps, so `--chars 20` tracks speech
  more tightly than paragraph mode.

## Output

Everything lands in `_output/<run_id>/` with per-stage subdirs (`01_probe` … `06_report`).
The mp4 is `_output/<run_id>/05_render/<stem>.aligned.<unit>.mp4`; the aggregated report is
`_output/<run_id>/06_report/report.md`.

## Input contract (standard .md)

- Leading metadata fenced code block(s) — **not narrated**.
- `##` headings split chapters (optional for short pieces).
- One paragraph per line, two trailing spaces; blank lines between paragraphs.
- Inline footnote markers `[^n]`; definitions `[^n]: …` collected at the end — **not narrated**.
- Long works: one `.md` per chapter in a directory (directory = volume).
