"""重启证据核对：落盘基线与审计标定记录对得上才允许接着投料。"""

from __future__ import annotations

import json
import os

import pytest

from glassmelt.app import Platform, build_platform
from glassmelt.batch.scale import BASELINE_KEY
from glassmelt.clock import ManualClock
from glassmelt.config import default_config
from glassmelt.errors import InterlockError


def _restart(platform: Platform, clock: ManualClock) -> Platform:
    """在同一个存储目录上装一台新平台，模拟断电重启。"""

    return build_platform(default_config(), clock=clock, storage_root=platform.store.root)


def _tamper_baseline(platform: Platform, **changes: object) -> None:
    """直接改写落盘的基线文件，模拟断电后留下的半份状态。"""

    path = platform.store.path_for(BASELINE_KEY)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.update(changes)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def test_consistent_evidence_resumes_with_stored_baseline(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    second = _restart(platform, clock)
    verdict = second.resume_verdict()
    assert verdict["ok"] is True
    assert verdict["fresh_start"] is False
    assert verdict["baseline"]["epoch"] == 1
    assert verdict["audited"]["epoch"] == 1
    report = second.start()
    assert report["violations"] == []
    assert second.scale.offset_kg == pytest.approx(4.5)
    assert second.scale.epoch == 1


def test_empty_storage_is_a_fresh_start(platform):
    verdict = platform.resume_verdict()
    assert verdict["ok"] is True
    assert verdict["fresh_start"] is True
    platform.start()
    assert platform.scale.epoch == 1
    assert platform.scale.offset_kg == pytest.approx(0.0)


def test_tampered_baseline_halts_and_lists_findings(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    _tamper_baseline(platform, offset_kg=9.25)
    second = _restart(platform, clock)
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    message = excinfo.value.message
    assert "偏移" in message
    assert "摘要" in message
    # 停在那儿：任何执行机构都没有动作，启动步骤为空。
    assert second.draft.is_running is False
    assert second.startup_report()["steps"] == []
    assert second.status()["started"] is False
    # 不一致清单也能从只读接口看到，且这次拒绝本身已入审计。
    readiness = second.readiness_report()
    findings = readiness["resume_evidence"]["findings"]
    assert any("摘要" in item for item in findings)
    assert any("偏移" in item for item in findings)
    assert any(
        record["action"] == "resume_evidence_checked"
        for record in second.audit.alarms()
    )


def test_expired_baseline_halts(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    clock.advance(platform.config.batch.baseline_max_age_s + 60.0)
    second = _restart(platform, clock)
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "过期" in excinfo.value.message
    assert second.status()["started"] is False


def test_future_dated_baseline_halts(platform, clock, tmp_path):
    platform.scale.recalibrate(4.5, clock.now())
    earlier = ManualClock(start=clock.now() - 3600.0)
    second = build_platform(
        default_config(), clock=earlier, storage_root=platform.store.root
    )
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "时钟" in excinfo.value.message


def test_missing_baseline_with_audit_history_halts(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    assert platform.store.remove(BASELINE_KEY) is True
    second = _restart(platform, clock)
    verdict = second.resume_verdict()
    assert verdict["ok"] is False
    assert verdict["fresh_start"] is False
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "缺失" in excinfo.value.message


def test_baseline_without_audit_record_halts(platform, clock):
    platform.store.write(
        BASELINE_KEY, {"offset_kg": 1.0, "epoch": 1, "calibrated_at": clock.now()}
    )
    second = _restart(platform, clock)
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "审计" in excinfo.value.message


def test_corrupt_audit_journal_halts(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    segment = os.path.join(platform.store.root, "audit", "00000.jsonl")
    with open(segment, "a", encoding="utf-8") as handle:
        handle.write('{"component": "batch", "action": "scale_recalib')
    second = _restart(platform, clock)
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "审计日志" in excinfo.value.message
    assert "损坏" in excinfo.value.message


def test_out_of_range_persisted_offset_halts(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    limit = platform.config.batch.scale_offset_max_kg
    _tamper_baseline(platform, offset_kg=limit + 1.0)
    second = _restart(platform, clock)
    with pytest.raises(InterlockError) as excinfo:
        second.start()
    assert "超出允许范围" in excinfo.value.message


def test_recalibration_clears_the_halt(platform, clock):
    platform.scale.recalibrate(4.5, clock.now())
    _tamper_baseline(platform, offset_kg=9.25)
    second = _restart(platform, clock)
    with pytest.raises(InterlockError):
        second.start()
    # 现场处置：重新标定（留下新的审计记录）后证据重新对上，允许启动。
    # 重启后的平台内存代次从 0 起，现场标定是它自己的第 1 代。
    second.recalibrate_scale(2.0)
    report = second.start()
    assert report["violations"] == []
    assert second.scale.offset_kg == pytest.approx(2.0)
    assert second.scale.epoch == 1
