"""غلاف رفيع حول REST endpoints الموثّقة لـ Massive (= Polygon.io).

نستخدم REST خام عبر `requests` بدل SDK، لأن:
- الـ endpoints موثّقة بدقّة (تأكّدنا منها) ومستقرّة.
- يعطينا تحكّمًا كاملًا وقابلية اختبار (نموك هذا الغلاف، لا SDK).
- endpoint الفلوت تجريبي وغير مغلّف في SDK أصلًا → REST خام إلزامي.

كل دوال الجلب ترجّع أنواع models.* مُطبّعة، عشان بقية الوحدات ما تلمس
JSON خام. الأعطال تُرفع كـ MassiveError ليلتقطها monitor.

ملاحظة الـrebrand: المفتاح نفسه يعمل على api.massive.com و api.polygon.io.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from .config import Config
from .models import Bar, Catalyst, SnapshotEntry

logger = logging.getLogger(__name__)


class MassiveError(RuntimeError):
    """عطل في الاتصال بـ Massive (شبكة/مصادقة/حدود)."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after   # مدّة الانتظار المقترحة من المزوّد (429)


class MassiveClient:
    """عميل REST لـ Massive. آمِن للاستخدام من ثريد واحد (حلقة المسح)."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._http = session or requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {cfg.massive_api_key}"})

    # ── طبقة النقل ────────────────────────────────────────────────
    def _get(self, path: str, params: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict[str, Any]:
        """نداء REST مع إعادة محاولة على الأعطال **العابرة** (مهلة/شبكة/429/5xx)
        بتراجع أسّي — حتى لا يكسر فشلٌ لحظيّ دورة المسح أو الباكتيست (القسم 3).
        الأخطاء الدائمة (401/4xx) تُرفع فورًا بلا إعادة (لا فائدة من تكرارها).
        """
        url = f"{self.cfg.massive_rest_base}{path}"
        timeout = timeout if timeout is not None else self.cfg.http_timeout
        retries = max(0, self.cfg.http_max_retries)
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                resp = self._http.get(url, params=params or {}, timeout=timeout)
            except requests.RequestException as exc:
                if last:
                    raise MassiveError(f"شبكة فشلت على {path}: {exc}") from exc
                time.sleep(min(2.0 ** attempt, 8.0))
                continue
            if resp.status_code == 401:
                raise MassiveError("مصادقة مرفوضة (401) — تأكّد من MASSIVE_API_KEY")
            if resp.status_code == 429:
                try:
                    ra = float(resp.headers.get("Retry-After", 0)) or None
                except (TypeError, ValueError):
                    ra = None
                logger.warning("Massive 429 — Retry-After=%s", ra)
                if last:
                    raise MassiveError(
                        "تجاوز حدّ الطلبات (429) — خفّض معدّل الاستدعاء",
                        retry_after=ra)
                time.sleep(ra if ra else min(2.0 ** attempt, 8.0))
                continue
            if resp.status_code >= 500:    # خطأ خادم عابر → أعد المحاولة
                if last:
                    raise MassiveError(
                        f"خطأ {resp.status_code} على {path}: {resp.text[:200]}")
                time.sleep(min(2.0 ** attempt, 8.0))
                continue
            if resp.status_code >= 400:    # خطأ دائم (4xx) → لا إعادة
                raise MassiveError(
                    f"خطأ {resp.status_code} على {path}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise MassiveError(f"رد غير JSON من {path}") from exc
        raise MassiveError(f"تعذّر الاتصال بـ {path} بعد {retries + 1} محاولات")

    # ── Full Market Snapshot (كشف +20%) ───────────────────────────
    def full_snapshot(self) -> list[SnapshotEntry]:
        """كل السوق الأمريكي في نداء واحد. يُرجّع المداخل الصالحة فقط."""
        data = self._get("/v2/snapshot/locale/us/markets/stocks/tickers")
        tickers = data.get("tickers") or []
        out: list[SnapshotEntry] = []
        for t in tickers:
            entry = self._parse_snapshot_entry(t)
            if entry is not None:
                out.append(entry)
        return out

    @staticmethod
    def _parse_snapshot_entry(t: dict[str, Any]) -> Optional[SnapshotEntry]:
        try:
            day = t.get("day") or {}
            prev = t.get("prevDay") or {}
            last_trade = t.get("lastTrade") or {}
            min_bar = t.get("min") or {}
            # السعر اللحظي: آخر صفقة، وإلا إغلاق شمعة الدقيقة، وإلا إغلاق اليوم.
            last_price = (
                last_trade.get("p")
                or min_bar.get("c")
                or day.get("c")
                or 0.0
            )
            last_price = float(last_price or 0.0)
            prev_close = float(prev.get("c") or 0.0)
            # نسبة التغيّر: نفضّل حقل الـ API، وإلا نحسبها احتياطيًا من
            # السعر/إغلاق أمس (حماية لو غاب الحقل = نتجنّب فقدان سهم).
            raw_change = t.get("todaysChangePerc")
            if raw_change is None and prev_close > 0 and last_price > 0:
                change_pct = (last_price - prev_close) / prev_close * 100.0
            else:
                change_pct = float(raw_change or 0.0)
            return SnapshotEntry(
                ticker=t.get("ticker", ""),
                last_price=last_price,
                prev_close=prev_close,
                day_open=float(day.get("o") or 0.0),
                day_high=float(day.get("h") or 0.0),
                day_low=float(day.get("l") or 0.0),
                day_volume=float(day.get("v") or 0.0),
                day_vwap=float(day.get("vw") or 0.0),
                change_pct=change_pct,
                updated_ns=int(t.get("updated") or 0),
            )
        except (TypeError, ValueError):
            return None

    def single_snapshot(self, ticker: str) -> Optional[SnapshotEntry]:
        """سنابشوت رمز واحد (للفحص/التشخيص بدل سحب السوق كامل)."""
        data = self._get(
            f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        t = data.get("ticker")
        return self._parse_snapshot_entry(t) if isinstance(t, dict) else None

    # ── شموع 5د (الزخم اللحظي) ────────────────────────────────────
    def aggregates(self, ticker: str, multiplier: int, timespan: str,
                   start: str, end: str, limit: int = 50000,
                   adjusted: bool = True) -> list[Bar]:
        """شموع مخصّصة. التواريخ YYYY-MM-DD أو طوابع مللي ثانية (ET)."""
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start}/{end}"
        data = self._get(path, params={
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": limit,
        })
        return [self._parse_bar(b) for b in (data.get("results") or [])]

    def bars_5min(self, ticker: str, start: str, end: str) -> list[Bar]:
        return self.aggregates(ticker, 5, "minute", start, end)

    def bars_1min(self, ticker: str, start: str, end: str) -> list[Bar]:
        return self.aggregates(ticker, 1, "minute", start, end)

    def bars_daily(self, ticker: str, start: str, end: str) -> list[Bar]:
        return self.aggregates(ticker, 1, "day", start, end)

    # ── بيانات يوم تاريخي كامل (للباكتيست) ────────────────────────
    def grouped_daily(self, date: str, adjusted: bool = True) -> list[dict]:
        """كل أسهم السوق ليوم تاريخي (OHLCV) بنداء واحد — مكافئ السنابشوت
        للماضي. يرجّع نتائج خام [{T,o,h,l,c,v,vw,t}]. [] عند الفشل."""
        try:
            data = self._get(
                f"/v2/aggs/grouped/locale/us/market/stocks/{date}",
                params={"adjusted": str(adjusted).lower()})
            return data.get("results") or []
        except MassiveError as exc:
            logger.debug("grouped_daily فشل %s: %s", date, exc)
            return []

    @staticmethod
    def _parse_bar(b: dict[str, Any]) -> Bar:
        return Bar(
            t_ms=int(b.get("t") or 0),
            o=float(b.get("o") or 0.0),
            h=float(b.get("h") or 0.0),
            l=float(b.get("l") or 0.0),
            c=float(b.get("c") or 0.0),
            v=float(b.get("v") or 0.0),
            vw=float(b.get("vw") or 0.0),
            n=int(b.get("n") or 0),
        )

    # ── تفاصيل الورقة + الفلوت ────────────────────────────────────
    def ticker_overview(self, ticker: str) -> dict:
        """تفاصيل الورقة في نداء واحد: type · primary_exchange · الأسهم
        القائمة. يرجّع {} عند الفشل (best-effort)."""
        try:
            data = self._get(f"/v3/reference/tickers/{ticker}")
            return data.get("results") or {}
        except MassiveError as exc:
            logger.debug("ticker overview فشل لـ %s: %s", ticker, exc)
            return {}

    def float_endpoint(self, ticker: str) -> Optional[float]:
        """الفلوت الحر من endpoint vX التجريبي (None عند أي فشل)."""
        try:
            data = self._get("/stocks/vX/float", params={"ticker": ticker})
            results = data.get("results")
            row = results[0] if isinstance(results, list) and results else results
            if isinstance(row, dict) and row.get("free_float"):
                return float(row["free_float"])
        except (MassiveError, TypeError, ValueError) as exc:
            logger.debug("float endpoint فشل لـ %s: %s", ticker, exc)
        return None

    # ── الخبر/المحفّز (إشارة تقوية) ───────────────────────────────
    def latest_news(self, ticker: str, published_gte_utc: str,
                    limit: int = 5,
                    published_lte_utc: Optional[str] = None) -> Optional[Catalyst]:
        """أحدث خبر للسهم بعد طابع زمني UTC (RFC3339). None لو ما فيه.
        published_lte_utc: سقف زمني علوي (للباكتيست: لا أخبار من المستقبل)."""
        # التحليل داخل الحماية: رد مشوّه (شكل مختلف) يُتجاهَل ولا يكسر الدورة.
        try:
            params = {
                "ticker": ticker,
                "published_utc.gte": published_gte_utc,
                "order": "desc",
                "sort": "published_utc",
                "limit": limit,
            }
            if published_lte_utc:
                params["published_utc.lte"] = published_lte_utc
            data = self._get("/v2/reference/news", params=params)
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or not results:
                return None
            top = results[0]
            if not isinstance(top, dict):
                return None
            pub = top.get("publisher") or {}
            return Catalyst(
                has_news=True,
                headline=top.get("title", ""),
                publisher=pub.get("name", "") if isinstance(pub, dict) else str(pub),
                url=top.get("article_url", ""),
                published_utc=top.get("published_utc", ""),
                description=top.get("description", "") or "",
            )
        except (MassiveError, TypeError, ValueError, KeyError,
                AttributeError, IndexError) as exc:
            logger.debug("news فشل لـ %s: %s", ticker, exc)
            return None

    # ── حالة السوق (تشخيص عام، ليست توقّف per-ticker) ─────────────
    def market_status(self) -> dict[str, Any]:
        try:
            return self._get("/v1/marketstatus/now")
        except MassiveError:
            return {}
