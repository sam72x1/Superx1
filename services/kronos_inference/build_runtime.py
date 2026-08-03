"""Bake the pinned Kronos source and weights into a native Render artifact."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .config import (
    DEFAULT_KRONOS_SOURCE_REVISION,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    DEFAULT_TOKENIZER_ID,
    DEFAULT_TOKENIZER_REVISION,
    ServiceConfig,
)
from .runtime_layout import RuntimeLayout, runtime_layout, snapshot_inventory


KRONOS_REPOSITORY = "https://github.com/shiyu-coder/Kronos.git"


class RuntimeBuildError(RuntimeError):
    """The immutable runtime artifact could not be prepared safely."""


def _run(command: Sequence[str], *, timeout: int = 900) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeBuildError(f"فشل أمر البناء: {command[0]}") from exc
    return completed.stdout.strip()


def verify_checkout(
    repo_path: Path,
    *,
    expected_revision: str = DEFAULT_KRONOS_SOURCE_REVISION,
) -> None:
    """Reject a missing, modified, or differently pinned Kronos checkout."""

    if not repo_path.is_dir():
        raise RuntimeBuildError("مسار مصدر Kronos غير موجود")
    for required in (repo_path / "model" / "kronos.py", repo_path / "LICENSE"):
        if not required.is_file():
            raise RuntimeBuildError("checkout Kronos ناقص")

    prefix = ("git", "--no-optional-locks", "-C", str(repo_path))
    head = _run((*prefix, "rev-parse", "HEAD"), timeout=30)
    if head.casefold() != expected_revision.casefold():
        raise RuntimeBuildError("HEAD مصدر Kronos لا يطابق المراجعة المثبتة")
    if _run((*prefix, "status", "--porcelain", "--untracked-files=all"), timeout=30):
        raise RuntimeBuildError("checkout Kronos ليس نظيفًا")


def ensure_checkout(layout: RuntimeLayout) -> Path:
    """Clone once into the revision-scoped artifact path, then verify it."""

    layout.generated_root.mkdir(parents=True, exist_ok=True)
    repo_path = layout.kronos_repo_path
    if not repo_path.exists():
        _run(
            (
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                KRONOS_REPOSITORY,
                str(repo_path),
            ),
            timeout=1800,
        )
        _run(
            (
                "git",
                "--no-optional-locks",
                "-C",
                str(repo_path),
                "checkout",
                "--detach",
                DEFAULT_KRONOS_SOURCE_REVISION,
            ),
            timeout=300,
        )
    verify_checkout(repo_path)
    return repo_path


def ensure_weights(layout: RuntimeLayout) -> dict[str, Path]:
    """Download pinned snapshots and prove the same revisions work offline."""

    layout.huggingface_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(layout.huggingface_home)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ.pop("HF_HUB_OFFLINE", None)

    from huggingface_hub import snapshot_download

    assets = (
        ("tokenizer", DEFAULT_TOKENIZER_ID, DEFAULT_TOKENIZER_REVISION),
        ("model", DEFAULT_MODEL_ID, DEFAULT_MODEL_REVISION),
    )
    hub_cache = layout.huggingface_home / "hub"
    resolved: dict[str, Path] = {}
    for label, repo_id, revision in assets:
        snapshot = snapshot_download(
            repo_id,
            revision=revision,
            cache_dir=str(hub_cache),
        )
        if not Path(snapshot).is_dir():
            raise RuntimeBuildError(f"تنزيل {label} لم ينتج مجلدًا")
        offline_snapshot = snapshot_download(
            repo_id,
            revision=revision,
            cache_dir=str(hub_cache),
            local_files_only=True,
        )
        if Path(offline_snapshot).resolve() != Path(snapshot).resolve():
            raise RuntimeBuildError(f"تحقق offline لـ{label} أعاد snapshot مختلفًا")
        resolved[label] = Path(snapshot).resolve()
    return resolved


def _manifest(layout: RuntimeLayout, snapshots: dict[str, Path]) -> dict[str, Any]:
    cache_root = layout.huggingface_home.resolve()
    return {
        "schema": 2,
        "kronos_repository": KRONOS_REPOSITORY,
        "kronos_revision": DEFAULT_KRONOS_SOURCE_REVISION,
        "kronos_repo_path": layout.kronos_repo_path.relative_to(
            layout.project_root
        ).as_posix(),
        "model": DEFAULT_MODEL_ID,
        "model_revision": DEFAULT_MODEL_REVISION,
        "tokenizer": DEFAULT_TOKENIZER_ID,
        "tokenizer_revision": DEFAULT_TOKENIZER_REVISION,
        "huggingface_home": layout.huggingface_home.relative_to(
            layout.project_root
        ).as_posix(),
        "snapshots": {
            label: {
                "path": snapshot.relative_to(cache_root).as_posix(),
                "files": snapshot_inventory(snapshot, cache_root),
            }
            for label, snapshot in sorted(snapshots.items())
        },
    }


def write_manifest(layout: RuntimeLayout, snapshots: dict[str, Path]) -> None:
    body = json.dumps(
        _manifest(layout, snapshots),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary = layout.manifest_path.with_suffix(".json.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(layout.manifest_path)


def verify_service_readiness(layout: RuntimeLayout) -> None:
    """Run the same source/dependency guard used by `/ready`, without weights."""

    from .engine import KronosEngine

    config = ServiceConfig(kronos_repo_path=str(layout.kronos_repo_path), device="cpu")
    if not KronosEngine(config).is_ready():
        raise RuntimeBuildError("فشل حارس جاهزية خدمة Kronos بعد البناء")


def main() -> None:
    layout = runtime_layout()
    checkout = ensure_checkout(layout)
    snapshots = ensure_weights(layout)
    write_manifest(layout, snapshots)
    verify_service_readiness(layout)
    print(f"Kronos runtime ready: source={checkout.name}, assets=offline")


if __name__ == "__main__":
    main()
