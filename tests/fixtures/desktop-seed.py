"""Create synthetic, source-backed records for the opt-in browser acceptance.

This fixture never reads a Harness profile or existing memory database. Its
caller provides a newly-created temporary database; role initialization remains
an actual user-interface operation in the browser driver.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memorydreaming.config import Settings
from memorydreaming.models import MemoryItem, MemoryType, Scope, Turn, new_id
from memorydreaming.runtime import MemoryRuntime


def seed(path: Path) -> dict:
    if path.exists():
        raise ValueError("Browser seed requires a new database")
    runtime = MemoryRuntime(settings=Settings(db_path=path))
    scope = Scope("desktop-test-user", "desktop-test-character", "desktop-test-relationship")
    created = {}

    def add(kind, body, source):
        now = runtime.clock.now()
        event = runtime.observe(Turn("user", source, now, "desktop-demo"), scope, new_id("desktop-input")).event
        item = MemoryItem(new_id("mem"), kind, scope, body,
            source_event_ids=[event.event_id], source_speaker="user", occurred_start=now,
            occurred_precision="exact", recorded_at=now,
            source_spans={event.event_id: [[0, len(source)]]})
        with runtime.db.transaction():
            runtime.db.repositories.memories.insert(item)
        return {"id": item.memory_id, "event_id": event.event_id, "source": source}

    try:
        created["working"] = add(MemoryType.WORKING,
            {"goal": "演示工作：准备热茶", "open_loops": ["清洗茶杯"], "active_entities": ["演示角色"], "checkpoint_note": "等待水开"},
            "演示工作：准备热茶，先清洗茶杯，等水开。")
        created["episodic"] = add(MemoryType.EPISODIC,
            {"what_happened": "演示情景：花园听雨", "participants": ["演示角色", "演示用户"], "context": "小雨", "outcome": "一起记录雨声"},
            "演示情景：今天在花园听雨，一起记录了雨声。")
        created["procedural"] = add(MemoryType.PROCEDURAL,
            {"trigger": "演示程序：泡茶", "preconditions": ["有干净茶杯"], "steps": ["温杯", "放茶", "注水"], "exceptions": [], "expected_outcome": "一杯温茶", "supporting_examples": []},
            "演示程序：泡茶时先温杯、放茶，再注水。")
        created["correct"] = add(MemoryType.SEMANTIC,
            {"subject": "演示用户", "predicate": "喜欢", "obj": "热茶纠错目标", "scope_note": "合成验收资料"},
            "演示用户喜欢热茶纠错目标。")
        created["forget"] = add(MemoryType.SEMANTIC,
            {"subject": "演示用户", "predicate": "喜欢", "obj": "删除专用薄荷茶", "scope_note": "合成验收资料"},
            "演示用户喜欢删除专用薄荷茶。")
        payload = '<img src=x onerror="window.__memorydreamingInjected=1">合成HTML测试'
        created["html"] = add(MemoryType.SEMANTIC,
            {"subject": "演示用户", "predicate": "引用", "obj": payload, "scope_note": "仅文本"}, payload)
        for index in range(23):
            add(MemoryType.SEMANTIC,
                {"subject": "演示用户", "predicate": "收藏", "obj": f"分页热茶样本{index:02d}", "scope_note": "合成验收资料"},
                f"演示用户收藏分页热茶样本{index:02d}。")
        growth_event = runtime.observe(Turn("user", "我希望演示角色讲故事时慢一点。", runtime.clock.now(), "desktop-demo"), scope, new_id("desktop-input")).event
        created["growth"] = runtime.growth.propose(scope, "storytelling", "演示成长：讲故事慢一点", [growth_event.event_id],
            reason="合成用户明确希望", request_id="desktop-browser-growth")
        return {"status": "seeded", "synthetic": True, "records": created, "semantic_count": 26}
    finally:
        runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(seed(arguments.database), ensure_ascii=False))
