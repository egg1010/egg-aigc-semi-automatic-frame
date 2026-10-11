"""写入安全测试 —— 架构改造方案 v2 §11 验收表（事实/冲突/原子性/隔离/回写/注入）

运行：python -m unittest -v test_memory_store.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import (  # noqa: E402
    Conflict, EvidenceError, IdempotencyError, MemoryDB, PermissionError_,
)


def build(db, project_id="film_001"):
    """搭一个 3 场的最小剧本环境。"""
    db.project(project_id, "测试短片")
    script = "第三场：林雨将钥匙交给陈明。"
    doc = db.document(project_id, "剧本/第三场.txt", script)
    chunk = db.chunk(project_id, doc, script, scene_id="scene_3", ordinal=0)
    db.scene(project_id, "scene_1", 1)
    db.scene(project_id, "scene_3", 3)
    db.scene(project_id, "scene_5", 5)
    db.entity(project_id, "key_001", "prop", "钥匙")
    db.entity(project_id, "key_002", "prop", "盒子")
    return chunk


class TestApprovedEventAndConflict(unittest.TestCase):
    """§11 事实转移 + 错误旧值"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_approved_transfer(self):
        e1 = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", e1)
        self.assertEqual(self.db.state("film_001", "key_001", "owner"), "linyu")

        e2 = self.db.propose_event(
            "film_001", "scene_3", "prop_transfer",
            [{"entity_id": "key_001", "field": "owner",
              "expected": "linyu", "new": "chenming"}], self.chunk,
            event_order=1)
        self.db.approve_event("film_001", e2)
        self.assertEqual(self.db.state("film_001", "key_001", "owner"),
                         "chenming")

        # 事件与证据可查
        events = self.db.events_for_scene("film_001", "scene_3")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["approval"] == "approved" for e in events))
        ev = self.db.evidence("film_001", events[1]["evidence_chunk_id"])
        self.assertIn("钥匙", ev["content"])

    def test_wrong_expected_rejected_and_state_untouched(self):
        e1 = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", e1)

        # 陈旧提案：以为还是 linyu
        stale = self.db.propose_event(
            "film_001", "scene_3", "prop_transfer",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "chenming"}], self.chunk,
            idempotency_key="stale-1")
        with self.assertRaises(Conflict) as ctx:
            self.db.approve_event("film_001", stale)
        self.assertEqual(ctx.exception.error_code, "CONFLICT_STATE")
        self.assertIn("expected", str(ctx.exception))
        # 状态未被污染
        self.assertEqual(self.db.state("film_001", "key_001", "owner"), "linyu")
        row = self.db.db.execute(
            "SELECT approval FROM events WHERE event_id=?", (stale,)).fetchone()
        self.assertEqual(row["approval"], "proposed")


