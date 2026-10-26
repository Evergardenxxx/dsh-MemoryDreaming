"""演示核心建议与宿主审批；不调用外部 LLM，也不把示例审批当成真实用户授权。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorydreaming import CoreProposalPolicy, HostAdapter, MemoryRuntime, Scope, SessionBinding
from memorydreaming.llm import dumps


def run(db_path):
    scope = Scope("demo-user", "xiaomeng", "family-role")
    policy = CoreProposalPolicy(relationship_fields=("称呼",))
    binding = SessionBinding("demo-user", "xiaomeng", "family-role", "window-1")
    with MemoryRuntime(db_path=str(db_path)) as memory:
        memory.apply_core_update(scope, "identity", {"名字": "小梦"}, "示例初始设定")
        memory.apply_core_update(scope, "relationship", {"称呼": "爸爸"}, "示例初始设定")
        session = HostAdapter(memory, core_policy=policy).open_session(binding)
        turn = session.prepare_turn("u-1", "以后叫我老爸吧。")
        target = session.bootstrap()["data"]["relationship_ref"]
        # 真实宿主将 adapter.tools 交给模型，再校验并转发它的工具参数。
        proposed = session.call_tool("memory_core_propose", {
            "target_ref": target, "fields": [{"name": "称呼", "value": "老爸", "evidence_refs": []}],
            "reason": "用户明确修改称呼", "max_tokens": 1200,
        })
        proposal_id = proposed["data"]["proposal_id"]
        print("建议结果：", dumps(proposed))
        print("审批前：", memory.bootstrap("demo-user", "xiaomeng", relationship_id="family-role").relationship.payload)

    # 模拟重启。只有宿主在核验上述示例请求后，调用这个带外审批入口。
    with MemoryRuntime(db_path=str(db_path)) as memory:
        session = HostAdapter(memory, core_policy=policy).open_session(binding)
        print("恢复待审建议：", dumps(session.get_core_proposal(proposal_id)))
        receipt = session.approve_core_proposal(proposal_id, user_event_id=turn.user_event_id,
            reviewer="demo-host", reason="示例宿主核验了这条固定用户请求")
        print("生效回执：", dumps(receipt))
        prepared = session.prepare_turn("u-2", "你现在怎么叫我？")
        print("审批后恢复：", prepared.messages[1]["content"])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        run(Path(folder) / "core-proposal.db")
