"""Fetch/verify model assets.

Required:  whisper.cpp binary + ggml model (default align engine, CPU) —
           missing assets print manual steps and exit 1.
Optional:  whisperX/faster-whisper + wav2vec2 weights (only for --engine
           whisperx) — on repeated failure print manual steps and continue.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .align_whispercpp import DEFAULT_BIN, DEFAULT_MODEL

REPOS = {
    "Systran/faster-whisper-large-v3": (
        ["model.bin", "config.json"],
        "ASR large-v3 (CTranslate2)",
    ),
    "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn": (
        ["config.json"],
        "zh forced-alignment wav2vec2",
    ),
}

MANUAL = """
Automatic prefetch failed (network). Resolve manually, then rerun `readalong prefetch`:

  A) speed up with hf_transfer:
       uv tool install --with hf_transfer readalong-video
  B) mirror + no hf_transfer:
       set "HF_HUB_ENABLE_HF_TRANSFER=0"
       set "HF_ENDPOINT=https://hf-mirror.com"
       uvx --from "huggingface_hub[cli]" hf download Systran/faster-whisper-large-v3
       uvx --from "huggingface_hub[cli]" hf download jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn
  C) place files into the HF cache yourself: %USERPROFILE%\\.cache\\huggingface\\hub
"""

# whisperX weights are optional (only --engine whisperx uses them); their failure is a warning.
WCPP_MANUAL = """
whisper.cpp assets missing — required by the default align engine. Resolve manually:

  A) binary (CPU build; sha256 6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb):
       https://gh-proxy.com/https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip
     extract so whisper-cli.exe lands at:
       %USERPROFILE%\\.cache\\whisper.cpp\\bin\\x64\\Release\\whisper-cli.exe
  B) model: put a ggml whisper bin (e.g. ggml-large-v3-turbo.bin) at
       %USERPROFILE%\\Local\\Model\\whisper-cpp\\ggml-large-v3-turbo.bin
     or point WHISPER_CPP_MODEL at any ggml .bin
"""


def _dir(repo: str) -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))


def installed(repo: str) -> bool:
    required = REPOS[repo][0]
    snaps = _dir(repo) / "snapshots"
    if not snaps.is_dir():
        return False
    for snap in snaps.iterdir():
        names = {f.name for f in snap.iterdir()} if snap.is_dir() else set()
        if all(r in names for r in required) and any(
            w in names for w in ("model.bin", "model.safetensors", "pytorch_model.bin")
        ):
            return True
    return False


def fetch(retries: int = 6) -> bool | None:
    """Fetch whisperX weights; None when huggingface_hub is unavailable (the
    whisperx extra is not installed — only needed for --engine whisperx)."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return None

    # The host may set HF_HUB_ENABLE_HF_TRANSFER=1 without the package installed.
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

    for repo, (_, label) in REPOS.items():
        if installed(repo):
            print(f"OK    {repo}")
            continue
        ok = False
        for attempt in range(1, retries + 1):
            print(f"fetch {label}: {repo} (attempt {attempt})", flush=True)
            try:
                snapshot_download(repo, max_workers=4)
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
                time.sleep(min(10 * attempt, 60))
        if not ok:
            return False
    return all(installed(r) for r in REPOS)


def wcpp_ready() -> tuple[bool, str]:
    if not DEFAULT_BIN.exists():
        return False, f"whisper-cli not found: {DEFAULT_BIN}"
    if not DEFAULT_MODEL.exists():
        return False, f"whisper.cpp model not found: {DEFAULT_MODEL}"
    return True, ""


def add_args(ap: argparse.ArgumentParser) -> None:
    pass  # no flags


def run(args: argparse.Namespace) -> None:
    ok, why = wcpp_ready()
    if not ok:
        print(why, file=sys.stderr)
        print(WCPP_MANUAL, file=sys.stderr)
        raise SystemExit(1)
    print(f"OK    whisper.cpp: {DEFAULT_BIN.name}")
    print(f"OK    whisper.cpp model: {DEFAULT_MODEL.name}")
    status = fetch()
    if status is None:
        print(
            "whisperX weights: skipped (install the whisperx extra to enable: "
            "uv tool install 'readalong-video[whisperx]')"
        )
        return
    if status:
        print("all model weights ready")
        return
    print(MANUAL, file=sys.stderr)
    print("whisperX weights missing — only needed for --engine whisperx; continuing")