class TestAtomicity(unittest.TestCase):
    """§11 事务原子性：一项合法、一项冲突 → 全不变"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_multi_change_all_or_nothing(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "multi",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"},
             {"entity_id": "key_002", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", ev)

        # 第二项 expected 是错的 → 第一项也不得落库
        bad = self.db.propose_event(
            "film_001", "scene_3", "multi_bad",
            [{"entity_id": "key_001", "field": "owner",
              "expected": "linyu", "new": "chenming"},
             {"entity_id": "key_002", "field": "owner",
              "expected": "WRONG", "new": "chenming"}], self.chunk)
        with self.assertRaises(Conflict):
            self.db.approve_event("film_001", bad)

        self.assertEqual(self.db.state("film_001", "key_001", "owner"), "linyu")
        self.assertEqual(self.db.state("film_001", "key_002", "owner"), "linyu")


class TestIdempotency(unittest.TestCase):
    """§11 崩溃恢复 / 重复执行不重复变更"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_same_key_same_payload_returns_same_event(self):
        changes = [{"entity_id": "key_001", "field": "owner",
                    "expected": None, "new": "linyu"}]
        a = self.db.propose_event("film_001", "scene_3", "initial_state",
                                  changes, self.chunk, idempotency_key="k1")
        b = self.db.propose_event("film_001", "scene_3", "initial_state",
                                  changes, self.chunk, idempotency_key="k1")
        self.assertEqual(a, b)

    def test_same_key_different_payload_rejected(self):
        self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk,
            idempotency_key="k1")
        with self.assertRaises(IdempotencyError):
            self.db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "chenming"}], self.chunk,
                idempotency_key="k1")

    def test_repeated_approval_no_double_apply(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", ev)
        self.db.approve_event("film_001", ev)  # 重试
        rows = self.db.state("film_001", "key_001")
        self.assertEqual(rows[0]["version"], 1, "重复审批不得重复变更")


class TestEvidenceRequired(unittest.TestCase):
    """§3.2 第 2 条：证据必填且可追溯"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_missing_evidence_rejected(self):
        with self.assertRaises(EvidenceError) as ctx:
            self.db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], "chunk_不存在")
        self.assertEqual(ctx.exception.error_code, "EVIDENCE_NOT_FOUND")

    def test_cross_project_evidence_rejected(self):
        """同库多项目场景：A 项目的证据不得用于 B 项目的事件。

        单库单项目部署时别的项目的 chunk 不可见，会退化为 EVIDENCE_NOT_FOUND；
        这里显式构造同库双项目，验证按 project_id 的真实拦截。
        """
        # 直接在同一个库里建第二个项目与文档（模拟多项目共库）
        self.db.project("film_002", "另一部短片")
        doc_b = self.db.document("film_002", "剧本/别处.txt", "别的项目的原文")
        chunk_b = self.db.chunk("film_002", doc_b, "别的项目的原文")
        self.db.scene("film_002", "scene_3", 3)

        with self.assertRaises(EvidenceError) as ctx:
            self.db.propose_event(
                "film_002", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], self.chunk)
        # 用的是 film_001 的证据块 → 跨项目拦截
        self.assertEqual(ctx.exception.error_code, "EVIDENCE_CROSS_PROJECT")

    def test_unknown_chunk_is_not_found(self):
        with self.assertRaises(EvidenceError) as ctx:
            self.db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], "chunk_真的不存在")
        self.assertEqual(ctx.exception.error_code, "EVIDENCE_NOT_FOUND")

    def test_unknown_entity_rejected(self):
        with self.assertRaises(Conflict) as ctx:
            self.db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "不存在", "field": "owner",
                  "expected": None, "new": "linyu"}], self.chunk)
        self.assertEqual(ctx.exception.error_code, "ENTITY_NOT_FOUND")

    def test_unknown_scene_rejected(self):
        with self.assertRaises(Conflict) as ctx:
            self.db.propose_event(
                "film_001", "scene_99", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], self.chunk)
        self.assertEqual(ctx.exception.error_code, "SCENE_NOT_FOUND")


class TestProjectIsolation(unittest.TestCase):
    """§11 项目隔离"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk_a = build(self.db, "film_001")

    def tearDown(self):
        self.db.close()

    def test_state_isolated(self):
        build(self.db, "film_002")
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk_a)
        self.db.approve_event("film_001", ev)

        self.assertEqual(self.db.state("film_001", "key_001", "owner"), "linyu")
        self.assertIsNone(self.db.state("film_002", "key_001", "owner"))
        self.assertEqual(self.db.state("film_002"), [])

    def test_search_isolated(self):
        """同一个库内两个项目：检索结果不得跨项目串台。"""
        build(self.db, "film_002")
        hits_a = self.db.search("film_001", "钥匙")
        hits_b = self.db.search("film_002", "钥匙")
        self.assertEqual(len(hits_a), 1)
        self.assertEqual(len(hits_b), 1)
        # 关键：两个项目各命中自己的块，绝不返回对方的 chunk
        self.assertNotEqual(hits_a[0]["chunk_id"], hits_b[0]["chunk_id"])
        ids_a = {r["chunk_id"] for r in self.db.db.execute(
            "SELECT chunk_id FROM chunks WHERE project_id='film_001'")}
        self.assertTrue({r["chunk_id"] for r in hits_b}.isdisjoint(ids_a))

    def test_unknown_project_rejected(self):
        with self.assertRaises(Conflict) as ctx:
            self.db.state("不存在的项目")
        self.assertEqual(ctx.exception.error_code, "PROJECT_NOT_FOUND")


