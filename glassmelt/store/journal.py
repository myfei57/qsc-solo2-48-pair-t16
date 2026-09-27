"""追加型 JSONL 日志，带段切换。

审计与告警按时间顺序追加，读取方必须能连续读回整段历史。段达到容量阈值
后新建段，旧段不再追加，读取时按段序拼接，保证顺序与写入顺序一致。
"""

from __future__ import annotations

import json
import os
import threading

from ..errors import StorageError


class JsonlJournal:
    """按段追加的 JSONL 日志。"""

    def __init__(self, root: str, name: str, segment_limit: int = 512) -> None:
        if segment_limit < 1:
            raise StorageError("段容量必须为正", component="store")
        self._root = os.path.abspath(os.path.join(root, name))
        self._segment_limit = int(segment_limit)
        self._counts: dict[int, int] = {}
        self._lock = threading.RLock()
        os.makedirs(self._root, exist_ok=True)
        self._index_existing()

    def _index_existing(self) -> None:
        with self._lock:
            for entry in os.listdir(self._root):
                if not entry.endswith(".jsonl"):
                    continue
                try:
                    index = int(entry.split(".")[0])
                except ValueError:
                    continue
                with open(os.path.join(self._root, entry), "r", encoding="utf-8") as handle:
                    self._counts[index] = sum(1 for _ in handle)

    def _active_segment(self) -> int:
        if not self._counts:
            self._counts[0] = 0
            return 0
        index = max(self._counts)
        if self._counts[index] >= self._segment_limit:
            index += 1
            self._counts.setdefault(index, 0)
        return index

    def append(self, record: dict) -> int:
        """追加一条记录，返回它落到的段号。"""

        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            index = self._active_segment()
            target = os.path.join(self._root, f"{index:05d}.jsonl")
            try:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:  # pragma: no cover - 依赖真实磁盘故障
                raise StorageError(f"追加审计日志失败: {exc}", component="store") from exc
            self._counts[index] = self._counts.get(index, 0) + 1
            return index

    def verify_integrity(self) -> list[str]:
        """逐段逐行核对落盘日志，返回问题清单（空列表表示完整可读）。

        断电可能在最后一行写到一半时发生：段索引仍会把它算作一行，但重启
        读回时 ``json.loads`` 会失败。这里只核对、不修复，结论交给重启
        恢复流程决定能不能接着用。
        """

        problems: list[str] = []
        with self._lock:
            indexes = sorted(self._counts)
        if indexes:
            expected = set(range(indexes[0], indexes[-1] + 1))
            for missing in sorted(expected.difference(indexes)):
                problems.append(f"审计段 {missing:05d} 缺失，落盘日志不连续")
        for index in indexes:
            path = os.path.join(self._root, f"{index:05d}.jsonl")
            if not os.path.isfile(path):
                continue  # 活动段已计数但尚未落过任何一行，属于正常情况
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except OSError as exc:
                problems.append(f"审计段 {index:05d} 无法读取: {exc}")
                continue
            for lineno, raw in enumerate(lines, start=1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    problems.append(
                        f"审计段 {index:05d} 第 {lineno} 行不是完整 JSON"
                        f"（疑似断电写了一半）: {exc}"
                    )
                    continue
                if not isinstance(parsed, dict):
                    problems.append(f"审计段 {index:05d} 第 {lineno} 行不是 JSON 对象")
        return problems

    def segment_count(self) -> int:
        with self._lock:
            return len(self._counts)

    def read_all(self) -> list[dict]:
        """按段号顺序读回全部记录。"""

        with self._lock:
            indexes = sorted(self._counts)
        records: list[dict] = []
        for index in indexes:
            records.extend(self.read_segment(index))
        return records

    def read_segment(self, index: int) -> list[dict]:
        """读回指定段。"""

        path = os.path.join(self._root, f"{index:05d}.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
