"""Shared, revision-scoped paths for the Render build artifact."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_KRONOS_SOURCE_REVISION


@dataclass(frozen=True)
class RuntimeLayout:
    project_root: Path
    generated_root: Path
    kronos_repo_path: Path
    huggingface_home: Path
    manifest_path: Path


class ArtifactIntegrityError(RuntimeError):
    """A baked model snapshot is missing, broken, or outside its cache."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_inventory(snapshot_path: Path, cache_root: Path) -> list[dict[str, Any]]:
    """Hash every snapshot file and reject broken or escaping symlinks."""

    try:
        snapshot = snapshot_path.resolve(strict=True)
        cache = cache_root.resolve(strict=True)
        snapshot.relative_to(cache)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArtifactIntegrityError("مسار snapshot خارج cache المثبت") from exc
    if not snapshot.is_dir():
        raise ArtifactIntegrityError("snapshot المثبت ليس مجلدًا")

    for required_name in ("config.json", "model.safetensors"):
        required = snapshot / required_name
        if not required.is_file():
            raise ArtifactIntegrityError(f"snapshot ينقصه {required_name}")

    inventory: list[dict[str, Any]] = []
    for path in sorted(snapshot.rglob("*")):
        if path.is_symlink() and not path.exists():
            raise ArtifactIntegrityError("snapshot يحتوي symlink مكسورًا")
        if not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(cache)
            size = resolved.stat().st_size
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactIntegrityError("ملف snapshot خارج cache المثبت") from exc
        if size <= 0:
            raise ArtifactIntegrityError("snapshot يحتوي ملفًا فارغًا")
        inventory.append(
            {
                "path": path.relative_to(snapshot).as_posix(),
                "size": size,
                "sha256": _sha256(resolved),
            }
        )
    if not inventory:
        raise ArtifactIntegrityError("snapshot المثبت فارغ")
    return inventory


def runtime_layout(project_root: Path | None = None) -> RuntimeLayout:
    """Return deterministic paths that are copied into Render's build artifact."""

    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    generated = root / ".render" / "kronos-runtime"
    return RuntimeLayout(
        project_root=root,
        generated_root=generated,
        kronos_repo_path=generated / f"source-{DEFAULT_KRONOS_SOURCE_REVISION}",
        huggingface_home=generated / "huggingface",
        manifest_path=generated / "manifest.json",
    )
