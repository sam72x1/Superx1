from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CPU_TORCH = '"torch==2.13.0+cpu"'
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def test_render_installs_cpu_torch_before_shared_requirements():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

    cpu_install = f"pip install {CPU_TORCH} --index-url {CPU_INDEX}"
    shared_install = (
        "pip install -r requirements.txt "
        "-r services/kronos_inference/requirements.txt"
    )

    assert cpu_install in blueprint
    assert shared_install in blueprint
    assert blueprint.index(cpu_install) < blueprint.index(shared_install)


def test_optional_docker_image_uses_the_same_cpu_only_torch_wheel():
    dockerfile = (ROOT / "services/kronos_inference/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert CPU_TORCH in dockerfile
    assert CPU_INDEX in dockerfile
    assert dockerfile.index(CPU_TORCH) < dockerfile.index(
        "-r /tmp/kronos-requirements.txt"
    )
