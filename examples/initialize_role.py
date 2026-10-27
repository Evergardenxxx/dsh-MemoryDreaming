"""由本地操作者明确提供设定，为空角色初始化有来源的核心；拒绝覆盖旧核心。"""

import argparse
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorydreaming import MemoryRuntime, Scope
from memorydreaming.llm import dumps
from memorydreaming.models import CoreKind, Turn


def initialize_role(db_path, *, user, character, relationship, name, role, address, relationship_note, domain="real"):
    """可信本地入口，不接受模型自动批准；两个核心与用户设定来源同事务落库。"""
    for field, value in (("name", name), ("role", role), ("address", address), ("relationship_note", relationship_note)):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{field} 须为1..200字符的明确设定")
    scope = Scope(user, character, relationship, domain=domain)
    with MemoryRuntime(db_path=str(db_path)) as memory:
        with memory.db.transaction():
            # 检查原始当前版本而非经过可见性过滤的bootstrap结果；来源失效也
            # 不能被误判为“从未设置”，从而用初始化绕过历史或共享身份。
            if any(memory.db.repositories.cores.latest(scope, kind) is not None
                   for kind in (CoreKind.IDENTITY, CoreKind.RELATIONSHIP)):
                raise ValueError("该绑定已有身份或关系核心；请使用可信宿主审批修改，初始化不覆盖旧值")
            identity = {"名字": name.strip(), "身份": role.strip()}
            relation = {"称呼": address.strip(), "相处设定": relationship_note.strip()}
            source = "本地操作者明确初始化角色设定：" + dumps({"identity": identity, "relationship": relation})
            turn = Turn(speaker="user", content=source, occurred_at=memory.clock.now(),
                        conversation_id="local-explicit-role-initialization")
            event = memory.observe(turn, scope, "initialize-role:"+uuid.uuid4().hex).event
            reason = "本地操作者显式传入初始化参数"
            for kind, fields in (("identity", identity), ("relationship", relation)):
                memory.apply_core_update(scope, kind, fields, reason, expected_version=0,
                                         evidence_ids=[event.event_id])
        return {"initialized": True, "database": str(Path(db_path).resolve()),
                "continuity": memory.continuity.check(scope)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--user", default="u1")
    parser.add_argument("--character", default="c1")
    parser.add_argument("--relationship", default="r1")
    parser.add_argument("--domain", default="real")
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--relationship-note", required=True)
    args = parser.parse_args()
    try:
        result = initialize_role(args.db, user=args.user, character=args.character,
            relationship=args.relationship, name=args.name, role=args.role, address=args.address,
            relationship_note=args.relationship_note, domain=args.domain)
    except ValueError as error:
        parser.exit(2, str(error)+"\n")
    print(dumps(result))
