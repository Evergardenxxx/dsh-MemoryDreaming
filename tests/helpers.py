"""测试公共脚手架。

为什么要有这一层？
每个测试都需要「临时数据库 + 固定时钟 + 默认作用域」。
把这些重复代码收在基类里，测试用例本身就能一眼看清在验证什么。

固定时钟（FixedClock）是这套测试能成立的关键：
如果没有它，「三天后还能不能查到旧事」这种用例就得真的等三天。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorydreaming.clock import FixedClock, load_timezone
from memorydreaming.config import Settings
from memorydreaming.models import (
    MemoryItem,
    MemoryType,
    Scope,
    Turn,
    new_id,
)
from memorydreaming.runtime import MemoryRuntime

# 所有测试共用的「现在」：2026-10-20 20:00（Asia/Shanghai）
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class RuntimeTestCase(unittest.TestCase):
    """提供 runtime / repos / scope / clock 的基类。"""

    def setUp(self) -> None:
        # 临时目录，测试结束自动清理，不会污染项目里的 .memorydreaming
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.db"

        self.clock = FixedClock(NOW)
        settings = Settings(db_path=self.db_path, timezone="Asia/Shanghai")
        self.runtime = MemoryRuntime(settings=settings, clock=self.clock)
        self.addCleanup(self.runtime.close)

        self.repos = self.runtime.db.repositories
        self.scope = Scope(user_id="u1", character_id="c1")
        self.tz = load_timezone("Asia/Shanghai")

    # -- 便捷方法 ---------------------------------------------------------

    def make_turn(
        self,
        content: str,
        *,
        speaker: str = "user",
        conversation_id: str = "conv-test",
        activity_id: str | None = None,
    ) -> Turn:
        return Turn(
            speaker=speaker,
            content=content,
            occurred_at=self.clock.now(),
            conversation_id=conversation_id,
            activity_id=activity_id,
        )

    def add_memory(
        self,
        memory_type: MemoryType,
        body: dict,
        *,
        hours_ago: float | None = None,
        occurred_at: datetime | None = None,
        importance: float = 0.5,
        dedupe_key: str | None = None,
        **kwargs: object,
    ) -> MemoryItem | None:
        """直接往库里塞一条记忆，跳过抽取流程，便于单独测检索。"""
        if occurred_at is not None:
            moment = occurred_at
        elif hours_ago is not None:
            moment = NOW - timedelta(hours=hours_ago)
        else:
            moment = self.clock.now()

        item = MemoryItem(
            memory_id=new_id("mem"),
            memory_type=memory_type,
            scope=self.scope,
            body=body,
            occurred_start=moment,
            recorded_at=moment,
            importance=importance,
            **kwargs,  # type: ignore[arg-type]
        )
        with self.runtime.db.transaction():
            return self.repos.memories.insert(item, dedupe_key=dedupe_key)

    def count_rows(self, table: str) -> int:
        row = self.runtime.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])