class TestHistoricalRewrite(unittest.TestCase):
    """§11 历史回写：不得直接覆盖头部状态"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_earlier_scene_cannot_overwrite_later_state(self):
        e1 = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", e1, actor="human")
        e2 = self.db.propose_event(
            "film_001", "scene_5", "prop_transfer",
            [{"entity_id": "key_001", "field": "owner",
              "expected": "linyu", "new": "chenming"}], self.chunk,
            event_order=1)
        self.db.approve_event("film_001", e2, actor="human")

        # 现在回写第 1 场（order=1 < 5）
        stale = self.db.propose_event(
            "film_001", "scene_1", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": "chenming", "new": "linyu"}], self.chunk,
            event_order=2)
        with self.assertRaises(Conflict) as ctx:
            self.db.approve_event("film_001", stale)
        self.assertEqual(ctx.exception.error_code,
                         "HISTORICAL_EDIT_REQUIRES_REPLAY")
        self.assertIn("重放", str(ctx.exception))
        self.assertEqual(self.db.state("film_001", "key_001", "owner"),
                         "chenming")


class TestCertaintyGate(unittest.TestCase):
    """§5：非 explicit 事实不得直接进入正式状态"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_inferred_rejected(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk,
            certainty="inferred")
        with self.assertRaises(PermissionError_) as ctx:
            self.db.approve_event("film_001", ev)
        self.assertEqual(ctx.exception.error_code, "CERTAINTY_NOT_EXPLICIT")
        self.assertIsNone(self.db.state("film_001", "key_001", "owner"))

    def test_invalid_certainty_value(self):
        with self.assertRaises(ValueError):
            self.db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], self.chunk,
                certainty="瞎猜")


class TestPersistenceAndAudit(unittest.TestCase):
    """落盘 + 审计 + 跨项目挂块防护"""

    def test_on_disk_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = MemoryDB.project_db_path(tmp, "film_001")
            db = MemoryDB(path)
            chunk = build(db)
            ev = db.propose_event(
                "film_001", "scene_3", "initial_state",
                [{"entity_id": "key_001", "field": "owner",
                  "expected": None, "new": "linyu"}], chunk)
            db.approve_event("film_001", ev)
            db.close()

            db2 = MemoryDB(path)
            self.assertEqual(db2.state("film_001", "key_001", "owner"), "linyu")
            user_version = db2.db.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(user_version, 1)
            actions = [r["action"] for r in db2.db.execute(
                "SELECT action FROM audit_log ORDER BY audit_id")]
            self.assertIn("event.propose", actions)
            self.assertIn("event.approve", actions)
            db2.close()

    def test_project_db_path_blocks_traversal(self):
        with self.assertRaises(ValueError):
            MemoryDB.project_db_path("data", "../../etc")

    def test_cross_project_chunk_denied(self):
        db = MemoryDB(":memory:")
        try:
            build(db, "film_001")
            build(db, "film_002")
            doc_a = db.db.execute(
                "SELECT document_id FROM documents WHERE project_id='film_001'"
            ).fetchone()["document_id"]
            with self.assertRaises(PermissionError_) as ctx:
                db.chunk("film_002", doc_a, "偷挂")
            self.assertEqual(ctx.exception.error_code, "CROSS_PROJECT_DENIED")
        finally:
            db.close()


class TestAuditIntegrity(unittest.TestCase):
    """审计详情可机读"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.chunk = build(self.db)

    def tearDown(self):
        self.db.close()

    def test_audit_details_are_json(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "key_001", "field": "owner",
              "expected": None, "new": "linyu"}], self.chunk)
        self.db.approve_event("film_001", ev, actor="human")
        row = self.db.db.execute(
            "SELECT details_json FROM audit_log WHERE action='event.approve'"
        ).fetchone()
        details = json.loads(row["details_json"])
        self.assertEqual(details["actor"], "human")


if __name__ == "__main__":
    unittest.main(verbosity=2)
