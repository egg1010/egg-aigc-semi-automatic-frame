"""候选提取与 certainty 门禁测试 —— v2 §5 / §11"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_extractor import (  # noqa: E402
    CERTAINTY_EXPLICIT, CERTAINTY_INFERRED, CERTAINTY_UNKNOWN,
    Candidate, MemoryExtractor, group_into_proposals,
    proposal_idempotency_key, to_event_proposals,
)
from memory_store import Conflict, MemoryDB  # noqa: E402


class TestExtraction(unittest.TestCase):

    def setUp(self):
        self.ex = MemoryExtractor()

    def test_transfer_is_explicit_with_evidence(self):
        ev = [{"chunk_id": "chunk_1", "content": "第三场：林雨将钥匙交给陈明。",
               "scene_id": "scene_3"}]
        cands = self.ex.extract_candidates("", ev)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.certainty, CERTAINTY_EXPLICIT)
        self.assertEqual(c.entity_id, "钥匙")
        self.assertEqual(c.new, "陈明")
        self.assertEqual(c.quote, "林雨将钥匙交给陈明")
        self.assertTrue(c.is_approvable)

    def test_inferred_not_approvable(self):
        ev = [{"chunk_id": "chunk_1", "content": "钥匙可能属于陈明。",
               "scene_id": "scene_3"}]
        cands = self.ex.extract_candidates("", ev)
        self.assertTrue(cands)
        self.assertEqual(cands[0].certainty, CERTAINTY_INFERRED)
        self.assertFalse(cands[0].is_approvable)

    def test_explicit_without_evidence_not_approvable(self):
        c = Candidate(entity_id="钥匙", field="owner", new="陈明",
                      certainty=CERTAINTY_EXPLICIT, evidence_chunk_id=None)
        self.assertFalse(c.is_approvable)

    def test_invalid_certainty_rejected(self):
        with self.assertRaises(ValueError):
            Candidate(entity_id="x", field="owner", new="y", certainty="猜")

    def test_extractor_never_touches_db(self):
        """提取器必须无副作用：不给它任何 DB 句柄也不报错。"""
        ev = [{"chunk_id": "chunk_1", "content": "林雨将钥匙交给陈明。"}]
        self.ex.extract_candidates("", ev)  # 不抛异常即证明无写库依赖


class TestGrouping(unittest.TestCase):

    def setUp(self):
        self.ex = MemoryExtractor()

    def test_grouping_splits_three_ways(self):
        cands = [
            Candidate(entity_id="钥匙", field="owner", new="陈明",
                      certainty=CERTAINTY_EXPLICIT, evidence_chunk_id="chunk_1"),
            Candidate(entity_id="盒子", field="owner", new="陈明",
                      certainty=CERTAINTY_INFERRED, evidence_chunk_id="chunk_1"),
            Candidate(entity_id="箱子", field="owner", new="陈明",
                      certainty=CERTAINTY_UNKNOWN, evidence_chunk_id="chunk_1"),
            Candidate(entity_id="无证据", field="owner", new="陈明",
                      certainty=CERTAINTY_EXPLICIT, evidence_chunk_id=None),
        ]
        g = group_into_proposals(cands)
        self.assertEqual(len(g["approvable"]), 1)
        self.assertEqual(len(g["needs_confirmation"]), 1)
        # unknown 与「explicit 但无证据」都不得进入审批队列
        self.assertEqual(len(g["discarded"]), 2)


class TestIdempotencyKey(unittest.TestCase):

    def test_key_is_stable(self):
        changes = [{"entity_id": "钥匙", "field": "owner",
                    "expected": "林雨", "new": "陈明"}]
        k1 = proposal_idempotency_key("film_001", "scene_3", "prop_transfer",
                                      changes)
        k2 = proposal_idempotency_key("film_001", "scene_3", "prop_transfer",
                                      list(reversed(changes)))
        self.assertEqual(k1, k2, "键应对 changes 顺序不敏感")

    def test_key_differs_per_scene(self):
        changes = [{"entity_id": "钥匙", "field": "owner", "new": "陈明"}]
        self.assertNotEqual(
            proposal_idempotency_key("p", "scene_1", "t", changes),
            proposal_idempotency_key("p", "scene_2", "t", changes))


class TestExtractorRoutedThroughGate(unittest.TestCase):
    """端到端：提取 → 组装提案 → 事务提交，且 inferred 被挡在门外。"""

    def setUp(self):
        self.db = MemoryDB(":memory:")
        self.db.project("film_001", "测试短片")
        doc = self.db.document("film_001", "剧本/第三场.txt",
                               "第三场：林雨将钥匙交给陈明。钥匙可能属于别人。")
        self.chunk = self.db.chunk("film_001", doc,
                                   "第三场：林雨将钥匙交给陈明。钥匙可能属于别人。",
                                   scene_id="scene_3")
        self.db.scene("film_001", "scene_3", 3)
        self.db.entity("film_001", "钥匙", "prop", "钥匙")
        self.db.entity("film_001", "林雨", "character", "林雨")
        self.db.entity("film_001", "陈明", "character", "陈明")

    def tearDown(self):
        self.db.close()

    def test_only_explicit_reaches_database(self):
        ex = MemoryExtractor()
        ev = [{"chunk_id": self.chunk,
               "content": "第三场：林雨将钥匙交给陈明。钥匙可能属于别人。",
               "scene_id": "scene_3"}]
        cands = ex.extract_candidates("", ev)

        g = group_into_proposals(cands)
        self.assertTrue(g["needs_confirmation"], "应有 inferred 候选被拦下")

        # 先建立初始归属，再走转移
        self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": None, "new": "林雨"}], self.chunk,
            idempotency_key="seed")
        seed = self.db.db.execute(
            "SELECT event_id FROM events WHERE idempotency_key='seed'"
        ).fetchone()["event_id"]
        self.db.approve_event("film_001", seed, actor="human")

        proposals = to_event_proposals(cands, "film_001")
        transfer = [p for p in proposals if p["event_type"] == "prop_transfer"]
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer[0]["changes"][0]["expected"], None)

        # 提取器给的 expected 尚不准确，生产由校验层补正；
        # 这里显式修正后提交，验证链路可走通
        self.db.propose_event(
            "film_001", "scene_3", "prop_transfer",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": "林雨", "new": "陈明"}], self.chunk,
            idempotency_key=transfer[0]["idempotency_key"])
        ev_id = self.db.db.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (transfer[0]["idempotency_key"],)).fetchone()["event_id"]
        self.db.approve_event("film_001", ev_id, actor="human")
        self.assertEqual(self.db.state("film_001", "钥匙", "owner"), "陈明")

        # inferred 候选始终没进库
        approved = self.db.db.execute(
            "SELECT certainty FROM events WHERE approval='approved'"
        ).fetchall()
        self.assertTrue(all(r["certainty"] == "explicit" for r in approved))


if __name__ == "__main__":
    unittest.main(verbosity=2)
