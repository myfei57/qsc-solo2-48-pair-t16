"""重启时的落盘证据核对。

断电重启之后，磁盘上同时躺着投料秤标定基线与审计日志：两份证据必须
互相对得上，产线才允许接着投料。本模块在启动前把落盘的几样证据逐一
核对——基线文件能解析、取值在量程内、标定没过期、审计日志没有半截
记录、日志里最后一笔标定与基线文件是同一版。全部一致才把状态交还给
控制链；任何一项对不上就把不一致逐条列清，停在原地，不带着半份状态
继续跑。核对只读不改：结论怎么执行由 ``Platform`` 决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..batch.scale import BASELINE_KEY
from ..config import BatchLimits
from ..errors import StorageError
from ..eventbus import Event, EventBus
from .journal import JsonlJournal
from .jsonfile import JsonStore

#: 标定时间戳允许的未来偏差（秒），容忍正常的时钟抖动。
CLOCK_SKEW_TOLERANCE_S = 60.0


@dataclass(frozen=True)
class RecoveryReport:
    """一次落盘证据核对的结论。"""

    ok: bool
    fresh: bool
    checks: tuple[str, ...]
    discrepancies: tuple[str, ...]
    baseline: dict | None
    audit_records: int
    verified_at: float

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fresh": self.fresh,
            "checks": list(self.checks),
            "discrepancies": list(self.discrepancies),
            "baseline": self.baseline,
            "audit_records": self.audit_records,
            "verified_at": round(self.verified_at, 3),
        }


class EvidenceRecovery:
    """核对标定基线与审计日志，给出"接着用／重新标定／停住"的依据。"""

    def __init__(
        self,
        store: JsonStore,
        journal: JsonlJournal,
        limits: BatchLimits,
        bus: EventBus,
    ) -> None:
        self._store = store
        self._journal = journal
        self._limits = limits
        self._bus = bus

    def verify(self, now: float) -> RecoveryReport:
        """核对全部落盘证据，返回结论报告；本身不改动任何运行状态。"""

        moment = float(now)
        checks: list[str] = []
        discrepancies: list[str] = []

        checks.append("audit_journal_integrity")
        journal_problems = self._journal.verify_integrity()
        discrepancies.extend(journal_problems)
        records = [] if journal_problems else self._journal.read_all()

        baseline = self._read_baseline(checks, discrepancies)
        if baseline is not None:
            self._check_baseline(baseline, moment, checks, discrepancies)
            self._check_against_audit(
                baseline, records, not journal_problems, checks, discrepancies
            )

        report = RecoveryReport(
            ok=not discrepancies,
            fresh=baseline is None and not discrepancies,
            checks=tuple(checks),
            discrepancies=tuple(discrepancies),
            baseline=baseline,
            audit_records=len(records),
            verified_at=moment,
        )
        self._bus.publish(
            Event.of(
                "store",
                "store",
                "evidence_verified" if report.ok else "evidence_rejected",
                subject="evidence_recovery",
                severity="info" if report.ok else "warning",
                fresh=report.fresh,
                discrepancies=list(report.discrepancies),
            )
        )
        return report

    def _read_baseline(self, checks: list[str], discrepancies: list[str]) -> dict | None:
        """读出落盘基线；文件缺失返回 ``None``，读不出内容记一条不一致。"""

        checks.append("baseline_document")
        if not self._store.exists(BASELINE_KEY):
            return None
        try:
            return self._store.read(BASELINE_KEY)
        except StorageError as exc:
            discrepancies.append(f"标定基线文件无法解析（疑似断电留下半份文件）: {exc.message}")
            return None

    def _check_baseline(
        self,
        baseline: dict,
        now: float,
        checks: list[str],
        discrepancies: list[str],
    ) -> None:
        """核对基线取值本身：量程、世代号与标定有效期。"""

        checks.append("baseline_limits")
        offset = baseline.get("offset_kg")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            discrepancies.append("标定基线的 offset_kg 缺失或不是数值")
        elif not self._limits.offset_ok(float(offset)):
            discrepancies.append(
                f"落盘的标定偏移 {float(offset):.3f} kg 超出允许范围"
                f" ±{self._limits.scale_offset_max_kg:.3f} kg（文件被改动过？）"
            )
        epoch = baseline.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            discrepancies.append(f"标定基线的 epoch 非法: {epoch!r}")

        checks.append("baseline_freshness")
        calibrated_at = baseline.get("calibrated_at")
        if isinstance(calibrated_at, bool) or not isinstance(calibrated_at, (int, float)):
            discrepancies.append("标定基线的 calibrated_at 缺失或不是数值")
            return
        stamp = float(calibrated_at)
        if stamp > now + CLOCK_SKEW_TOLERANCE_S:
            discrepancies.append(
                f"标定时间 {stamp:.3f} 晚于当前时钟 {now:.3f}（时钟回退或文件来自别处）"
            )
            return
        age = now - stamp
        if age > self._limits.baseline_max_age_s:
            discrepancies.append(
                f"标定基线已过期：距今 {age:.0f} s，"
                f"超过有效期 {self._limits.baseline_max_age_s:.0f} s，需要重新标定"
            )

    def _check_against_audit(
        self,
        baseline: dict,
        records: list[dict],
        journal_ok: bool,
        checks: list[str],
        discrepancies: list[str],
    ) -> None:
        """把基线文件与审计日志里最后一笔标定对账。"""

        checks.append("baseline_audit_crosscheck")
        if not journal_ok:
            return  # 日志自身已不可信，交叉核对得不出结论
        recalibrations = [
            record for record in records if record.get("action") == "scale_recalibrated"
        ]
        if not recalibrations:
            discrepancies.append(
                "基线文件在盘上，但审计日志里没有任何标定记录（只剩半份状态）"
            )
            return
        detail = recalibrations[-1].get("detail")
        if not isinstance(detail, dict):
            discrepancies.append("审计里最后一笔标定记录缺少明细，无法对账")
            return
        if detail.get("epoch") != baseline.get("epoch"):
            discrepancies.append(
                f"审计最后一笔标定是 epoch {detail.get('epoch')}，"
                f"基线文件却是 epoch {baseline.get('epoch')}"
            )
        digest = self._store.digest(BASELINE_KEY)[:12]
        if detail.get("digest") != digest:
            discrepancies.append(
                "基线文件摘要与审计记录不符（文件在标定落盘后被改动）"
            )
