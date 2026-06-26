"""رادار التخفيف (SEC EDGAR) — يحذّر من الطرح/التخفيف القادم.

التخفيف (Dilution) = إصدار الشركة أسهمًا جديدة (طرح) يزيد المعروض ويضغط
السعر. للسهم الصاعد +20% هذا **خطر مباشر**: كثير من الـ small-cap تصدر
أسهمًا أو تفعّل ATM في ذروة الارتفاع لتجمع نقدًا → ينهار السهم. نرصده من
ملفات SEC العامة ونحذّر منه (تمامًا كمبدأ «الشورت يضرّ») — **لا نكافئ أبدًا**.

المصدر: SEC EDGAR العام (مجاني، بلا مفتاح). نخطوتان:
  1. خريطة الرمز→CIK من company_tickers.json (تُحمّل مرة، تُكاش في الذاكرة).
  2. ملفات الشركة الأخيرة من data.sec.gov/submissions/CIK##########.json.

تصنيف النماذج:
  • طرح فعّال/وشيك (خطر مرتفع): 424B* · EFFECT · *MEF · FWP  ضمن نافذة قصيرة.
  • رفّ مُسجّل (خطر متوسط): S-1 · S-3 · F-1 · F-3 (+ ASR) ضمن نافذة أطول
    = قدرة جاهزة على التخفيف.

best-effort تمامًا: لا إنترنت/خطأ/رمز غير موجود = None (لا يضرّ ولا ينفع،
البوت يكمل). SEC تشترط ترويسة User-Agent تعريفية (SEC_USER_AGENT).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import requests

from .config import Config
from .models import DilutionResult

logger = logging.getLogger(__name__)

_CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# نماذج طرح فعّال/وشيك (نشرة جارية أو سريان تسجيل) — الأخطر.
_ACTIVE_FORMS = {
    "424B1", "424B2", "424B3", "424B4", "424B5", "424A",
    "EFFECT", "S-1MEF", "S-3MEF", "FWP",
}
# نماذج رفّ/تسجيل (قدرة على التخفيف لاحقًا) — خطر متوسط.
_SHELF_FORMS = {
    "S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR",
    "F-1", "F-1/A", "F-3", "F-3/A", "F-3ASR",
}


def _days_since(d: str, today: date) -> int | None:
    """عدد الأيام من تاريخ ملف (YYYY-MM-DD) حتى اليوم. None لو غير صالح."""
    try:
        return (today - date.fromisoformat(d[:10])).days
    except (ValueError, TypeError):
        return None


class SecRadar:
    """عميل رصد ملفات SEC التخفيفية. best-effort، آمِن للاستدعاء من الحلقة."""

    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self._http = session or requests.Session()
        self._http.headers.update({
            "User-Agent": cfg.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self._cik_map: dict[str, str] | None = None   # رمز → CIK (10 خانات)

    # ── طبقة النقل (تُموَّك في الاختبارات) ────────────────────────
    def _get_json(self, url: str) -> dict | list | None:
        try:
            resp = self._http.get(url, timeout=12)
        except requests.RequestException as exc:
            logger.debug("SEC شبكة فشلت %s: %s", url, exc)
            return None
        if resp.status_code != 200:
            logger.debug("SEC رفض (%s) %s", resp.status_code, url)
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # ── خريطة الرمز→CIK (تُحمّل مرة) ──────────────────────────────
    def _cik_for(self, ticker: str) -> str | None:
        if self._cik_map is None:
            data = self._get_json(_CIK_MAP_URL)
            mapping: dict[str, str] = {}
            if isinstance(data, dict):
                for row in data.values():
                    try:
                        sym = str(row["ticker"]).upper()
                        mapping[sym] = f"{int(row['cik_str']):010d}"
                    except (KeyError, TypeError, ValueError):
                        continue
            self._cik_map = mapping   # حتى لو فاضية: لا نعيد المحاولة كل دورة
        return self._cik_map.get(ticker.upper())

    # ── الملفات الأخيرة لشركة ─────────────────────────────────────
    def _recent_filings(self, cik10: str) -> list[tuple[str, str]]:
        """يرجّع [(form, filingDate)] لأحدث الملفات. [] عند الفشل."""
        data = self._get_json(_SUBMISSIONS_URL.format(cik10=cik10))
        if not isinstance(data, dict):
            return []
        recent = ((data.get("filings") or {}).get("recent")) or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        return list(zip(forms, dates))

    # ── الواجهة العامة ────────────────────────────────────────────
    def check(self, ticker: str, today: date | None = None
              ) -> DilutionResult | None:
        """يرصد خطر التخفيف لرمز. None لو معطّل/تعذّر/لا CIK."""
        if not self.cfg.dilution_radar_enabled:
            return None
        today = today or datetime.now(timezone.utc).date()
        cik = self._cik_for(ticker)
        if not cik:
            return None
        filings = self._recent_filings(cik)
        if not filings:
            return None

        active: list[tuple[str, str, int]] = []   # (form, date, days)
        shelf: list[tuple[str, str, int]] = []
        for form, fdate in filings:
            f = (form or "").upper().strip()
            days = _days_since(fdate, today)
            if days is None or days < 0:
                continue
            if f in _ACTIVE_FORMS and days <= self.cfg.dilution_active_days:
                active.append((f, fdate, days))
            elif f in _SHELF_FORMS and days <= self.cfg.dilution_shelf_days:
                shelf.append((f, fdate, days))

        if active:
            latest = min(active, key=lambda x: x[2])   # الأحدث
            forms = sorted({f for f, _, _ in active})
            return DilutionResult(
                risk="مرتفع", forms=forms,
                latest_form=latest[0], latest_date=latest[1],
                note=(f"طرح/تخفيف فعّال — {latest[0]} قبل {latest[2]} يوم "
                      "(إصدار أسهم يضغط السعر)"))
        if shelf:
            latest = min(shelf, key=lambda x: x[2])
            forms = sorted({f for f, _, _ in shelf})
            return DilutionResult(
                risk="متوسط", forms=forms,
                latest_form=latest[0], latest_date=latest[1],
                note=(f"رفّ تخفيف مُسجّل — {latest[0]} قبل {latest[2]} يوم "
                      "(قدرة جاهزة على إصدار أسهم)"))
        return DilutionResult(risk="لا")
