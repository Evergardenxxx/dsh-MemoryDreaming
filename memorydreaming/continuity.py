"""苏醒连续性检查与可迁移生存档案。

这里验证可恢复的角色资料，不声明模型主观身份相同。档案封装现有快照，
保留目标库的遗忘预检。私密密钥恢复仅适用于全新空库，非空目标的屏障
绝不重设；公开状态卡和回执不泄露恢复材料。
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from .clock import to_utc_iso
from .life_evidence import dumps
from .life_evidence import LifeEvidence
from .llm import AdapterError, EstimatedTokenCounter
from .models import Scope
from .narrative import NarrativeService
from .portability import export_snapshot, import_snapshot, validate_snapshot
from .references import ViewReader
from .storage.repository import _scope_key


def _hash(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def _binding(scope):
    return SimpleNamespace(user_id=scope.user_id, character_id=scope.character_id,
        relationship_id=scope.relationship_id, domain=scope.domain, scope=scope,
        allowed_activity_ids=(() if scope.activity_id is None else (scope.activity_id,)))


class ContinuityService:
    """按同一可信 scope 复核当前角色、经历、承诺和恢复缺口。"""

    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db

    def _life_state(self, scope):
        """只读的完整连续性锚，不调用会懒清理状态的列表接口。

        卡片可以截断，档案核验不能因为UI预算漏掉后面的承诺/偏好。表内容
        不进入模型，最终只输出摘要。来源已失效的值不作为当前生活状态。
        """
        evidence = LifeEvidence(self.runtime)
        state = {}
        tables = {row[0] for row in self.db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        definitions = (
            ("intentions", "state='open'", "sources_json", "confirmation_json"),
            ("companionship_preferences", "state='confirmed'", "evidence_json", "approval_event_json"),
            ("growth_values", "state='active'", "sources_json", "confirmation_json"),
        )
        for table, condition, sources, confirmations in definitions:
            accepted = []
            if table in tables:
                rows = self.db.connection.execute("SELECT * FROM "+table+" WHERE scope_key=? AND "+condition, (_scope_key(scope),)).fetchall()
                for row in rows:
                    refs = json.loads(row[sources])
                    confirmation = json.loads(row[confirmations] or "[]")
                    trigger = json.loads(row["trigger_evidence_json"] or "[]") if table == "intentions" else []
                    if (not evidence.valid(scope, refs) or (confirmation and not evidence.valid(scope, confirmation, require_user=True))
                            or (trigger and not evidence.valid(scope, trigger))):
                        continue
                    if table == "growth_values" and row["source_epoch"] != self.db.repositories.forgetting.epoch(scope):
                        continue
                    accepted.append(dict(row))
            state[table] = sorted(accepted, key=dumps)
        return state

    def _core(self, scope, kind):
        try:
            view = ViewReader(self.runtime, _binding(scope), self.runtime.clock).load("core_version", kind)
            payload = json.loads(view.data["text"])
            return {"state": "recovered" if payload else "incomplete", "version": view.data["record_version"], "fields": payload}
        except AdapterError:
            return {"state": "missing", "version": None, "fields": {}}

    def check(self, scope, *, max_tokens=900, intentions=None, companionship=None):
        """生成有界状态卡；缺少核心就报告缺口，不生成临时身份覆盖它。"""
        if not isinstance(scope, Scope):
            raise ValueError("scope 必须由可信宿主提供")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 128 <= max_tokens <= 4096:
            raise ValueError("max_tokens 必须为 128..4096")
        with self.db.transaction(write=False):
            identity, relationship = self._core(scope, "identity"), self._core(scope, "relationship")
            story = NarrativeService(self.runtime).timeline(scope, limit=5, max_tokens=600)
            if intentions is None:
                intentions = [{"intention_id": row["intention_id"], "version": row["version"], "text": row["text"],
                    "owner": row["owner"], "category": row["category"], "due_at": row["due_at"],
                    "event_trigger": row["event_trigger"], "state": "open", "source_refs": json.loads(row["sources_json"])}
                    for row in self._life_state(scope)["intentions"][:5]]
            if companionship is None:
                service = getattr(self.runtime, "companionship", None)
                companionship = service.read_for_context(scope, context="default", limit=5) if service and hasattr(service, "read_for_context") else []
            if isinstance(intentions, dict):
                intentions = intentions.get("items", intentions.get("intentions", intentions.get("promises", [])))
            if isinstance(companionship, dict):
                companionship = companionship.get("items", companionship.get("preferences", []))
            gaps = []
            for name, core in (("identity", identity), ("relationship", relationship)):
                if core["state"] != "recovered":
                    gaps.append({"code": "MISSING_"+name.upper(), "meaning": "该核心尚未完整恢复；不应猜测"})
            if not story["entries"]:
                gaps.append({"code": "NO_CONFIRMED_AUTOBIOGRAPHY", "meaning": "尚无来源有效的已确认自传节点"})
            card = {"kind": "continuity_card", "recovery_status": "complete" if not any(core["state"] != "recovered" for core in (identity, relationship)) else "incomplete",
                    "identity": identity, "relationship": relationship,
                    "autobiography": story["entries"], "intentions": list(intentions or [])[:5],
                    "companionship": list(companionship or [])[:5], "gaps": gaps,
                    "growth": self.runtime.growth.profile(scope, limit=4) if hasattr(self.runtime, "growth") else [],
                    "instructions": "只沿用已恢复资料。自传是已确认投影；梦不是事实，恢复缺口须坦诚说明。",
                    "truncated": story["truncated"]}
            counter = EstimatedTokenCounter()
            while counter.count(dumps(card)) > max_tokens:
                if card["autobiography"]:
                    card["autobiography"].pop(0)
                elif card["growth"]:
                    card["growth"].pop()
                elif card["companionship"]:
                    card["companionship"].pop()
                elif card["intentions"]:
                    card["intentions"].pop()
                elif card.get("instructions"):
                    del card["instructions"]
                elif any(core["fields"] for core in (identity, relationship)):
                    # 优先保留身份名称/称呼。字段整体裁剪，不能伪造半个值。
                    core = max((identity, relationship), key=lambda value: len(dumps(value["fields"])))
                    field = next((name for name in core["fields"] if name not in ("名字", "称呼")), next(iter(core["fields"])))
                    core["fields"].pop(field)
                else:
                    card = {"kind": "continuity_card", "recovery_status": card["recovery_status"],
                            "gaps": [gap["code"] for gap in gaps], "truncated": True}
                    if counter.count(dumps(card)) > max_tokens:
                        card["gaps"] = ["DETAILS_EXCEED_BUDGET"]
                    break
                card["truncated"] = True
            return card

    def fingerprint(self, scope):
        """恢复核验锚：核心内容/版本、自传当前存活版本、来源遗忘代次。"""
        with self.db.transaction(write=False):
            identity, relationship = self._core(scope, "identity"), self._core(scope, "relationship")
            service = NarrativeService(self.runtime)
            rows = []
            for row in self.db.connection.execute(
                "SELECT entry_id,version,sources_json FROM narrative_entries WHERE scope_key=? AND state='accepted' ORDER BY entry_id",
                (_scope_key(scope),),
            ).fetchall():
                refs = json.loads(row["sources_json"])
                if service.evidence.valid(scope, refs):
                    rows.append({"entry_id": row["entry_id"], "version": row["version"], "sources": refs})
            return _hash({"scope": asdict(scope), "identity": identity, "relationship": relationship,
                          "narrative": rows, "life_state": self._life_state(scope),
                          "epoch": self.db.repositories.forgetting.epoch(scope)})

    def prepare_archive(self, scope, path):
        """以一个原子文件保存角色快照及连续性清单，拒绝覆盖现有档案。"""
        if not isinstance(scope, Scope):
            raise ValueError("scope 必须由可信宿主提供")
        target = Path(path).expanduser().resolve()
        if target.exists():
            raise ValueError("生存档案已存在，拒绝覆盖")
        with self.db.transaction(write=False):
            snapshot = export_snapshot(self.db, scope=scope)
            manifest = {"format": "memorydreaming.continuity-archive.v1", "scope": asdict(scope),
                        "created_at": to_utc_iso(self.runtime.clock.now()), "continuity_fingerprint": self.fingerprint(scope),
                        "snapshot_sha256": _hash(snapshot), "table_counts": {name: len(rows) for name, rows in snapshot["tables"].items()},
                        "scope_epoch": self.db.repositories.forgetting.epoch(scope),
                        "forgetting_key_digest": snapshot["manifest"]["forgetting_key_digest"],
                        "claims": {"role_data_continuity": True, "same_subjective_identity": False,
                                   "cross_key_import_supported": False, "fresh_target_recovery": True,
                                   "external_context_erased": False}}
            # 档案本来就含私密原话；另带恢复材料才能在全新设备保留原遗忘
            # 指纹语义。密钥不进入公开manifest、状态卡、模型上下文或回执。
            wrapper = {"manifest": manifest, "snapshot": snapshot,
                       "recovery_material": {"forgetting_hmac_key": self.db.meta_get("forgetting_hmac_key")}}
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=target.name+".", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(dumps(wrapper)+"\n"); output.flush(); os.fsync(output.fileno())
            # 原子 no-clobber 发布，其他写入者抢先创建目标时不覆盖备份。
            os.link(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"path": str(target), "manifest": manifest, "written": True}

    def _archive(self, path, scope):
        if not isinstance(scope, Scope):
            raise ValueError("scope 必须由可信宿主提供")
        target = Path(path).expanduser().resolve()
        if not target.is_file() or target.is_symlink() or target.stat().st_size > 256*1024*1024:
            raise ValueError("生存档案缺失或超过 256 MiB 上限")
        wrapper = json.loads(target.read_text(encoding="utf-8"))
        manifest, snapshot = wrapper.get("manifest"), wrapper.get("snapshot")
        recovery = wrapper.get("recovery_material")
        if (not isinstance(manifest, dict) or manifest.get("format") != "memorydreaming.continuity-archive.v1"
                or manifest.get("scope") != asdict(scope) or not isinstance(snapshot, dict)
                or manifest.get("snapshot_sha256") != _hash(snapshot)
                or manifest.get("claims") != {"role_data_continuity": True, "same_subjective_identity": False,
                    "cross_key_import_supported": False, "fresh_target_recovery": True, "external_context_erased": False}
                or not isinstance(recovery, dict) or set(recovery) != {"forgetting_hmac_key"}):
            raise ValueError("生存档案格式、范围或内容摘要不匹配")
        key = recovery["forgetting_hmac_key"]
        if (not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
                or hashlib.sha256(key.encode("utf-8")).hexdigest() != manifest.get("forgetting_key_digest")
                or snapshot.get("manifest", {}).get("forgetting_key_digest") != manifest.get("forgetting_key_digest")):
            raise ValueError("恢复材料与遗忘账本摘要不一致")
        return manifest, snapshot, key

    def _empty_target(self):
        """只允许真正空的新库接收私密恢复密钥；已有删除账本也算有数据。"""
        for row in self.db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            table = row[0]
            if table in ("schema_meta", "retrieval_state", "sqlite_sequence") or table.startswith("memories_fts"):
                continue
            # 数据表名称来自sqlite_master，双引号转义避免把名称当SQL代码。
            quoted = '"'+table.replace('"', '""')+'"'
            if self.db.connection.execute("SELECT 1 FROM "+quoted+" LIMIT 1").fetchone():
                return False
        return True

    def _fresh_recovery_possible(self, snapshot, key):
        return (self.db.meta_get("forgetting_hmac_key") != key and self._empty_target())

    def verify_archive(self, path, scope):
        """只读预检；目标库的账本/代次/已清除范围继续具有拒绝权。"""
        if not isinstance(scope, Scope):
            raise ValueError("scope 必须由可信宿主提供")
        manifest, snapshot, key = self._archive(path, scope)
        with self.db.transaction(write=False):
            preflight = validate_snapshot(self.db, snapshot)
            current = self.fingerprint(scope)
            fresh = self._fresh_recovery_possible(snapshot, key)
            effective_errors = [error for error in preflight["errors"]
                                if not (fresh and error["code"] in ("FORGETTING_KEY_MISMATCH", "GENERATION_MISMATCH"))]
        return {"ready": not effective_errors, "preflight": {**preflight, "ready": not effective_errors, "errors": effective_errors},
                "continuity_matches_current": current == manifest["continuity_fingerprint"],
                "expected_fingerprint": manifest["continuity_fingerprint"], "current_fingerprint": current,
                "cross_key_import_supported": False, "fresh_target_key_recovery_required": fresh}

    def restore_archive(self, path, scope):
        """复用主快照原子导入并核验连续性，绝不为导入重置遗忘账本。"""
        manifest, snapshot, key = self._archive(path, scope)
        with self.db.transaction():
            fresh = self._fresh_recovery_possible(snapshot, key)
            if fresh:
                self.db.meta_set("forgetting_hmac_key", key)
                # 仅空库同步档案的遗忘代次再执行原预检；非空库绝不重设
                # 屏障。后续任何失败与密钥/屏障同步在同一事务中回滚。
                for row in snapshot.get("tables", {}).get("scope_mutations", []):
                    self.db.connection.execute("INSERT INTO scope_mutations(scope_key,epoch) VALUES(?,?)",
                                               (row["scope_key"], row["epoch"]))
            preflight = validate_snapshot(self.db, snapshot)
            if not preflight["ready"]:
                raise ValueError(dumps(preflight))
            imported = import_snapshot(self.db, snapshot)
            actual = self.fingerprint(scope)
            if actual != manifest["continuity_fingerprint"]:
                raise ValueError("恢复后的核心/自传/遗忘代次与档案不一致，导入已回滚")
            return {"restored": True, "continuity_verified": True, "fingerprint": actual,
                    "fresh_target_key_recovered": fresh, "import": imported, "claims": manifest["claims"]}
