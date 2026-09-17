"""只追加的事件日志：哈希链 + 写锁，支撑封存不可变与崩溃恢复。"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from .errors import StoreCorrupted

GENESIS = "0" * 64


def canonical(obj) -> str:
    """确定性序列化，供哈希与比对使用。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class EventStore:
    """JSONL 事件日志。

    每条事件携带 seq、prev_hash 与自身 hash；加载时逐条校验，
    任何篡改都会在恢复时暴露。append 在锁内完成，保证并发投票
    时序号与哈希链不断裂。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.events: list[dict] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        prev = GENESIS
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            body = {
                "seq": ev["seq"],
                "type": ev["type"],
                "case_id": ev["case_id"],
                "payload": ev["payload"],
                "prev_hash": ev["prev_hash"],
            }
            if ev["seq"] != len(self.events) + 1 or ev["prev_hash"] != prev or ev.get("hash") != digest(body):
                raise StoreCorrupted(f"事件日志校验失败: seq={ev.get('seq')}")
            self.events.append(ev)
            prev = ev["hash"]

    @property
    def head_hash(self) -> str:
        return self.events[-1]["hash"] if self.events else GENESIS

    def append(self, type_: str, case_id: str, payload: dict) -> dict:
        with self.lock:
            ev = {
                "seq": len(self.events) + 1,
                "type": type_,
                "case_id": case_id,
                "payload": payload,
                "prev_hash": self.head_hash,
            }
            ev["hash"] = digest(ev)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                fh.flush()
            self.events.append(ev)
            return ev
