"""首次上手示例必须保护已有角色与来源，失败不能留下半套核心。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from examples.initialize_role import initialize_role
from memorydreaming import MemoryRuntime, Scope


class RoleInitializationTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)/"role.db"
        self.options = dict(user="u1", character="c1", relationship="r1", name="小梦",
                            role="长期陪伴角色", address="爸爸", relationship_note="用户确认的父女角色称呼")

    def test_fresh_role_has_user_source_and_forgetting_cannot_reinitialize_it(self):
        result = initialize_role(self.path, **self.options)
        self.assertTrue(result["initialized"])
        self.assertEqual(result["continuity"]["recovery_status"], "complete")
        scope = Scope("u1", "c1", "r1")
        with MemoryRuntime(db_path=str(self.path)) as memory:
            core = memory.bootstrap("u1", "c1", relationship_id="r1").identity
            self.assertEqual(core.payload["名字"], "小梦")
            self.assertEqual(len(core.evidence_ids), 1)
            source = memory.db.repositories.events.get(core.evidence_ids[0])
            self.assertEqual(source.speaker, "user")
            self.assertIn("小梦", source.content)
            memory.forget_event(source.event_id)
            self.assertEqual(memory.continuity.check(scope)["recovery_status"], "incomplete")
        with self.assertRaisesRegex(ValueError, "已有"):
            initialize_role(self.path, **self.options)

    def test_repeated_or_shared_identity_initialization_does_not_overwrite(self):
        initialize_role(self.path, **self.options)
        for options in (self.options, {**self.options, "relationship": "another", "name": "另一个名字"}):
            with self.assertRaisesRegex(ValueError, "已有"):
                initialize_role(self.path, **options)
        with MemoryRuntime(db_path=str(self.path)) as memory:
            self.assertEqual(memory.bootstrap("u1", "c1", relationship_id="r1").identity.payload["名字"], "小梦")
            self.assertEqual(memory.db.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(memory.db.connection.execute("SELECT COUNT(*) FROM core_versions").fetchone()[0], 2)

    def test_second_core_failure_rolls_back_event_jobs_and_first_core(self):
        original = MemoryRuntime.apply_core_update

        def fail_relationship(memory, scope, kind, *args, **kwargs):
            if kind == "relationship":
                raise RuntimeError("injected relationship failure")
            return original(memory, scope, kind, *args, **kwargs)

        with patch.object(MemoryRuntime, "apply_core_update", fail_relationship):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                initialize_role(self.path, **self.options)
        with closing(sqlite3.connect(self.path)) as connection:
            for table in ("events", "core_versions", "consolidation_jobs", "journal_entries"):
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0, table)

    def test_invalid_local_settings_do_not_create_database(self):
        for field in ("name", "role", "address", "relationship_note"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                initialize_role(self.path, **{**self.options, field: ""})
        self.assertFalse(self.path.exists())
