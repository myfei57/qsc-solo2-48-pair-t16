"""重启恢复：落盘证据（标定基线 + 审计日志）核对一致才允许接着投料。"""

from __future__ import annotations

import pytest

from glassmelt.app import build_platform
from glassmelt.batch.scale import BASELINE_KEY
from glassmelt.cli import main
from glassmelt.config import default_config
from glassmelt.errors import StorageError
from glassmelt.store.jsonfile import JsonStore


def _reboot(clock, root):
    """在同一存储目录上重建平台，模拟断电后的重启。"""

    return build_platform(default_config(), clock=clock, storage_root=root)


def test_fresh_storage_boots_as_initial_calibration(platform):
    report = platform.verify_evidence()
    assert report["ok"] is True
    assert report["fresh"] is True
    assert report["discrepancies"] == []
    platform.start()
    assert platform.scale.epoch == 1
    assert platform.scale.is_durable() is True


def test_consistent_evidence_is_reused_after_reboot(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is True
    assert report["fresh"] is False
    assert report["baseline"]["epoch"] == 1
    assert report["audit_records"] > 0
    second.start()
    assert second.scale.offset_kg == pytest.approx(4.0)
    assert second.scale.epoch == 1
    assert second.startup_report()["violations"] == []


def test_tampered_baseline_file_halts_startup(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    doc = first.store.read(BASELINE_KEY)
    doc["offset_kg"] = 7.5  # 标定落盘后文件被改动
    first.store.write(BASELINE_KEY, doc)
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is False
    assert any("摘要" in item for item in report["discrepancies"])
    with pytest.raises(StorageError) as excinfo:
        second.start()
    assert "摘要" in excinfo.value.message
    assert second.status()["started"] is False


def test_expired_baseline_halts_startup(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    clock.advance(first.config.batch.baseline_max_age_s + 1.0)
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is False
    assert any("过期" in item for item in report["discrepancies"])
    with pytest.raises(StorageError):
        second.start()


def test_truncated_audit_line_halts_startup(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    segment = first.store.root + "/audit/00000.jsonl"
    with open(segment, "a", encoding="utf-8") as handle:
        handle.write('{"record_id": "ab12", "act')  # 断电时写到一半的行
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is False
    assert any("不是完整 JSON" in item for item in report["discrepancies"])
    with pytest.raises(StorageError):
        second.start()


def test_unparseable_baseline_halts_startup(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    with open(first.store.path_for(BASELINE_KEY), "w", encoding="utf-8") as handle:
        handle.write('{"offset_kg": 4.0, "epo')  # 半份基线文件
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is False
    assert report["fresh"] is False
    assert any("无法解析" in item for item in report["discrepancies"])
    with pytest.raises(StorageError):
        second.start()


def test_missing_baseline_is_a_fresh_start(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    first.store.remove(BASELINE_KEY)  # 等效于 reset：清掉基线重新标定
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is True
    assert report["fresh"] is True
    second.start()
    assert second.scale.offset_kg == pytest.approx(0.0)


def test_out_of_range_persisted_offset_halts_startup(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(1.0, clock.now())
    doc = first.store.read(BASELINE_KEY)
    doc["offset_kg"] = first.config.batch.scale_offset_max_kg + 50.0
    first.store.write(BASELINE_KEY, doc)
    second = _reboot(clock, root)
    report = second.verify_evidence()
    assert report["ok"] is False
    assert any("超出允许范围" in item for item in report["discrepancies"])
    with pytest.raises(StorageError):
        second.start()


def test_baseline_without_audit_provenance_halts_startup(platform):
    platform.store.write(
        BASELINE_KEY,
        {"offset_kg": 1.0, "epoch": 1, "calibrated_at": platform.clock.now()},
    )
    report = platform.verify_evidence()
    assert report["ok"] is False
    assert any("没有任何标定记录" in item for item in report["discrepancies"])
    with pytest.raises(StorageError):
        platform.start()


def test_rejected_evidence_is_audited(clock, tmp_path):
    root = str(tmp_path / "state")
    first = _reboot(clock, root)
    first.scale.recalibrate(4.0, clock.now())
    doc = first.store.read(BASELINE_KEY)
    doc["offset_kg"] = 7.5
    first.store.write(BASELINE_KEY, doc)
    second = _reboot(clock, root)
    second.verify_evidence()
    actions = [entry["action"] for entry in second.audit.entries(limit=50)]
    assert "evidence_rejected" in actions
    readiness = second.readiness_report()
    assert readiness["recovery"]["ok"] is False
    assert readiness["recovery"]["discrepancies"]


def test_cli_verify_exit_codes(tmp_path, capsys):
    root = str(tmp_path / "state")
    assert main(["--storage-root", root, "verify"]) == 0
    capsys.readouterr()
    store = JsonStore(root)
    store.write(
        BASELINE_KEY, {"offset_kg": 1.0, "epoch": 1, "calibrated_at": 1_756_000_000.0}
    )
    assert main(["--storage-root", root, "verify"]) == 1
    out = capsys.readouterr().out
    assert "discrepancies" in out
