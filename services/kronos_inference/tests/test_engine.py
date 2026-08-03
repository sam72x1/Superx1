from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from services.kronos_inference.config import DEFAULT_KRONOS_SOURCE_REVISION, ServiceConfig
from services.kronos_inference.engine import (
    EngineUnavailable,
    KronosEngine,
    _runtime_version_matches,
)


class EngineReadinessTests(unittest.TestCase):
    def test_health_is_liveness_and_includes_source_identity(self) -> None:
        engine = KronosEngine(ServiceConfig())
        body = engine.health()
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["model_loaded"])
        self.assertEqual(body["kronos_revision"], DEFAULT_KRONOS_SOURCE_REVISION)
        self.assertEqual(body["device"], "auto")
        self.assertEqual(body["max_context"], 512)

    def test_ready_requires_checkout_shape_and_dependencies_without_importing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_path = Path(temporary_directory)
            engine = KronosEngine(ServiceConfig(kronos_repo_path=str(repo_path)))
            with patch("services.kronos_inference.engine.find_spec") as find:
                self.assertFalse(engine.is_ready())
                find.assert_not_called()

            model_directory = repo_path / "model"
            model_directory.mkdir()
            (model_directory / "kronos.py").write_text("", encoding="utf-8")
            engine = KronosEngine(ServiceConfig(kronos_repo_path=str(repo_path)))
            with patch.object(engine, "_verify_source_revision"), patch(
                    "services.kronos_inference.engine.find_spec",
                    return_value=object()) as find, patch(
                    "services.kronos_inference.engine.package_version",
                    side_effect={
                        "numpy": "1.26.4",
                        "pandas": "2.2.2",
                        "torch": "2.13.0+cpu",
                        "einops": "0.8.1",
                        "huggingface_hub": "0.33.1",
                        "safetensors": "0.6.2",
                        "tqdm": "4.67.1",
                    }.get) as version:
                self.assertTrue(engine.is_ready())
                self.assertTrue(engine.is_ready())
                self.assertEqual(find.call_args_list, [
                    call("numpy"),
                    call("pandas"),
                    call("torch"),
                    call("einops"),
                    call("huggingface_hub"),
                    call("safetensors"),
                    call("tqdm"),
                ])
                self.assertEqual(version.call_count, 7)

            engine = KronosEngine(ServiceConfig(kronos_repo_path=str(repo_path)))
            with patch.object(engine, "_verify_source_revision"), patch(
                    "services.kronos_inference.engine.find_spec",
                    side_effect=[object(), None]), patch(
                    "services.kronos_inference.engine.package_version",
                    return_value="1.26.4"):
                self.assertFalse(engine.is_ready())

            engine = KronosEngine(ServiceConfig(kronos_repo_path=str(repo_path)))
            with patch.object(engine, "_verify_source_revision"), patch(
                    "services.kronos_inference.engine.find_spec",
                    return_value=object()), patch(
                    "services.kronos_inference.engine.package_version",
                    return_value="wrong-version"):
                self.assertFalse(engine.is_ready())

    def test_runtime_pin_accepts_only_the_official_cpu_local_build(self) -> None:
        self.assertTrue(_runtime_version_matches("torch", "2.13.0", "2.13.0"))
        self.assertTrue(
            _runtime_version_matches("torch", "2.13.0+cpu", "2.13.0")
        )
        self.assertFalse(
            _runtime_version_matches("torch", "2.13.0+cu130", "2.13.0")
        )
        self.assertFalse(
            _runtime_version_matches("numpy", "1.26.4+cpu", "1.26.4")
        )

    def test_ready_rejects_wrong_or_modified_git_checkout(self) -> None:
        config = ServiceConfig(
            kronos_repo_path="/tmp/kronos", kronos_source_revision="a" * 40)
        engine = KronosEngine(config)
        repo = Path("/tmp/kronos")

        clean_head = Mock(stdout=("a" * 40) + "\n")
        clean_status = Mock(stdout="")
        tracked = Mock(stdout="model/kronos.py\n")
        with patch("services.kronos_inference.engine.subprocess.run",
                   side_effect=[clean_head, clean_status, tracked]) as run:
            engine._verify_source_revision(repo)
            self.assertEqual(run.call_count, 3)

        wrong_head = Mock(stdout=("b" * 40) + "\n")
        with patch("services.kronos_inference.engine.subprocess.run",
                   side_effect=[wrong_head, clean_status, tracked]):
            with self.assertRaisesRegex(EngineUnavailable, "لا تطابق"):
                engine._verify_source_revision(repo)

        dirty_status = Mock(stdout=" M model/kronos.py\n")
        with patch("services.kronos_inference.engine.subprocess.run",
                   side_effect=[clean_head, dirty_status, tracked]):
            with self.assertRaisesRegex(EngineUnavailable, "معدّلة"):
                engine._verify_source_revision(repo)

        with tempfile.TemporaryDirectory() as temporary_directory:
            real_repo = Path(temporary_directory)
            (real_repo / "model").mkdir()
            (real_repo / "model" / "kronos.py").write_text("", encoding="utf-8")
            (real_repo / "torch.py").write_bytes(b"unexpected import shadow")
            with patch("services.kronos_inference.engine.subprocess.run",
                       side_effect=[clean_head, clean_status, tracked]):
                with self.assertRaisesRegex(EngineUnavailable, "غير مثبتة"):
                    engine._verify_source_revision(real_repo)

    def test_load_rejects_wrong_dependency_versions_before_importing_runtime(self) -> None:
        engine = KronosEngine(ServiceConfig(kronos_repo_path="/tmp/kronos"))
        with patch.object(
                engine, "_resolve_repo_path", return_value=Path("/tmp/kronos")
        ), patch(
            "services.kronos_inference.engine._runtime_dependencies_ready",
            return_value=False,
        ), patch(
            "services.kronos_inference.engine.importlib.import_module"
        ) as import_module:
            with self.assertRaisesRegex(EngineUnavailable, "إصدارات اعتمادات"):
                engine._load()  # noqa: SLF001 — اختبار حارس مسار POST المباشر
            import_module.assert_not_called()
