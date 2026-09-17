"""追加式事件日志：所有状态变更先落盘再应用，恢复时重放得到同一状态。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    """JSONL 追加日志；每行一个事件，带单调序号。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._count_existing()

    def _count_existing(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def append(self, event_type: str, case_id: str, at: str, payload: dict[str, Any]) -> dict:
        self._seq += 1
        event = {
            "seq": self._seq,
            "type": event_type,
            "case_id": case_id,
            "at": at,
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        return event

    def read_all(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
