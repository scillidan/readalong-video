set dotenv-load

# == add (full pipeline: standard .md + sibling .mp3 -> mp4)
# Trailing flags are forwarded, e.g. --pdf-variant screen --chars 20 --layout linear.
add target *flags:
    uv run scripts/add.py "{{target}}" {{ flags }}

# == probe (01: md segments / audio / pdf)
probe target *flags:
    uv run scripts/probe.py "{{target}}" {{ flags }}

# == ingest (02: rinsed text + 16k mono wav)
ingest target *flags:
    uv run scripts/ingest.py "{{target}}" {{ flags }}

# == align (03: whisperX forced alignment)
align target *flags:
    uv run scripts/align.py "{{target}}" {{ flags }}

# == verify (04: timeline QC + page mapping)
verify target *flags:
    uv run scripts/verify.py "{{target}}" {{ flags }}

# == render (05: highlights + turns -> mp4)
render target *flags:
    uv run scripts/render.py "{{target}}" {{ flags }}

# == report (06: aggregate report)
report target *flags:
    uv run scripts/report.py "{{target}}" {{ flags }}

# == prefetch (whisperX weights; manual steps printed on failure)
prefetch:
    uv run scripts/prefetch.py

# == bleach (Project Gutenberg .txt -> standard .md)
bleach target *flags:
    uv run scripts/bleach.py "{{target}}" {{ flags }}
