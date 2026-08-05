from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CPU_TORCH = '"torch==2.13.0+cpu"'
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def test_render_installs_cpu_torch_before_kronos_requirements():
    """العقد الحقيقي: services/kronos_inference/requirements.txt يثبّت
    `torch==2.13.0` العام — وهو يسحب حزم CUDA ضخمة على Linux بلا فائدة (لا GPU
    على Render). فلازم يسبقه wheel الـ+cpu كي يجده pip مُرضًى. (الاختبار كان
    مكتوبًا على نصّ حرفي لأمر واحد؛ صار يعبّر عن العقد لا عن الصياغة.)"""
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

    cpu_install = f"pip install {CPU_TORCH} --index-url {CPU_INDEX}"
    kronos_reqs = "-r services/kronos_inference/requirements.txt"

    assert cpu_install in blueprint
    assert kronos_reqs in blueprint
    assert blueprint.index(cpu_install) < blueprint.index(kronos_reqs)


def test_kronos_build_failure_never_blocks_deploying_the_scanner():
    """ميزة قياس لا يجوز أن تصير حاجزًا أمام شحن الحافة (§3): سلسلة && واحدة
    كانت تعني أن أي فشل شبكي في (فهرس pytorch · مصدر Kronos · HuggingFace)
    يُفشل البناء كلّه ⇒ لا يُنشر أي إصلاح تنبيه. الماسح يُبنى ويُتحقَّق أوّلًا
    وحاسمًا، وسلسلة Kronos بعده داخل قوسين لا تُفشل البناء."""
    import re
    import shutil
    import subprocess

    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    cmd = re.search(r"buildCommand:\s*(.+)", blueprint).group(1).strip()

    scanner_install = "pip install -r requirements.txt"
    import_check = 'python -c "import runner_scanner.main"'
    assert cmd.index(scanner_install) < cmd.index(CPU_TORCH), \
        "متطلّبات الماسح لازم تسبق شقّ Kronos"
    assert cmd.index(import_check) < cmd.index(CPU_TORCH), \
        "فحص استيراد الماسح لازم يسبق شقّ Kronos"

    # الدلالة الفعلية في الصدفة: فشل Kronos ⇒ 0 · فشل الماسح ⇒ ≠0
    sh = shutil.which("sh")
    if sh:                      # (تخطٍّ آمن على بيئة بلا sh)
        shape = "%s && %s && ( %s || echo skipped )"
        ok = subprocess.run([sh, "-c", shape % ("true", "true", "false")])
        bad = subprocess.run([sh, "-c", shape % ("false", "true", "false")])
        assert ok.returncode == 0, "فشل Kronos يجب ألّا يمنع النشر"
        assert bad.returncode != 0, "فشل الماسح يجب أن يُفشل البناء"


def test_optional_docker_image_uses_the_same_cpu_only_torch_wheel():
    dockerfile = (ROOT / "services/kronos_inference/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert CPU_TORCH in dockerfile
    assert CPU_INDEX in dockerfile
    assert dockerfile.index(CPU_TORCH) < dockerfile.index(
        "-r /tmp/kronos-requirements.txt"
    )
