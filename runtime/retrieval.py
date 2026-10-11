"""检索 —— 架构改造方案 v2 §4.2 / §4.3 / §10.4

原则：**精确状态优先于语义检索**。
    「钥匙在第 12 场之后由谁持有」这类确定性问题，必须查 SQL，
    不能只靠向量库回答。

检索次序（§4.2）：
    1 SQL 精确定位场景与实体，按故事时间取该场之前的已批准状态
    2 按参与角色/道具/场景检索相关事件与来源
    3 检索当前版本的项目规范
    4 相关事件不足时，用 FTS5 / LIKE 补充原文
    5 （可选）Embedding 补召回 —— 第二阶段
    6 交 context_builder 按 Token 预算组装
"""

from __future__ import annotations


class Retrieval:
    def __init__(self, db):
        self.db = db

    # -- 第 1 步：精确状态 ------------------------------------------------

    def state_as_of_scene(self, project_id: str, scene_id: str,
                          entity_ids=None) -> list[dict]:
        """该场景结束时的已批准状态快照。

        注意：state_current 是「头部状态」，因此这里按 scene_order 过滤，
        正确回答「第 N 场结束时某物归谁」。
        """
        row = self.db.db.execute(
            "SELECT scene_order FROM scenes WHERE project_id=? AND scene_id=?",
            (project_id, scene_id)).fetchone()
        if row is None:
            return []
        order = row["scene_order"]
        states = self.db.state(project_id, scene_order=order)
        if entity_ids:
            keep = set(entity_ids)
            states = [s for s in states if s["entity_id"] in keep]
        return states

    def entity_state(self, project_id: str, entity_id: str,
                     scene_id: str | None = None,
                     field: str | None = None):
        """某实体在指定场景时的状态。

        field 给定 → 返回标量值；否则返回 {字段: 值} 字典。
        """
        order = None
        if scene_id:
            row = self.db.db.execute(
                "SELECT scene_order FROM scenes WHERE project_id=? AND scene_id=?",
                (project_id, scene_id)).fetchone()
            order = row["scene_order"] if row else None
        if field:
            return self.db.state(project_id, entity_id, field,
                                 scene_order=order)
        rows = self.db.state(project_id, entity_id, scene_order=order)
        if not isinstance(rows, list):
            return rows
        return {r["field"]: r["value"] for r in rows}

    # -- 第 2 步：场景相关事件与来源 --------------------------------------

    def scene_events(self, project_id: str, scene_id: str,
                     approved_only: bool = True) -> list[dict]:
        events = self.db.events_for_scene(project_id, scene_id)
        if approved_only:
            events = [e for e in events if e["approval"] == "approved"]
        for e in events:
            ev = self.db.evidence(project_id, e["evidence_chunk_id"])
            e["evidence"] = ev
        return events

    def entity_timeline(self, project_id: str, entity_id: str) -> list[dict]:
        """某实体的状态变更轨迹，按故事时间排序。"""
        rows = self.db.db.execute(
            "SELECT e.event_id, e.scene_id, e.event_type, e.changes_json,"
            " e.evidence_chunk_id, e.approved_at, s.scene_order"
            " FROM events e JOIN scenes s ON s.project_id=e.project_id"
            "   AND s.scene_id=e.scene_id"
            " WHERE e.project_id=? AND e.approval='approved'"
            " ORDER BY s.scene_order, e.event_order",
            (project_id,)).fetchall()
        import json as _json
        out = []
        for r in rows:
            changes = _json.loads(r["changes_json"])
            if any(c.get("entity_id") == entity_id for c in changes):
                out.append({"event_id": r["event_id"], "scene_id": r["scene_id"],
                            "scene_order": r["scene_order"],
                            "event_type": r["event_type"],
                            "changes": [c for c in changes
                                        if c.get("entity_id") == entity_id],
                            "evidence_chunk_id": r["evidence_chunk_id"]})
        return out

    # -- 第 4 步：原文召回 ------------------------------------------------

    def search_text(self, project_id: str, query: str, limit: int = 10):
        return self.db.search(project_id, query, limit)

    def evidence_by_ids(self, project_id: str, chunk_ids):
        out = []
        for cid in chunk_ids:
            ev = self.db.evidence(project_id, cid)
            if ev:
                out.append(ev)
        return out

    # -- 综合：为某阶段装配检索结果 ---------------------------------------

    def gather(self, project_id: str, *, stage: str,
               scene_id: str | None = None,
               entity_ids=None, query: str | None = None,
               limit: int = 8) -> dict:
        """按 §4.2 次序汇总，产出供 context_builder 消费的原始素材。"""
        result = {"canonical_state": [], "source_evidence": [],
                  "retrieved_memories": [], "unresolved_conflicts": []}

        if scene_id:
            result["canonical_state"] = self.state_as_of_scene(
                project_id, scene_id, entity_ids)
            events = self.scene_events(project_id, scene_id)
            seen = set()
            for e in events:
                ev = e.get("evidence")
                if ev and ev["chunk_id"] not in seen:
                    seen.add(ev["chunk_id"])
                    result["source_evidence"].append({
                        "chunk_id": ev["chunk_id"],
                        "quote": _first_line(ev["content"]),
                        "path": ev["path"],
                    })

        if entity_ids:
            for eid in entity_ids:
                tl = self.entity_timeline(project_id, eid)
                for item in tl[-3:]:
                    result["retrieved_memories"].append(
                        {"kind": "entity_timeline", "entity_id": eid,
                         "scene_id": item["scene_id"],
                         "changes": item["changes"]})

        if query:
            for hit in self.search_text(project_id, query, limit):
                if hit["chunk_id"] in {s["chunk_id"]
                                       for s in result["source_evidence"]}:
                    continue
                result["retrieved_memories"].append({
                    "kind": "textual", "chunk_id": hit["chunk_id"],
                    "quote": _first_line(hit["content"]),
                    "path": hit["path"], "scene_id": hit["scene_id"]})

        # 未解决的冲突（提案未批准的）
        rows = self.db.db.execute(
            "SELECT event_id, scene_id, event_type FROM events"
            " WHERE project_id=? AND approval='proposed'", (project_id,)).fetchall()
        result["unresolved_conflicts"] = [dict(r) for r in rows]
        return result


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()
    s = line[0] if line else ""
    return s[:limit]
