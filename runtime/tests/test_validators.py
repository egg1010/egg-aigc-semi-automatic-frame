"""校验层测试 —— v2 §6 九步次序 / §8.6 权限分级"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validators as V  # noqa: E402
from memory_store import MemoryDB  # noqa: E402


def make_env():
    db = MemoryDB(":memory:")
    db.project("film_001", "测试短片")
    script = "第三场：林雨将钥匙交给陈明。"
    doc = db.document("film_001", "剧本/第三场.txt", script)
    chunk = db.chunk("film_001", doc, script, scene_id="scene_3")
    db.scene("film_001", "scene_3", 3)
    db.entity("film_001", "钥匙", "prop", "钥匙")
    db.entity("film_001", "林雨", "character", "林雨")
    db.entity("film_001", "陈明", "character", "陈明")
    return db, chunk


class TestSchemaStep(unittest.TestCase):
    """第 1-2 步：JSON 有效性 + Schema"""

    def test_invalid_json(self):
        res = V.validate(None, stage="storyboard",
                         raw_text="{不是 json")
        self.assertFalse(res.ok)
        self.assertTrue(any(f.code == "JSON" and f.step == 1
                            for f in res.findings))

    def test_top_level_not_object(self):
        res = V.validate(None, stage="storyboard", raw_text="[1,2,3]")
        self.assertFalse(res.ok)
        self.assertTrue(any(f.step == 1 for f in res.findings))

    def test_valid_storyboard(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "递钥匙",
                           "camera_movement": "dolly",
                           "characters": ["林雨"]}],
                "source_refs": ["chunk_x"]}
        res = V.Result()
        self.assertTrue(V.validate_schema(
            inst, os.path.join(V.SCHEMA_DIR, "storyboard.schema.json"), res))
        self.assertTrue(res.ok)

    def test_missing_source_refs_is_error(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "递钥匙",
                           "camera_movement": "dolly", "characters": []}]}
        res = V.Result()
        V.validate_schema(inst, os.path.join(
            V.SCHEMA_DIR, "storyboard.schema.json"), res)
        self.assertFalse(res.ok)

    def test_unknown_stage_rejected(self):
        res = V.validate({}, stage="不存在的阶段")
        self.assertFalse(res.ok)
        self.assertTrue(any(f.code == "SCHEMA" and f.step == 2
                            for f in res.findings))


class TestReferenceStep(unittest.TestCase):
    """第 3 步：引用真实性必须查库"""

    def setUp(self):
        self.db, self.chunk = make_env()

    def tearDown(self):
        self.db.close()

    def test_source_ref_must_exist(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "递钥匙",
                           "camera_movement": "static",
                           "characters": ["林雨"]}],
                "source_refs": ["chunk_不存在"]}
        res = V.Result()
        V.validate_references(inst, self.db, "film_001", res, "storyboard")
        self.assertFalse(res.ok)
        self.assertTrue(any(f.code == "REF" and f.step == 3
                            for f in res.findings))

    def test_real_chunk_passes(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "递钥匙",
                           "camera_movement": "static",
                           "characters": ["林雨"]}],
                "source_refs": [self.chunk]}
        res = V.Result()
        V.validate_references(inst, self.db, "film_001", res, "storyboard")
        self.assertTrue(res.ok)

    def test_unknown_entity_in_shot(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "递钥匙",
                           "camera_movement": "static",
                           "characters": ["查无此人"]}],
                "source_refs": [self.chunk]}
        res = V.Result()
        V.validate_references(inst, self.db, "film_001", res, "storyboard")
        self.assertFalse(res.ok)

    def test_unknown_scene(self):
        inst = {"scene_id": "scene_404", "shots": [
            {"shot_id": "s1", "description": "x",
             "camera_movement": "static", "characters": []}],
            "source_refs": [self.chunk]}
        res = V.Result()
        V.validate_references(inst, self.db, "film_001", res, "storyboard")
        self.assertFalse(res.ok)


class TestContinuityStep(unittest.TestCase):
    """第 5 步：状态连续性"""

    def setUp(self):
        self.db, self.chunk = make_env()

    def tearDown(self):
        self.db.close()

    def test_entity_without_prior_state_warns(self):
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "x",
                           "camera_movement": "static",
                           "characters": [], "entity_refs": ["钥匙"]}]}
        res = V.Result()
        V.validate_continuity(inst, self.db, "film_001", res)
        self.assertTrue(any(f.code == "CONT" and f.level == "WARN"
                            for f in res.findings))

    def test_entity_with_state_no_warning(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": None, "new": "林雨"}], self.chunk)
        self.db.approve_event("film_001", ev)
        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "x",
                           "camera_movement": "static",
                           "characters": [], "entity_refs": ["钥匙"]}]}
        res = V.Result()
        V.validate_continuity(inst, self.db, "film_001", res)
        self.assertFalse(any(f.code == "CONT" for f in res.findings))


class TestPermissionStep(unittest.TestCase):
    """第 6 步：授权分级（§8.6）"""

    def test_style_cannot_change_content(self):
        res = V.Result()
        V.validate_permission({"change_scope": "content"}, "style", res)
        self.assertFalse(res.ok)
        self.assertTrue(any(f.code == "PERM" and f.step == 6
                            for f in res.findings))

    def test_content_can_change_content(self):
        res = V.Result()
        V.validate_permission({"change_scope": "content"}, "content", res)
        self.assertTrue(res.ok)

    def test_style_can_change_style(self):
        res = V.Result()
        V.validate_permission({"change_scope": "format"}, "style", res)
        self.assertTrue(res.ok)

    def test_invalid_authorization(self):
        res = V.Result()
        V.validate_permission({}, "随便改", res)
        self.assertFalse(res.ok)

    def test_declared_scope_required_when_known(self):
        res = V.Result()
        V.validate_permission({"change_scope": "剧本重写"}, "style", res)
        self.assertFalse(res.ok)


class TestEventProposalGates(unittest.TestCase):
    """第 4 步 + §5 certainty 拦截"""

    def test_empty_changes_is_error(self):
        inst = {"project_id": "film_001", "scene_id": "scene_1",
                "event_type": "prop_transfer",
                "evidence_chunk_id": "chunk_1", "changes": []}
        res = V.validate(inst, stage="event_proposal")
        self.assertFalse(res.ok)
        self.assertTrue(any(f.code == "CONT" and f.step == 4
                            for f in res.findings))

    def test_inferred_gets_warning(self):
        inst = {"project_id": "film_001", "scene_id": "scene_1",
                "event_type": "state_change", "evidence_chunk_id": "chunk_1",
                "certainty": "inferred",
                "changes": [{"entity_id": "钥匙", "field": "owner", "new": "陈明"}]}
        res = V.validate(inst, stage="event_proposal")
        self.assertTrue(any(f.code == "CONT" and f.level == "WARN"
                            for f in res.findings))


class TestSoftConstraintNote(unittest.TestCase):
    """§6.2：美学判断不得伪装成机器可验证约束"""

    def test_soft_note_present(self):
        inst = {"scene_id": "scene_1",
                "shots": [{"shot_id": "s1", "description": "x",
                           "camera_movement": "static", "characters": []}],
                "source_refs": ["c"]}
        res = V.validate(inst, stage="storyboard")
        self.assertTrue(any(f.code == "SOFT" for f in res.findings))

    def test_asset_review_note(self):
        inst = {"project_id": "film_001",
                "assets": [{"asset_id": "a1", "entity_id": "钥匙",
                            "prompt": "一把铜钥匙"}]}
        res = V.validate(inst, stage="asset_inventory")
        self.assertTrue(any(f.code == "GATE" and f.step == 8
                            for f in res.findings))


class TestFullPipeline(unittest.TestCase):
    """端到端：提取 → 校验 → 事务提交"""

    def setUp(self):
        self.db, self.chunk = make_env()

    def tearDown(self):
        self.db.close()

    def test_storyboard_validated_against_real_state(self):
        ev = self.db.propose_event(
            "film_001", "scene_3", "initial_state",
            [{"entity_id": "钥匙", "field": "owner",
              "expected": None, "new": "林雨"}], self.chunk)
        self.db.approve_event("film_001", ev, actor="human")

        inst = {"scene_id": "scene_3",
                "shots": [{"shot_id": "s1", "description": "林雨递出钥匙",
                           "camera_movement": "dolly",
                           "characters": ["林雨"], "entity_refs": ["钥匙"]}],
                "source_refs": [self.chunk]}
        res = V.validate(inst, stage="storyboard", db=self.db,
                         project_id="film_001")
        self.assertTrue(res.ok, [f.to_dict() for f in res.findings])
        # 状态可查，供下一阶段装配上下文
        self.assertEqual(self.db.state("film_001", "钥匙", "owner"), "林雨")


if __name__ == "__main__":
    unittest.main(verbosity=2)
