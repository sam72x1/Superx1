from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from services.kronos_inference.build_runtime import RuntimeBuildError, verify_checkout
from services.kronos_inference.runtime_layout import (
    ArtifactIntegrityError,
    snapshot_inventory,
)


class BuildRuntimeTests(unittest.TestCase):
    def _repository(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (root / "model").mkdir()
        (root / "model" / "kronos.py").write_text("# pinned\n", encoding="utf-8")
        (root / "LICENSE").write_text("test license\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "LICENSE", "model/kronos.py"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True)
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return temporary, root, revision

    def test_clean_pinned_checkout_is_accepted(self) -> None:
        temporary, root, revision = self._repository()
        self.addCleanup(temporary.cleanup)
        verify_checkout(root, expected_revision=revision)

    def test_modified_checkout_is_rejected(self) -> None:
        temporary, root, revision = self._repository()
        self.addCleanup(temporary.cleanup)
        (root / "model" / "kronos.py").write_text("# modified\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeBuildError, "ليس نظيفًا"):
            verify_checkout(root, expected_revision=revision)

    def test_snapshot_inventory_hashes_required_files_and_detects_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            snapshot = cache / "hub" / "models--test" / "snapshots" / ("a" * 40)
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text('{"ok":true}\n', encoding="utf-8")
            weights = snapshot / "model.safetensors"
            weights.write_bytes(b"safe-tensors-fixture")

            before = snapshot_inventory(snapshot, cache)
            weights.write_bytes(b"x")
            after = snapshot_inventory(snapshot, cache)

            self.assertNotEqual(before, after)
            self.assertEqual(
                {row["path"] for row in before},
                {"config.json", "model.safetensors"},
            )

    def test_snapshot_inventory_rejects_empty_weight_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            snapshot = cache / "snapshot"
            snapshot.mkdir()
            (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
            (snapshot / "model.safetensors").write_bytes(b"")
            with self.assertRaisesRegex(ArtifactIntegrityError, "فارغ"):
                snapshot_inventory(snapshot, cache)


if __name__ == "__main__":
    unittest.main()
