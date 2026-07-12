"""تهيئة اختبارات مشتركة: عزل البيئة عن عتبات الماسح (TEST-17).

بلا هذا العزل، العتبات **تحت الاختبار** تُقرأ من `os.environ` وقت الجمع
(تسع وحدات تبني `Config.from_env()` على مستوى الوحدة). فلو ضُبط أي متغيّر في
الصدفة/الإنتاج اشتقّت الاختبارات توقّعاتها من قيمة منحرفة وبقيت **خضراء
وخاطئة** — وهذا أسوأ من الحمراء (درس تعارض 40 مقابل 60 في late_wave_run_pct).

نمسح كل مفاتيح البيئة التي يقرؤها `from_env` **فور استيراد conftest** (قبل
جمع وحدات الاختبار، فتقرأ CFG على مستوى الوحدة القيم الافتراضية)، ونكرّر
المسح كحزام أمان قبل كل اختبار. المصدر الوحيد للمفاتيح = تحليل مصدر
`from_env` نفسه (لا قائمة يدوية تنجرف)."""

from __future__ import annotations

import inspect
import os
import re

import pytest

from runner_scanner import config as _cfg


def _scanner_env_keys() -> set[str]:
    """كل مفاتيح البيئة التي يقرؤها Config.from_env عبر _f/_i/_s/_b/_ftuple."""
    src = inspect.getsource(_cfg.Config.from_env)
    return set(re.findall(r'_(?:f|i|s|b|ftuple)\(\s*"([A-Z0-9_]+)"', src))


SCANNER_ENV_KEYS = _scanner_env_keys()

# مسح فوري وقت الاستيراد (قبل جمع الوحدات) كي تقرأ الوحدات القيم الافتراضية.
for _k in SCANNER_ENV_KEYS:
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def _isolate_scanner_env(monkeypatch):
    """حزام أمان لكل اختبار: يمسح مفاتيح الماسح حتى لو ضبطها اختبار سابق."""
    for key in SCANNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
