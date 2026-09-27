"""重启证据核对。

断电重启之后，落盘的两样证据——投料秤标定基线文档与审计日志——必须先
互相对一遍才允许接着投料：基线文件自身要完整合法、标定要在有效期内，
而且文件内容必须与审计日志里最后一笔标定记录（代次、偏移、内容摘要）
逐条吻合。三样对得上，重启后按这份基线恢复；任何一样对不上或已过期，
就把全部不一致列出来并拒绝启动，绝不带着半份状态继续跑。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import BatchLimits
from ..errors import GlassMeltError
from ..store.journal import JsonlJournal
from ..store.jsonfile import JsonStore
from .scale import BASELINE_KEY

#: 审计日志里标定事件的动作名（与 ``BatchScale.recalibrate`` 发布的一致）。
RECALIBRATE_ACTION = "scale_recalibrated"

#: 基线时间戳允许的未来偏差（秒），超过即认为时钟回退或文件被换。
FUTURE_TOLERANCE_S = 1.0


@dataclass(frozen=True)
class ResumeVerdict:
    """一次重启证据核对的结论。"""

    findings: tuple[str, ...] = ()
    fresh_start: bool = False
    baseline_present: bool = False
    baseline: dict | None = None
    audited: dict | None = None

    @property
    def ok(self) -> bool:
        """全部证据对得上（或没有任何证据、走首次标定）。"""

        return not self.findings

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fresh_start": self.fresh_start,
            "baseline_present": self.baseline_present,
            "findings": list(self.findings),
            "baseline": self.baseline,
            "audited": self.audited,
        }


def _baseline_findings(baseline: dict, limits: BatchLimits, now: float) -> list[str]:
    """核对基线文档自身：字段齐全、偏移在限值内、标定未过期。"""

    findings: list[str] = []
    offset = baseline.get("offset_kg")
    epoch = baseline.get("epoch")
    calibrated_at = baseline.get("calibrated_at")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        findings.append("基线缺少有效的 offset_kg 字段")
    elif not limits.offset_ok(float(offset)):
        findings.append(
            f"基线偏移 {float(offset):.3f} kg 超出允许范围 "
            f"±{limits.scale_offset_max_kg:.3f} kg"
        )
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        findings.append("基线缺少有效的 epoch 字段")
    if isinstance(calibrated_at, bool) or not isinstance(calibrated_at, (int, float)):
        findings.append("基线缺少有效的 calibrated_at 字段")
    else:
        age_s = float(now) - float(calibrated_at)
        if age_s < -FUTURE_TOLERANCE_S:
            findings.append(
                f"基线标定时间比当前时钟晚 {-age_s:.1f} s，时钟可能回退或文件被换"
            )
        elif age_s > limits.baseline_max_age_s:
            findings.append(
                f"基线已过期：标定距今 {age_s:.0f} s，"
                f"超过有效期 {limits.baseline_max_age_s:.0f} s"
            )
    return findings


def _cross_findings(
    baseline: dict, audited: dict, digest_prefix: str
) -> list[str]:
    """把基线文档与审计里最后一笔标定记录逐字段对一遍。"""

    findings: list[str] = []
    if baseline.get("epoch") != audited.get("epoch"):
        findings.append(
            f"基线代次 {baseline.get('epoch')} 与审计记录代次 "
            f"{audited.get('epoch')} 不一致"
        )
    if baseline.get("offset_kg") != audited.get("offset_kg"):
        findings.append(
            f"基线偏移 {baseline.get('offset_kg')} kg 与审计记录偏移 "
            f"{audited.get('offset_kg')} kg 不一致"
        )
    recorded_digest = audited.get("digest")
    if not recorded_digest:
        findings.append("审计里的标定记录缺少基线内容摘要")
    elif digest_prefix != str(recorded_digest):
        findings.append(
            f"基线内容摘要 {digest_prefix} 与审计记录摘要 {recorded_digest} 不一致"
        )
    return findings


def verify_resume_evidence(
    store: JsonStore, journal: JsonlJournal, limits: BatchLimits, now: float
) -> ResumeVerdict:
    """核对落盘证据，返回结论；``findings`` 为空才允许接着投料。

    基线文件与审计标定记录都不存在时视为首次启动（``fresh_start``），由
    调用方走初始标定；只缺一样、一样损坏或内容互相矛盾时，全部不一致都会
    列进 ``findings``。
    """

    findings: list[str] = []
    baseline: dict | None = None
    baseline_present = store.exists(BASELINE_KEY)
    if baseline_present:
        try:
            baseline = store.read(BASELINE_KEY)
        except GlassMeltError as exc:
            findings.append(f"基线文件无法读取：{exc.message}")
        else:
            findings.extend(_baseline_findings(baseline or {}, limits, now))

    audited: dict | None = None
    journal_readable = True
    try:
        records = journal.read_all()
    except GlassMeltError as exc:
        records = []
        journal_readable = False
        findings.append(f"审计日志无法核对：{exc.message}")
    recalibrations = [
        record
        for record in records
        if record.get("component") == "batch"
        and record.get("action") == RECALIBRATE_ACTION
    ]
    if recalibrations:
        last = recalibrations[-1]
        detail = last.get("detail") or {}
        audited = {
            "epoch": detail.get("epoch"),
            "offset_kg": detail.get("offset_kg"),
            "digest": detail.get("digest"),
            "ts": last.get("ts"),
        }

    if baseline is not None and audited is not None:
        findings.extend(
            _cross_findings(baseline, audited, store.digest(BASELINE_KEY)[:12])
        )
    elif baseline is not None and audited is None and journal_readable:
        findings.append("审计日志里没有对应的标定记录，基线来源无法核对")
    elif baseline is None and baseline_present and audited is not None:
        findings.append("基线文件损坏，无法与审计里的标定记录核对")

    if baseline is None and not baseline_present and audited is not None:
        findings.append(
            f"基线文件缺失，但审计日志里有标定记录（最后代次 {audited.get('epoch')}），"
            "拒绝按未知偏移投料"
        )

    fresh_start = (
        baseline is None
        and not baseline_present
        and audited is None
        and journal_readable
    )
    if fresh_start:
        findings = []

    return ResumeVerdict(
        findings=tuple(findings),
        fresh_start=fresh_start,
        baseline_present=baseline_present,
        baseline=baseline,
        audited=audited,
    )
