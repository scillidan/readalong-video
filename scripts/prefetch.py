"""Fetch and verify whisperX model weights; on repeated failure print manual steps and exit 1."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = (
    "0"  # host sets =1 without the package installed
)

from huggingface_hub import constants, snapshot_download  # noqa: E402

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
Automatic prefetch failed (network). Resolve manually, then rerun `just prefetch`:

  A) install hf_transfer:  uv run python -m pip install hf_transfer
  B) mirror + no hf_transfer:
       set "HF_HUB_ENABLE_HF_TRANSFER=0"
       set "HF_ENDPOINT=https://hf-mirror.com"
       uv run hf download Systran/faster-whisper-large-v3
       uv run hf download jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn
  C) place files into the HF cache yourself: %USERPROFILE%\\.cache\\huggingface\\hub
"""


def _dir(repo: str) -> Path:
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


def fetch(retries: int = 6) -> bool:
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


def main() -> None:
    if fetch():
        print("all model weights ready")
        return
    print(MANUAL, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
