"""دمج الركيزتين → الدرجة النهائية /100 (القسم 5).

القواعد الحاسمة:
- الجاهزية الفنية ≥ 60/100 وإلا رفض (قرار المستخدم الصريح).
- الزخم لازم فوق حدّ أدنى (momentum_min_floor) وإلا رفض.
- الدرجة النهائية = زخم(/50) + جاهزية(/50) + تقوية الخبر، بسقف 100.
- الخبر إشارة تقوية لا بوّابة.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalyst import catalyst_bonus
from .config import Config
from .models import Candidate


@dataclass
class ScoreResult:
    final_score: float
    passed: bool
    reason: str = ""
    reason_code: str = ""   # كود ثابت للرفض (DEBT-13) — للتصنيف الآلي لا العرض


def score_candidate(cfg: Config, c: Candidate) -> ScoreResult:
    """يحسب الدرجة النهائية ويطبّق شروط القبول. يحدّث c.final_score."""
    if c.momentum is None or c.readiness is None:
        return ScoreResult(0.0, False, "تحليل ناقص", "incomplete")

    # ── شرط الجاهزية الفنية (≥60) ─────────────────────────────────
    if c.readiness.classic_score < cfg.tech_readiness_min:
        return ScoreResult(
            0.0, False,
            f"جاهزية فنية {c.readiness.classic_score:.0f} < "
            f"{cfg.tech_readiness_min:.0f} (غير جاهز فنيًا)",
            "readiness",
        )

    # ── شرط الزخم الأدنى ──────────────────────────────────────────
    if c.momentum.score < cfg.momentum_min_floor:
        return ScoreResult(
            0.0, False,
            f"زخم {c.momentum.score:.0f} < {cfg.momentum_min_floor:.0f} "
            f"(زخم ضعيف رغم +{cfg.trigger_change_pct:g}%)",
            "momentum",
        )

    # ── الدمج ─────────────────────────────────────────────────────
    base = c.momentum.score + c.readiness.pillar_score
    bonus = catalyst_bonus(cfg, c.catalyst)
    final = min(100.0, base + bonus)

    # خصم لو الفلوت مجهول (أولوية أدنى، لا رفض — best-effort)
    if c.float_shares is None:
        final = max(0.0, final - 5.0)

    c.final_score = round(final, 1)

    if final < cfg.alert_score_min:
        return ScoreResult(
            c.final_score, False,
            f"درجة {c.final_score:.0f} < عتبة التنبيه {cfg.alert_score_min:.0f}",
            "score",
        )

    return ScoreResult(c.final_score, True)
