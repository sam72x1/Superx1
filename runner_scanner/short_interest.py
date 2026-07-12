"""مزوّد بيانات الشورت بسلسلة مصادر احتياطية.

الشورت **يضرّ السهم** (ضغط بيعي) — نعرضه كتحذير لا نكافئه بالدرجة.

سلسلة المصادر (الأولوية):
1. Fintel  — الأدقّ، **محاولة صامتة** (Cloudflare غالبًا يحجبه). يعطي
             حجم الشورت اليومي + نسبة من الفلوت.
2. FINRA   — الأساس الموثوق (ملفات RegSHO اليومية المجانية). حجم يومي.
3. Yahoo   — احتياطي (yfinance). **نسبة من الفلوت فقط** (لا تُخلط بالحجم).

عند فشل الكل → None («—» في البطاقة). **تعذّر ≠ صفر**: لا نرفض ولا
نعاقب على شورت مجهول (البوّابة تعدّي بفائدة الشك).

ملاحظتان: (1) كل المصادر best-effort وصامتة عند الفشل. (2) كاش يومي لكل
رمز يقلّل الطلبات (الشورت يتغيّر يوميًا لا لحظيًا). الجلب الفعلي يعمل من
بيئة الإنتاج (Render)؛ بعض المواقع محجوبة في بيئات معزولة.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"


@dataclass
class ShortInfo:
    """نتيجة الشورت المجمّعة (لا تُخلط النسبتان)."""

    short_float_pct: Optional[float] = None   # نسبة الشورت من الفلوت%
    short_vol_pct: Optional[float] = None      # نسبة حجم الشورت اليومي%
    source: str = ""                           # المصادر المستخدمة

    @property
    def has_data(self) -> bool:
        return self.short_float_pct is not None or self.short_vol_pct is not None


class ShortInterestProvider:
    """يجلب الشورت بسلسلة مصادر مع كاش يومي لكل رمز."""

    def __init__(self, session: Optional[requests.Session] = None,
                 timeout: float = 6.0):
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA})
        self.timeout = timeout
        self._cache: dict[tuple[str, str], Optional[ShortInfo]] = {}
        self._finra_cache: dict[str, dict[str, tuple[float, float]]] = {}

    # ── الواجهة ───────────────────────────────────────────────────
    def get(self, ticker: str, today: Optional[str] = None) -> Optional[ShortInfo]:
        """يرجّع ShortInfo أو None (= «—»). يكاش لكل رمز/يوم."""
        day = today or date.today().isoformat()
        key = (ticker.upper(), day)
        if key in self._cache:
            return self._cache[key]
        info = self._fetch(ticker)
        self._cache[key] = info
        return info

    def _fetch(self, ticker: str) -> Optional[ShortInfo]:
        sources: list[str] = []
        # 1) Fintel (قد يعطي النسبتين)
        fintel = self._fintel(ticker)
        float_pct = fintel.short_float_pct if fintel else None
        vol_pct = fintel.short_vol_pct if fintel else None
        if fintel and fintel.has_data:
            sources.append("Fintel")

        # 2) FINRA لحجم الشورت اليومي (لو ناقص)
        if vol_pct is None:
            finra = self._finra_vol_pct(ticker)
            if finra is not None:
                vol_pct = finra
                sources.append("FINRA")

        # 3) Yahoo لنسبة الفلوت (لو ناقصة)
        if float_pct is None:
            yahoo = self._yahoo_float_pct(ticker)
            if yahoo is not None:
                float_pct = yahoo
                sources.append("Yahoo")

        if float_pct is None and vol_pct is None:
            return None   # تعذّر الكل → «—»
        return ShortInfo(short_float_pct=float_pct, short_vol_pct=vol_pct,
                         source="+".join(sources))

    # ── 1) Fintel (محاولة صامتة) ──────────────────────────────────
    def _fintel(self, ticker: str) -> Optional[ShortInfo]:
        try:
            resp = self._http.get(f"https://fintel.io/ss/us/{ticker.lower()}",
                                  timeout=self.timeout)
            if resp.status_code != 200:
                return None   # Cloudflare/403 غالبًا — صامت
            html = resp.text
        except requests.RequestException:
            return None
        float_pct = _search_pct(
            html, r"Short\s*%?\s*(?:of\s*)?Float[^0-9]{0,20}([0-9.]+)\s*%")
        vol_pct = _search_pct(
            html, r"Short\s*Volume\s*Ratio[^0-9]{0,20}([0-9.]+)\s*%")
        info = ShortInfo(short_float_pct=float_pct, short_vol_pct=vol_pct,
                         source="Fintel")
        return info if info.has_data else None

    # ── 2) FINRA RegSHO (حجم الشورت اليومي %) ─────────────────────
    def _finra_vol_pct(self, ticker: str) -> Optional[float]:
        table = self._finra_table()
        if not table:
            return None
        row = table.get(ticker.upper())
        if not row:
            return None
        short_vol, total_vol = row
        if total_vol <= 0:
            return None
        return min(100.0, short_vol / total_vol * 100.0)

    def _finra_table(self) -> dict[str, tuple[float, float]]:
        """يحمّل أحدث ملف RegSHO متاح (يجرّب اليوم ثم أيامًا سابقة)، ويكاش."""
        for back in range(0, 6):
            d = date.today() - timedelta(days=back)
            ymd = d.strftime("%Y%m%d")
            if ymd in self._finra_cache:
                return self._finra_cache[ymd]
            try:
                resp = self._http.get(_FINRA_URL.format(ymd=ymd),
                                      timeout=self.timeout)
            except requests.RequestException:
                continue
            if resp.status_code != 200 or "|" not in resp.text:
                continue
            table = _parse_finra(resp.text)
            if table:
                # أبقِ أحدث جدول فقط: القاموس بمفتاح ymd كان ينمو كل يوم تعيشه
                # العملية (~10 آلاف رمز/جدول) بلا إخلاء — وكاش ذاكرة قتل الخدمة
                # بحدّ ذاكرة Render مرّة من قبل.
                self._finra_cache = {ymd: table}
                return table
        return {}

    # ── 3) Yahoo (نسبة الفلوت فقط، عبر yfinance) ──────────────────
    def _yahoo_float_pct(self, ticker: str) -> Optional[float]:
        try:
            import yfinance as yf
        except ImportError:
            return None
        try:
            info = yf.Ticker(ticker).info
            val = info.get("shortPercentOfFloat")
            if val:
                return float(val) * 100.0   # كسر → نسبة مئوية
        except Exception:  # noqa: BLE001 — yfinance قد يرمي أي شيء
            return None
        return None


def _search_pct(text: str, pattern: str) -> Optional[float]:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def _parse_finra(text: str) -> dict[str, tuple[float, float]]:
    """يحلّل ملف RegSHO (Date|Symbol|ShortVolume|ShortExempt|TotalVolume|Market)."""
    out: dict[str, tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(text), delimiter="|")
    for row in reader:
        if len(row) < 5 or row[0].lower() == "date" or not row[1]:
            continue
        try:
            short_vol = float(row[2])
            total_vol = float(row[4])
        except (ValueError, IndexError):
            continue
        out[row[1].upper()] = (short_vol, total_vol)
    return out
