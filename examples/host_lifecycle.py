"""可运行的跨窗口宿主接线示例；打印真实记忆结果，不调用模型 API。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorydreaming import HostAdapter, MemoryRuntime, Scope, SessionBinding
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.llm import dumps


def run(db_path):
    scope = Scope("u1", "c1", "r1")
    with MemoryRuntime(db_path=str(db_path)) as memory:
        existing = memory.bootstrap("u1", "c1", relationship_id="r1")
        if existing.identity is None:
            memory.propose_core_update(scope, "identity", {"名字": "小梦", "身份": "长期陪伴角色"}, "示例用户明确设定")
        if existing.relationship is None:
            memory.propose_core_update(scope, "relationship", {"相处设定": "采用父女角色称呼", "称呼": "爸爸"}, "示例用户明确设定")
        session = HostAdapter(memory).open_session(SessionBinding("u1", "c1", "r1", "example-window-1"))
        turn = session.prepare_turn("u-1", "我喜欢睡前听海边故事。")
        # 真实宿主在这里将 turn.messages 交给模型，并转发 call_tool 工具调用。
        # 示例只登记一条明确的已显示回复，不伪造真实模型调用。
        session.after_delivery("a-1", "好，我们下次继续聊。", user_event_id=turn.user_event_id)
        for _ in range(3):
            memory.run_consolidation(RuleBasedExtractor())
        session.close()

    with MemoryRuntime(db_path=str(db_path)) as memory:
        session = HostAdapter(memory).open_session(SessionBinding("u1", "c1", "r1", "example-window-2"))
        turn = session.prepare_turn("u-2", "还记得我睡前喜欢什么吗？")
        result = session.call_tool("memory_search", {"query": "睡前海边故事", "activity_ref": None,
            "time_range": None, "detail": "evidence", "max_items": 4, "max_tokens": 1600})
        print("新窗口恢复：", turn.recovery_status)
        print("记忆消息计量：", turn.counted_tokens, turn.count_mode)
        print(dumps(result))
        if result["data"] and result["data"]["evidence"]:
            sources = result["data"]["evidence"][0]["source_refs"]
            print("原话展开：", dumps(session.call_tool("memory_read", {"references": sources, "max_tokens": 1600})))
        print("数据库：", Path(db_path).resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="保留示例库供 MCP 客户端查询；省略时使用临时库")
    args = parser.parse_args()
    if args.db:
        run(Path(args.db))
    else:
        with tempfile.TemporaryDirectory() as folder:
            run(Path(folder) / "host-demo.db")
