"""规则版本化 + 检索 + 上下文预算测试 —— v2 §4 / §8.2 / §10.3"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from context_builder import ContextBuilder, estimate_tokens  # noqa: E402
from knowledge_loader import (  # noqa: E402
    KnowledgeLoader, RuleCache, RuleChanged, sha256_text,
)
from memory_store import MemoryDB  # noqa: E402
from retrieval import Retrieval  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def make_env():
    db = MemoryDB(":memory:")
    db.project("film_001", "测试短片")
    script = "第三场：林雨将钥匙交给陈明。"
    doc = db.document("film_001", "剧本/第三场.txt", script)
    chunk = db.chunk("film_001", doc, script, scene_id="scene_3")
    db.scene("film_001", "scene_1", 1)
    db.scene("film_001", "scene_3", 3)
    db.entity("film_001", "钥匙", "prop", "钥匙")
    db.entity("film_001", "林雨", "character", "林雨")
    db.entity("film_001", "陈明", "character", "陈明")
    return db, chunk


class TestKnowledgeLoader(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "规则.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("规则版本 1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_and_hash(self):
        kl = KnowledgeLoader(self.root)
        v = kl.load("规则.md")
        self.assertEqual(v.sha256, sha256_text("规则版本 1"))
        self.assertEqual(v.content, "规则版本 1")

    def test_cache_reused_when_unchanged(self):
        kl = KnowledgeLoader(self.root)
        v1 = kl.load("规则.md")
        v2 = kl.load("规则.md")
        self.assertIs(v1, v2, "未变化时应复用同一对象")

    def test_hash_change_invalidates_cache(self):
        kl = KnowledgeLoader(self.root)
        v1 = kl.load("规则.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("规则版本 2")
        v2 = kl.load("规则.md")
        self.assertNotEqual(v1.sha256, v2.sha256)
        self.assertEqual(v2.content, "规则版本 2")

    def test_require_approved_blocks_unreviewed_change(self):
        kl = KnowledgeLoader(self.root)
        approved = {"规则.md": sha256_text("规则版本 1")}
        kl.load("规则.md", approved_hashes=approved, require_approved=True)

        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("规则版本 2（未审核）")
        with self.assertRaises(RuleChanged) as ctx:
            kl.load("规则.md", approved_hashes=approved, require_approved=True)
        self.assertEqual(ctx.exception.error_code, "RULE_CHANGED_UNAPPROVED")

    def test_missing_file(self):
        kl = KnowledgeLoader(self.root)
        with self.assertRaises(FileNotFoundError):
            kl.load("不存在.md")

    def test_path_traversal_blocked(self):
        kl = KnowledgeLoader(self.root)
        with self.assertRaises(ValueError):
            kl.load("../../etc/passwd")

    def test_snapshot_and_diff(self):
        """snapshot 只扫项目规范白名单，改真实规范文件验证 diff。"""
        kl = KnowledgeLoader(ROOT)
        snap = kl.snapshot()
        self.assertIn("规范/流程状态机.md", snap)
        d = kl.diff_against(snap)
        self.assertFalse(d["has_change"])

        # 模拟已审核清单落后于当前（哈希不同）
        stale = dict(snap)
        stale["规范/流程状态机.md"] = "0" * 64
        d2 = kl.diff_against(stale)
        self.assertTrue(d2["has_change"])
        self.assertIn("规范/流程状态机.md", d2["changed"])

    def test_snapshot_added_and_removed(self):
        kl = KnowledgeLoader(ROOT)
        approved = {"规范/流程状态机.md": "0" * 64,
                    "规范/已删除的旧规范.md": "1" * 64}
        d = kl.diff_against(approved)
        self.assertIn("规范/流程状态机.md", d["changed"])
        self.assertIn("规范/已删除的旧规范.md", d["removed"])

    def test_rule_cache_raises_on_change(self):
        kl = KnowledgeLoader(self.root)
        rc = RuleCache(kl)
        rc.get("规则.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("又改了")
        with self.assertRaises(RuleChanged) as ctx:
            rc.get("规则.md")
        self.assertEqual(ctx.exception.error_code, "RULE_CACHE_INVALIDATED")

    def test_project_rules_loadable(self):
        """真实项目规范文件应能加载并哈希。"""
        kl = KnowledgeLoader(ROOT)
        v = kl.load("规范/流程状态机.md")
        self.assertTrue(v.sha256)
        self.assertGreater(len(v.content), 100)


class TestRetrieval(unittest.TestCase):

    def setUp(self):
        self.db, self.chunk = make_env()
        self.r = Retrieval(self.db)
        ev = self.db.propose_event(
            "film_001", "scene_1", "initial_state",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": None, "new": "林雨"}], self.chunk)
        self.db.approve_event("film_001", ev, actor="human")

    def tearDown(self):
        self.db.close()

    def test_state_as_of_scene(self):
        s1 = self.r.state_as_of_scene("film_001", "scene_1")
        self.assertEqual(len(s1), 1)
        self.assertEqual(s1[0]["value"], "林雨")

    def test_entity_state_at_scene(self):
        self.assertEqual(
            self.r.entity_state("film_001", "钥匙", "scene_3",
                                field="owner"), "林雨")
        self.assertEqual(
            self.r.entity_state("film_001", "钥匙", "scene_3"),
            {"owner": "林雨"})

    def test_entity_timeline(self):
        tl = self.r.entity_timeline("film_001", "钥匙")
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["scene_order"], 1)

    def test_scene_events_carry_evidence(self):
        evs = self.r.scene_events("film_001", "scene_1")
        self.assertTrue(evs)
        self.assertIn("钥匙", evs[0]["evidence"]["content"])

    def test_gather_shape(self):
        g = self.r.gather("film_001", stage="storyboard",
                          scene_id="scene_3", entity_ids=["钥匙"],
                          query="钥匙")
        self.assertIn("canonical_state", g)
        self.assertIn("source_evidence", g)
        self.assertIn("unresolved_conflicts", g)
        self.assertEqual(g["canonical_state"][0]["value"], "林雨")


class TestContextBuilder(unittest.TestCase):

    def setUp(self):
        self.db, self.chunk = make_env()
        ev = self.db.propose_event(
            "film_001", "scene_1", "initial_state",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": None, "new": "林雨"}], self.chunk)
        self.db.approve_event("film_001", ev, actor="human")

    def tearDown(self):
        self.db.close()

    def test_build_includes_rules_with_hash(self):
        loader = KnowledgeLoader(ROOT)
        cb = ContextBuilder(self.db, loader, token_budget=100000)
        pkg = cb.build("film_001", stage="storyboard", scene_id="scene_3",
                       rule_paths=["规范/流程状态机.md"],
                       script_chunks=[self.chunk])
        self.assertTrue(pkg.applicable_rules)
        self.assertTrue(pkg.applicable_rules[0]["sha256"])
        self.assertTrue(pkg.source_script)
        self.assertTrue(pkg.canonical_state)

    def test_render_has_citation_ids(self):
        loader = KnowledgeLoader(ROOT)
        cb = ContextBuilder(self.db, loader, token_budget=100000)
        pkg = cb.build("film_001", stage="storyboard", scene_id="scene_3",
                       script_chunks=[self.chunk])
        text = cb.render(pkg)
        self.assertIn(self.chunk, text)
        self.assertIn("已确认状态", text)

    def test_budget_trims_low_priority_first(self):
        loader = KnowledgeLoader(ROOT)
        self.db.summary("film_001", "episode", "一集摘要" * 200, [self.chunk])
        cb0 = ContextBuilder(self.db, loader, token_budget=10 ** 6)
        full = cb0.build("film_001", stage="storyboard", scene_id="scene_3",
                         rule_paths=["规范/流程状态机.md"],
                         script_chunks=[self.chunk], query="钥匙")
        self.assertTrue(full.summaries, "全量构建应包含摘要")
        self.assertTrue(full.retrieved_memories, "全量构建应含语义召回")

        # 预算够硬约束 + 少量余量：只够裁掉低优先级的摘要与泛语义
        layers = full.budget["layers"]
        keep = (layers["source_script"] + layers["applicable_rules"]
                + layers["canonical_state"] + layers["source_evidence"])
        cb = ContextBuilder(self.db, loader, token_budget=full.budget["used"] - 100)
        pkg = cb.build("film_001", stage="storyboard", scene_id="scene_3",
                       rule_paths=["规范/流程状态机.md"],
                       script_chunks=[self.chunk], query="钥匙")
        self.assertTrue(pkg.source_script, "剧本原文不得被裁")
        self.assertTrue(pkg.applicable_rules, "规则不得被裁")
        self.assertTrue(pkg.canonical_state, "必需状态不得被裁")
        self.assertTrue(any("裁减" in n or "预算不足" in n for n in pkg.notes))

    def test_hard_constraints_never_silently_dropped(self):
        loader = KnowledgeLoader(ROOT)
        cb = ContextBuilder(self.db, loader, token_budget=1)
        pkg = cb.build("film_001", stage="storyboard", scene_id="scene_3",
                       rule_paths=["规范/流程状态机.md"],
                       script_chunks=[self.chunk])
        self.assertTrue(pkg.source_script)
        self.assertTrue(pkg.applicable_rules)
        self.assertTrue(any("预算不足" in n for n in pkg.notes),
                        "超预算时必须主动提示，而非静默")

    def test_summaries_go_stale_after_script_change(self):
        sid = self.db.summary("film_001", "episode", "摘要", [self.chunk])
        self.assertEqual(len(self.db.active_summaries("film_001")), 1)
        # 原文变化 → 引用该文档块的摘要失效
        doc = self.db.db.execute(
            "SELECT document_id FROM documents WHERE project_id='film_001'"
        ).fetchone()["document_id"]
        self.db.document("film_001", "剧本/第三场.txt", "第三场：改写了。")
        self.assertEqual(len(self.db.active_summaries("film_001")), 0)
        # 失效的摘要不得再作为权威上下文
        self.assertFalse(any(s["status"] != "stale"
                             for s in self.db.active_summaries("film_001")))

    def test_summaries_stale_marks_by_source_ref(self):
        self.db.summary("film_001", "episode", "摘要A", [self.chunk])
        self.db.summary("film_001", "episode", "摘要B", ["chunk_不相关"])
        n = self.db.mark_summaries_stale("film_001", self.chunk)
        self.assertEqual(n, 1)
        active = self.db.active_summaries("film_001")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "摘要B")

    def test_unresolved_conflicts_surfaced(self):
        self.db.propose_event(
            "film_001", "scene_3", "prop_transfer",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": "林雨", "new": "陈明"}], self.chunk,
            idempotency_key="pending-1")
        cb = ContextBuilder(self.db, token_budget=100000)
        pkg = cb.build("film_001", stage="storyboard", scene_id="scene_3")
        self.assertTrue(pkg.unresolved_conflicts)
        text = cb.render(pkg)
        self.assertIn("未解决冲突", text)
        self.assertIn("不得作为已确认事实使用", text)


class TestTokenEstimate(unittest.TestCase):

    def test_estimate(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("中文测试"), 0)
        self.assertGreater(estimate_tokens("hello world"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
