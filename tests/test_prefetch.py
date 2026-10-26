from __future__ import annotations

from unittest.mock import patch

from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.models import MemoryType
from tests.helpers import RuntimeTestCase


class PrefetchTests(RuntimeTestCase):
    def test_prefetch_warms_cache_without_returning_memory_body(self):
        self.scope = self.scope.__class__("u1", "c1", "r1")
        self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"},
        )
        session = HostAdapter(self.runtime, clock=self.clock).open_session(
            SessionBinding("u1", "c1", "r1", "prefetch-window")
        )
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            result = session.prefetch("喜欢海边故事")
            search = session.call_tool("memory_search", {
                "query": "喜欢海边故事", "activity_ref": None, "time_range": None,
                "detail": "auto", "max_items": 12, "max_tokens": 1200,
            })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(retrieve.call_count, 1)
        self.assertIn("海边故事", search["data"]["evidence"][0]["text"])
        self.assertNotIn("subject", str(result))
        self.assertNotIn("predicate", str(result))

