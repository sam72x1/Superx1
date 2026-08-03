"""قياس توقعات Kronos Shadow مقابل العوائد الفعلية الثابتة زمنيًا."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from .textutil import esc


LEGACY_SESSION = "غير محددة"


def _finite(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _sign(value: float) -> int:
    return 1 if value > 0 else (-1 if value < 0 else 0)


def _session_name(row: Mapping) -> str:
    """اسم الجلسة، مع وعاء صريح للصفوف القديمة بلا session."""
    raw = row.get("session")
    clean = str(raw).strip() if raw is not None else ""
    return clean or LEGACY_SESSION


def _experiment_key(row: Mapping) -> tuple:
    """هوية القياس؛ experiment_id حديث، وتركيب كامل للصفوف القديمة."""
    experiment_id = str(row.get("experiment_id") or "").strip()
    if experiment_id:
        return ("experiment_id", experiment_id)
    try:
        horizons = tuple(sorted(int(value) for value in (row.get("returns") or {})))
    except (TypeError, ValueError):
        horizons = ()
    return (
        "legacy",
        str(row.get("model") or ""),
        str(row.get("model_revision") or ""),
        str(row.get("tokenizer") or ""),
        str(row.get("tokenizer_revision") or ""),
        str(row.get("kronos_revision") or ""),
        str(row.get("service_revision") or ""),
        str(row.get("client_revision") or ""),
        str(row.get("market_timezone") or ""),
        str(row.get("device") or ""),
        row.get("max_context"),
        row.get("lookback"),
        row.get("pred_len"),
        horizons,
    )


def _new_scope() -> dict:
    return {
        "forecasts": 0,
        "ok": 0,
        "errors": 0,
        "skipped": 0,
        "completed": 0,
        "horizons": {},
    }


def _accumulate_scope(scope: dict, row: Mapping) -> None:
    """يجمع sufficient statistics فقط؛ الذاكرة لا تنمو مع عدد التوقعات."""
    scope["forecasts"] += 1
    status = row.get("status")
    if status == "error":
        scope["errors"] += 1
        return
    if status == "skipped":
        scope["skipped"] += 1
        return
    if status != "ok":
        return
    scope["ok"] += 1
    if row.get("completed_at"):
        scope["completed"] += 1

    predicted = row.get("returns") or {}
    actuals = row.get("actuals") or {}
    trade_date = str(row.get("trade_date") or "").strip()
    for horizon, predicted_value in predicted.items():
        try:
            horizon_i = int(horizon)
        except (TypeError, ValueError, OverflowError):
            continue
        pred = _finite(predicted_value)
        actual = _finite(actuals.get(horizon_i, actuals.get(str(horizon_i))))
        if horizon_i <= 0 or pred is None or actual is None:
            continue
        stats = scope["horizons"].setdefault(horizon_i, {
            "samples": 0,
            "trading_days": set(),
            "direction_hits": 0,
            "baseline_hits": 0,
            "absolute_error_sum": 0.0,
            "predicted_sum": 0.0,
            "actual_sum": 0.0,
        })
        stats["samples"] += 1
        if trade_date:
            stats["trading_days"].add(trade_date)
        stats["direction_hits"] += int(_sign(pred) == _sign(actual))
        stats["baseline_hits"] += int(_sign(actual) == 1)
        stats["absolute_error_sum"] += abs(pred - actual)
        stats["predicted_sum"] += pred
        stats["actual_sum"] += actual


def _finish_scope(scope: dict | None) -> dict:
    if scope is None:
        scope = _new_scope()
    horizons: dict[int, dict] = {}
    for horizon, raw in sorted(scope["horizons"].items()):
        samples = raw["samples"]
        accuracy = raw["direction_hits"] / samples * 100.0
        baseline_accuracy = raw["baseline_hits"] / samples * 100.0
        horizons[horizon] = {
            "samples": samples,
            "trading_days": len(raw["trading_days"]),
            "direction_hits": raw["direction_hits"],
            "directional_accuracy_pct": accuracy,
            "baseline_hits": raw["baseline_hits"],
            "baseline_accuracy_pct": baseline_accuracy,
            "directional_lift_pp": accuracy - baseline_accuracy,
            "mae_pct_points": raw["absolute_error_sum"] / samples,
            "mean_predicted_pct": raw["predicted_sum"] / samples,
            "mean_actual_pct": raw["actual_sum"] / samples,
        }
    return {
        "forecasts": scope["forecasts"],
        "ok": scope["ok"],
        "errors": scope["errors"],
        "skipped": scope["skipped"],
        "completed": scope["completed"],
        "horizons": horizons,
    }


def _created_key(row: Mapping, position: int) -> tuple[int, float, int, int]:
    try:
        created = datetime.fromisoformat(str(row.get("created_at") or ""))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        timestamp = created.timestamp()
        valid = 1
    except (OSError, OverflowError, TypeError, ValueError):
        timestamp = float("-inf")
        valid = 0
    try:
        rowid = int(row.get("_rowid") or 0)
    except (TypeError, ValueError, OverflowError):
        rowid = 0
    return valid, timestamp, rowid, -position


def summarize_kronos(rows: Iterable[Mapping]) -> dict:
    """تلخيص streaming من لقطة واحدة مرتبة من الأحدث إلى الأقدم.

    لا يحتفظ بالصفوف أو أزواج الآفاق في الذاكرة؛ فقط sufficient statistics
    لكل تجربة موجودة في نافذة القياس. اختيار التجربة النشطة والحالة التشغيلية
    من نفس الـiterator يمنع خلط لقطتين إذا وصلت كتابة أثناء إنشاء التقرير.
    """
    active_value: Mapping | None = None
    active_key: tuple | None = None
    scopes: dict[tuple, dict] = {}
    session_scopes: dict[tuple, dict[str, dict]] = {}
    total_forecasts = total_ok = total_errors = total_skipped = 0
    revisions: list[str] = []
    operational_best: tuple[tuple[int, float, int, int], Mapping] | None = None

    for position, row in enumerate(rows):
        total_forecasts += 1
        status = row.get("status")
        total_ok += int(status == "ok")
        total_errors += int(status == "error")
        total_skipped += int(status == "skipped")
        revision = str(row.get("model_revision") or "").strip()
        if status == "ok" and revision and revision not in revisions:
            revisions.append(revision)
        if active_value is None and status == "ok" and revision:
            active_value = row
            active_key = _experiment_key(row)

        key = _experiment_key(row)
        session = _session_name(row)
        _accumulate_scope(scopes.setdefault(key, _new_scope()), row)
        per_session = session_scopes.setdefault(key, {})
        _accumulate_scope(per_session.setdefault(session, _new_scope()), row)

        candidate = (_created_key(row, position), row)
        if operational_best is None or candidate[0] > operational_best[0]:
            operational_best = candidate

    active_scope = scopes.get(active_key) if active_key is not None else None
    active_session_scopes = (
        session_scopes.get(active_key, {}) if active_key is not None else {}
    )
    active = _finish_scope(active_scope)
    sessions = {
        session: _finish_scope(scope)
        for session, scope in active_session_scopes.items()
    }
    # توافق للمتعاملين القدماء: horizons متاح فقط عندما لا يوجد احتمال خلط
    # جلسات. عند تعددها يجب استعمال sessions صراحةً.
    lone_horizons = (
        next(iter(sessions.values()))["horizons"] if len(sessions) == 1 else {}
    )

    operational_row = operational_best[1] if operational_best is not None else None
    active_revision = (
        str(active_value.get("model_revision") or "").strip()
        if isinstance(active_value, Mapping) else ""
    )
    return {
        "total_forecasts": total_forecasts,
        "total_ok": total_ok,
        "total_errors": total_errors,
        "total_skipped": total_skipped,
        "latest_status": (
            str(operational_row.get("status") or "")
            if operational_row is not None else ""
        ),
        "latest_error": (
            str(operational_row.get("error") or "")
            if operational_row is not None else ""
        ),
        "latest_created_at": (
            str(operational_row.get("created_at") or "")
            if operational_row is not None else ""
        ),
        "forecasts": active["forecasts"],
        "ok": active["ok"],
        "errors": active["errors"],
        "skipped": active["skipped"],
        "completed": active["completed"],
        "active_experiment_id": (
            str(active_value.get("experiment_id") or "").strip()
            if isinstance(active_value, Mapping) else ""
        ),
        "active_model_revision": active_revision,
        "active_tokenizer_revision": (
            str(active_value.get("tokenizer_revision") or "").strip()
            if isinstance(active_value, Mapping) else ""
        ),
        "active_kronos_revision": (
            str(active_value.get("kronos_revision") or "").strip()
            if isinstance(active_value, Mapping) else ""
        ),
        "model_revisions": revisions,
        "sessions": sessions,
        "horizons": lone_horizons,
    }


def iter_flatten_kronos_rows(rows: Iterable[Mapping]) -> Iterable[dict]:
    """يبث صف CSV لكل توقع/أفق بلا مضاعفة نافذة القياس في الذاكرة."""
    for row in rows:
        predicted = row.get("returns") or {}
        actuals = row.get("actuals") or {}
        actual_observed_at = row.get("actual_observed_at") or {}
        horizons: list[int | None]
        try:
            horizons = sorted({int(value) for value in (*predicted, *actuals)})
        except (TypeError, ValueError):
            horizons = []
        if not horizons:
            horizons = [None]  # احتفظ بأخطاء الخدمة حتى لو لم تحمل توقعًا.
        for horizon in horizons:
            pred = (_finite(predicted.get(horizon, predicted.get(str(horizon))))
                    if horizon is not None else None)
            actual = (_finite(actuals.get(horizon, actuals.get(str(horizon))))
                      if horizon is not None else None)
            yield {
                "ticker": row.get("ticker"),
                "trade_date": row.get("trade_date"),
                "asof_at": row.get("asof_at"),
                "status": row.get("status"),
                "model": row.get("model"),
                "experiment_id": row.get("experiment_id"),
                "model_revision": row.get("model_revision"),
                "tokenizer": row.get("tokenizer"),
                "tokenizer_revision": row.get("tokenizer_revision"),
                "kronos_revision": row.get("kronos_revision"),
                "service_revision": row.get("service_revision"),
                "client_revision": row.get("client_revision"),
                "market_timezone": row.get("market_timezone"),
                "device": row.get("device"),
                "max_context": row.get("max_context"),
                "observation_grace_min": row.get("observation_grace_min"),
                "session": row.get("session"),
                "lookback": row.get("lookback"),
                "pred_len": row.get("pred_len"),
                "horizon_bars": horizon,
                "horizon_minutes": horizon * 5 if horizon is not None else None,
                "predicted_return_pct": pred,
                "actual_return_pct": actual,
                "actual_observed_at": (
                    actual_observed_at.get(
                        horizon, actual_observed_at.get(str(horizon))
                    ) if horizon is not None else None
                ),
                "direction_hit": (
                    int(_sign(pred) == _sign(actual))
                    if pred is not None and actual is not None else None
                ),
                "always_up_baseline_hit": (
                    int(_sign(actual) == 1) if actual is not None else None
                ),
                "absolute_error_pct_points": (
                    abs(pred - actual)
                    if pred is not None and actual is not None else None
                ),
                "base_close": row.get("base_close"),
                "round_trip_latency_ms": row.get("latency_ms"),
                "service_latency_ms": row.get("service_latency_ms"),
                "error": row.get("error"),
                "requested_at": row.get("requested_at"),
                "received_at": row.get("received_at"),
                "created_at": row.get("created_at"),
                "completed_at": row.get("completed_at"),
            }


def flatten_kronos_rows(rows: Iterable[Mapping]) -> list[dict]:
    """واجهة list صغيرة متوافقة؛ مسار التصدير الحي يستخدم iterator مباشرة."""
    return list(iter_flatten_kronos_rows(rows))


def _append_horizon_lines(
    lines: list[str],
    horizons: Mapping[int, Mapping],
    min_sample: int,
    min_trading_days: int,
) -> None:
    """إضافة أسطر آفاق جلسة واحدة إلى التقرير."""
    if not horizons:
        lines.append("⏳ لا توجد نتائج فعلية صالحة بعد؛ ننتظر اكتمال الآفاق.")
        return
    for horizon, stats in horizons.items():
        minutes = horizon * 5
        lines.append(
            f"• {minutes}د: اتجاه {stats['directional_accuracy_pct']:.1f}% "
            f"({stats['direction_hits']}/{stats['samples']}) · "
            f"خط أساس الصعود {stats['baseline_accuracy_pct']:.1f}% · "
            f"lift {stats['directional_lift_pp']:+.1f} نقطة · "
            f"MAE {stats['mae_pct_points']:.2f} نقطة% · "
            f"أيام {stats['trading_days']}"
        )
        if stats["samples"] < min_sample:
            lines.append(
                f"  ↳ عيّنة {stats['samples']} أقل من {min_sample} — لا حكم بعد."
            )
        if stats["trading_days"] < min_trading_days:
            lines.append(
                f"  ↳ أيام قياس هذا الأفق {stats['trading_days']} أقل من "
                f"{min_trading_days} — لا ترقية بعد."
            )


def _runtime_stats_line(runtime_stats: Mapping | None) -> str:
    if runtime_stats is None:
        return ""

    def count(key: str) -> int:
        try:
            return max(0, int(runtime_stats.get(key, 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    return (
        "عامل هذه العملية: أُدرج "
        f"{count('enqueued')} · امتلاء الطابور {count('queue_full')} · "
        f"قديم قبل الجلب {count('stale_before_fetch')} · "
        f"فشل حفظ {count('save_failed')} · "
        f"العمق {count('queue_depth')}/{count('queue_capacity')}"
    )


def _runtime_stat(runtime_stats: Mapping | None, key: str) -> int:
    if runtime_stats is None:
        return 0
    try:
        return max(0, int(runtime_stats.get(key, 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def format_kronos_report(
    store, *, min_sample: int = 200, min_trading_days: int = 20,
    runtime_stats: Mapping | None = None, evaluation_days: int = 30,
) -> str:
    """يبني تقرير HTML موجزًا لأمر تيليجرام ``/kronos``."""
    if getattr(store, "kronos_available", True) is False:
        error = str(getattr(store, "kronos_error", "") or "")
        detail = f" — {esc(error[:300])}" if error else ""
        return (
            "🧪 <b>Kronos Shadow</b>\n"
            f"⚠️ مخزن القياس غير متاح؛ Superx1 الأساسي مستمر{detail}"
        )
    runtime_line = _runtime_stats_line(runtime_stats)
    window_fetch = getattr(store, "fetch_kronos_evaluation_window", None)
    safe_evaluation_days = max(1, min(int(evaluation_days), 366))
    if callable(window_fetch):
        rows = window_fetch(trading_days=safe_evaluation_days)
        scope_label = f"نافذة آخر {safe_evaluation_days} يوم تداول مسجّل"
    else:
        rows = store.fetch_kronos_forecasts(limit=10_000)
        scope_label = "آخر 10,000 سجل"
    # التجربة النشطة وأحدث حالة تُستنتجان من النافذة نفسها. الاستعلامات
    # المنفصلة هنا قد ترى snapshot أقدم من generator عند وصول كتابة متزامنة.
    summary = summarize_kronos(rows)
    if not summary["total_forecasts"]:
        text = (
            "🧪 <b>Kronos Shadow</b>\n"
            "لا توجد توقعات بعد. الوضع قياس فقط ولا يغيّر قرارات التنبيه."
        )
        if runtime_line:
            text += f"\n{runtime_line}"
        if _runtime_stat(runtime_stats, "queue_full"):
            text += "\n⚠️ امتلاء الطابور يعني أن عينة Shadow فقدت بعض المرشحين."
        if _runtime_stat(runtime_stats, "save_failed"):
            text += "\n⚠️ فشل حفظ Shadow؛ هذه العملية لا تملك coverage كاملًا."
        return text
    lines = [
        "🧪 <b>Kronos Shadow — قياس مغلق الحلقة</b>",
        f"سجلات القياس ({scope_label}): {summary['total_forecasts']} · "
        f"ناجحة: {summary['total_ok']} · أخطاء: {summary['total_errors']} · "
        f"متخطاة: {summary['total_skipped']}",
    ]
    if runtime_line:
        lines.append(runtime_line)
        if _runtime_stat(runtime_stats, "queue_full"):
            lines.append(
                "⚠️ امتلاء الطابور يعني أن عينة Shadow فقدت بعض المرشحين."
            )
        if _runtime_stat(runtime_stats, "save_failed"):
            lines.append(
                "⚠️ فشل حفظ Shadow؛ لا تعتمد coverage هذه العملية للترقية."
            )
    if summary["latest_status"] != "ok":
        status_labels = {"error": "خطأ", "skipped": "تخطٍّ"}
        latest_label = status_labels.get(
            summary["latest_status"], summary["latest_status"] or "غير معروفة"
        )
        latest_line = f"⚠️ أحدث سجل تشغيلي: {esc(latest_label)}"
        if summary["latest_error"]:
            latest_line += f" — {esc(summary['latest_error'][:300])}"
        lines.append(latest_line)
    if not summary["active_model_revision"]:
        lines.append(
            "لا توجد توقعات ناجحة بنسخة نموذج مثبتة بعد. "
            "الوضع قياس فقط ولا يغيّر قرارات التنبيه."
        )
        return "\n".join(lines)

    lines.extend([
        f"نسخة النموذج: <code>{esc(summary['active_model_revision'])}</code>",
        f"توقعات النسخة النشطة: {summary['forecasts']} · "
        f"ناجحة: {summary['ok']} · أخطاء: {summary['errors']} · "
        f"متخطاة: {summary['skipped']} · "
        f"مكتملة القياس: {summary['completed']}",
    ])
    if summary["active_experiment_id"]:
        lines.insert(
            2,
            "التجربة: <code>"
            f"{esc(summary['active_experiment_id'][:16])}</code>",
        )
    show_session_headers = (
        len(summary["sessions"]) > 1
        or any(session != LEGACY_SESSION for session in summary["sessions"])
    )
    for session, session_summary in summary["sessions"].items():
        if show_session_headers:
            lines.append(
                f"\n<b>الجلسة: {esc(session)}</b> · "
                f"توقعات: {session_summary['forecasts']} · "
                f"مكتملة: {session_summary['completed']}"
            )
        _append_horizon_lines(
            lines,
            session_summary["horizons"],
            min_sample,
            min_trading_days,
        )
    lines.append(
        "⚠️ الـlift مقابل «صعود دائم» أهم من الرقم الخام؛ ولا يساوي عائدًا بعد "
        "التكاليف. Shadow لا ينفّذ ولا يفلتر."
    )
    lines.append(
        "⚠️ الحقيقة الحالية أول lastTrade صالح قرب موعد الأفق، وليست إغلاق "
        "aggregate 5د رسميًا؛ يلزم backfill للإغلاقات قبل أي ترقية A/B."
    )
    return "\n".join(lines)
