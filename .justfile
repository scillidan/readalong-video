set dotenv-load

# == add (full pipeline: standard .md file/dir + --media audio file/dir -> mp4)
# Trailing flags are forwarded, e.g. --gen-a5 --chars 20 --output <file|dir>.
add target *flags:
    uv run scripts/add.py "{{target}}" {{ flags }}

# == gen-a4 (standard .md -> A4 landscape 2-up reader PDF)
gen-a4 target *flags:
    uv run scripts/gen_pdf.py "{{target}}" --a4 {{ flags }}

# == gen-a5 (standard .md -> single A5 pages PDF)
gen-a5 target *flags:
    uv run scripts/gen_pdf.py "{{target}}" --a5 {{ flags }}

# == probe (01: md segments / audio / pdf)
probe target *flags:
    uv run scripts/probe.py "{{target}}" {{ flags }}

# == ingest (02: rinsed text + 16k mono wav; pass --media <file/dir> or sibling <stem>.mp3)
ingest target *flags:
    uv run scripts/ingest.py "{{target}}" {{ flags }}

# == align (03: whisperX forced alignment; legacy torch CUDA engine)
align target *flags:
    uv run scripts/align.py "{{target}}" {{ flags }}

# == alignw (03: whisper.cpp forced alignment; default stable CPU engine)
alignw target *flags:
    uv run scripts/align_whispercpp.py "{{target}}" {{ flags }}

# == verify (04: timeline QC + page mapping)
verify target *flags:
    uv run scripts/verify.py "{{target}}" {{ flags }}

# == render (05: highlights + turns -> mp4)
render target *flags:
    uv run scripts/render.py "{{target}}" {{ flags }}

# == report (06: aggregate report)
report target *flags:
    uv run scripts/report.py "{{target}}" {{ flags }}

# == prefetch (whisper.cpp assets required, whisperX weights optional; manual steps printed on failure)
prefetch:
    uv run scripts/prefetch.py

# == bleach (Project Gutenberg .txt -> standard .md)
bleach target *flags:
    uv run scripts/bleach.py "{{target}}" {{ flags }}
