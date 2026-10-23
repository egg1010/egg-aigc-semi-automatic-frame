"""SQLite 记忆层 Repository —— egg-aigc-semi-automatic-frame

依据：架构改造方案 v2 §3（Schema 与事务边界）、§10.1-10.3（工程保障）

核心边界（§0.1）：
    AI 只提出事实变更和生成结果；**只有本模块能够提交数据库状态**。
    业务事实修改必须有原始证据（evidence_chunk_id）；
    冲突或证据不足的提案不得进入已确认事实集合。

安全约定：
    - 所有 SQL 使用绑定参数，绝不拼接用户输入或模型输出
    - SQLite 里的原文/规则是「数据」，不是可执行指令（抗提示词注入）
    - 不向外部暴露 Connection，避免绕过事务边界
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "migrations", "001_initial.sql")

# certainty 分级（§5）：仅 explicit 可直接进入审批队列
CERTAINTY_LEVELS = ("explicit", "inferred", "unknown")

# 可被审批的事件状态
APPROVAL_STATES = ("proposed", "approved", "rejected")


class Conflict(Exception):
    """状态冲突：预期旧值与当前状态不一致，或历史回写需重放。"""

    def __init__(self, message, *, entity_id=None, field=None,
                 expected=None, found=None, error_code="CONFLICT_STATE"):
        super().__init__(message)
        self.message = message
        self.entity_id = entity_id
        self.field = field
        self.expected = expected
        self.found = found
        self.error_code = error_code

    def to_dict(self):
        return {
            "error_code": self.error_code,
            "message": self.message,
            "entity_id": self.entity_id,
            "field": self.field,
            "expected": self.expected,
            "found": self.found,
        }


class EvidenceError(Exception):
    """证据缺失或不合法：禁止无原文证据的事实写入（§3.2 第 2 条）。"""

    def __init__(self, message, *, error_code="EVIDENCE_MISSING"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code

    def to_dict(self):
        return {"error_code": self.error_code, "message": self.message}


class IdempotencyError(Exception):
    """同一 idempotency_key 提交了不同 payload。"""

    def __init__(self, message, *, error_code="IDEMPOTENCY_PAYLOAD_MISMATCH"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code

    def to_dict(self):
        return {"error_code": self.error_code, "message": self.message}


class PermissionError_(Exception):
    """越权操作：例如 AI 自行审批人工关口（§11 人工审批）。"""

    def __init__(self, message, *, error_code="PERMISSION_DENIED"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code

    def to_dict(self):
        return {"error_code": self.error_code, "message": self.message}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _j(value) -> str:
    """稳定序列化：便于 idempotency payload 比较。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class MemoryDB:
    """SQLite 记忆层。一个项目一个文件（单机版，§10.1）。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # §3.4 初始化 PRAGMA
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 3000")
        self._migrate()

    # -- 生命周期 ---------------------------------------------------------

    def _migrate(self):
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            self.db.executescript(fh.read())
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @staticmethod
    def project_db_path(data_root: str, project_id: str) -> str:
        """§2：AI数据/data/projects/<project_id>/project.db"""
        _assert_safe_identifier(project_id, "project_id")
        return os.path.join(data_root, "projects", project_id, "project.db")

    # -- 项目 -------------------------------------------------------------

    def project(self, project_id: str, name: str) -> str:
        _assert_safe_identifier(project_id, "project_id")
        self.db.execute(
            "INSERT OR IGNORE INTO projects(project_id, name) VALUES(?, ?)",
            (project_id, name))
        self.db.commit()
        return project_id

    # -- 原文与分块 -------------------------------------------------------

    def document(self, project_id: str, path: str, content: str,
                 *, expect_project: str | None = None) -> str:
        """写入原文。相同 path+sha256 不重复写入；内容变化生成新 version。

        返回 document_id。
        """
        self._require_project(project_id)
        digest = _sha256(content)
        row = self.db.execute(
            "SELECT document_id, version FROM documents "
            "WHERE project_id=? AND path=? AND sha256=?",
            (project_id, path, digest)).fetchone()
        if row:
            return row["document_id"]

        prev = self.db.execute(
            "SELECT MAX(version) AS v FROM documents "
            "WHERE project_id=? AND path=?", (project_id, path)).fetchone()
        version = (prev["v"] or 0) + 1
        doc_id = _new_id("doc")
        self.db.execute(
            "INSERT INTO documents(document_id, project_id, path, version,"
            " sha256, content) VALUES(?, ?, ?, ?, ?, ?)",
            (doc_id, project_id, path, version, digest, content))
        self._audit(project_id, "document.write", doc_id,
                    {"path": path, "version": version, "sha256": digest})
        self.db.commit()
        # 原文版本变化 → 依赖该路径**历史版本**的摘要全部失效（§5）
        self._invalidate_summaries_by_path(project_id, path)
        return doc_id

    def chunk(self, project_id: str, document_id: str, content: str,
              *, ordinal: int = 0, scene_id: str | None = None,
              source_start: int = 0, source_end: int | None = None) -> str:
        """切块写入，保留到原文的字符偏移以便定位（§4.1）。"""
        self._require_project(project_id)
        # 跨项目所有权检查：禁止把 A 项目的块挂到 B 项目文档下
        row = self.db.execute(
            "SELECT project_id FROM documents WHERE document_id=?",
            (document_id,)).fetchone()
        if row is None:
            raise EvidenceError(f"文档不存在：{document_id}",
                                error_code="DOCUMENT_NOT_FOUND")
        if row["project_id"] != project_id:
            raise PermissionError_(
                f"文档 {document_id} 属于项目 {row['project_id']}，"
                f"不能挂到项目 {project_id}",
                error_code="CROSS_PROJECT_DENIED")

        chunk_id = _new_id("chunk")
        if source_end is None:
            source_end = source_start + len(content)
        self.db.execute(
            "INSERT INTO chunks(chunk_id, project_id, document_id, ordinal,"
            " scene_id, source_start, source_end, content)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, project_id, document_id, ordinal, scene_id,
             source_start, source_end, content))
        self.db.commit()
        return chunk_id

    # -- 实体与场次 -------------------------------------------------------

    def entity(self, project_id: str, entity_id: str, kind: str,
               name: str) -> str:
        if kind not in ("character", "prop", "location", "asset"):
            raise ValueError(f"kind 非法：{kind}")
        self._require_project(project_id)
        self.db.execute(
            "INSERT OR IGNORE INTO entities(project_id, entity_id, kind, name)"
            " VALUES(?, ?, ?, ?)", (project_id, entity_id, kind, name))
        self.db.commit()
        return entity_id

    def scene(self, project_id: str, scene_id: str, scene_order: int,
              title: str | None = None) -> str:
        self._require_project(project_id)
        self.db.execute(
            "INSERT OR IGNORE INTO scenes(project_id, scene_id, scene_order,"
            " title) VALUES(?, ?, ?, ?)",
            (project_id, scene_id, scene_order, title))
        self.db.commit()
        return scene_id

    # -- 事件：提案 → 校验 → 审批 → 提交（§3.3 / §3.4）-------------------

    def propose_event(self, project_id: str, scene_id: str, event_type: str,
                      changes: list[dict], evidence_chunk_id: str,
                      *, idempotency_key: str | None = None,
                      event_order: int = 0,
                      certainty: str = "explicit") -> str:
        """写入 approval='proposed' 的候选事件。**不修改 state_current**。

        changes 每项：{"entity_id", "field", "expected", "new"}
        """
        self._require_project(project_id)
        if certainty not in CERTAINTY_LEVELS:
            raise ValueError(f"certainty 非法：{certainty}")
        if not changes:
            raise ValueError("changes 不能为空")

        # 证据必须真实存在且属于同一项目（§3.2 第 2 条）
        # 注：单库隔离下，别的项目的 chunk 在本库不可见 → 一律判为证据不可用。
        # 若未来改为多项目共库，这里仍会按 project_id 拦截跨项目引用。
        ev = self.db.execute(
            "SELECT project_id FROM chunks WHERE chunk_id=?",
            (evidence_chunk_id,)).fetchone()
        if ev is None:
            if self._chunk_exists_in_other_db(evidence_chunk_id, project_id):
                raise EvidenceError(
                    f"证据块 {evidence_chunk_id} 不属于项目 {project_id}",
                    error_code="EVIDENCE_CROSS_PROJECT")
            raise EvidenceError(
                f"证据块不存在：{evidence_chunk_id}",
                error_code="EVIDENCE_NOT_FOUND")
        if ev["project_id"] != project_id:
            raise EvidenceError(
                f"证据块 {evidence_chunk_id} 不属于项目 {project_id}",
                error_code="EVIDENCE_CROSS_PROJECT")

        # 场次必须存在且属于同一项目（§3.2 第 1 条）
        sc = self.db.execute(
            "SELECT scene_order FROM scenes WHERE project_id=? AND scene_id=?",
            (project_id, scene_id)).fetchone()
        if sc is None:
            raise Conflict(f"场次不存在：{scene_id}", error_code="SCENE_NOT_FOUND")

        for c in changes:
            for key in ("entity_id", "field", "new"):
                if key not in c:
                    raise ValueError(f"change 缺少字段 {key}：{c}")
            ent = self.db.execute(
                "SELECT 1 FROM entities WHERE project_id=? AND entity_id=?",
                (project_id, c["entity_id"])).fetchone()
            if ent is None:
                raise Conflict(f"实体不存在：{c['entity_id']}",
                               error_code="ENTITY_NOT_FOUND")

        if idempotency_key is None:
            idempotency_key = _new_id("idem")

        payload = _j(changes)
        existing = self.db.execute(
            "SELECT event_id, changes_json FROM events "
            "WHERE project_id=? AND idempotency_key=?",
            (project_id, idempotency_key)).fetchone()
        if existing:
            if existing["changes_json"] != payload:
                raise IdempotencyError(
                    f"幂等键 {idempotency_key} 已存在但 payload 不同")
            return existing["event_id"]

        event_id = _new_id("evt")
        self.db.execute(
            "INSERT INTO events(event_id, project_id, scene_id, event_type,"
            " event_order, evidence_chunk_id, idempotency_key, changes_json,"
            " certainty, approval) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed')",
            (event_id, project_id, scene_id, event_type, event_order,
             evidence_chunk_id, idempotency_key, payload, certainty))
        self._audit(project_id, "event.propose", event_id,
                    {"scene_id": scene_id, "event_type": event_type,
                     "certainty": certainty})
        self.db.commit()
        return event_id

    def approve_event(self, project_id: str, event_id: str, *,
                      actor: str = "system") -> None:
        """在单个事务内提交事件（§3.4）。

        次序：BEGIN IMMEDIATE → 全量校验 expected → 全量应用 → 标记 approved
              → 审计 → commit；任何异常 rollback 并抛出。
        """
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            ev = db.execute(
                "SELECT * FROM events WHERE project_id=? AND event_id=?",
                (project_id, event_id)).fetchone()
            if ev is None:
                raise Conflict(f"事件不存在：{event_id}",
                               error_code="EVENT_NOT_FOUND")

            # 幂等：已批准的事件重复审批不重复应用
            if ev["approval"] == "approved":
                db.rollback()
                return

            # inferred 默认不进正式状态（§5）
            if ev["certainty"] != "explicit":
                raise PermissionError_(
                    f"事件 {event_id} 的 certainty={ev['certainty']}，"
                    "非 explicit 事实不得直接进入正式状态，需用户确认",
                    error_code="CERTAINTY_NOT_EXPLICIT")

            changes = json.loads(ev["changes_json"])

            scene_row = db.execute(
                "SELECT scene_order FROM scenes WHERE project_id=? AND scene_id=?",
                (project_id, ev["scene_id"])).fetchone()
            scene_order = scene_row["scene_order"]

            # 第 1 步：先检查整个事件，再开始修改任何状态
            for c in changes:
                row = db.execute(
                    "SELECT value_json, last_scene_order FROM state_current "
                    "WHERE project_id=? AND entity_id=? AND field=?",
                    (project_id, c["entity_id"], c["field"])).fetchone()
                current = None if row is None else json.loads(row["value_json"])
                expected = c.get("expected")
                if current != expected:
                    raise Conflict(
                        f"{c['entity_id']}.{c['field']}: "
                        f"expected {expected!r}, found {current!r}",
                        entity_id=c["entity_id"], field=c["field"],
                        expected=expected, found=current)

                # 历史回写保护：不得覆盖故事时间上更晚的状态（§3.2 第 6 条）
                if row is not None and scene_order < row["last_scene_order"]:
                    raise Conflict(
                        f"{c['entity_id']}.{c['field']}: 场景 {ev['scene_id']}"
                        f"(order={scene_order}) 早于当前状态来源场景"
                        f"(order={row['last_scene_order']})，需重放后续事件"
                        "或创建分支版本",
                        entity_id=c["entity_id"], field=c["field"],
                        error_code="HISTORICAL_EDIT_REQUIRES_REPLAY")

            # 第 2 步：全部应用（任一失败整体回滚）
            for c in changes:
                self._upsert_state(project_id, c, event_id, scene_order)

            # 第 3 步：标记已批准 + 审计
            db.execute(
                "UPDATE events SET approval='approved',"
                " approved_at=datetime('now') WHERE event_id=?", (event_id,))
            self._audit(project_id, "event.approve", event_id,
                        {"actor": actor, "changes": changes,
                         "scene_order": scene_order})
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _upsert_state(self, project_id: str, change: dict, event_id: str,
                      scene_order: int) -> None:
        self.db.execute(
            "INSERT INTO state_current(project_id, entity_id, field,"
            " value_json, last_scene_order, version, source_event_id)"
            " VALUES(?, ?, ?, ?, ?, 1, ?)"
            " ON CONFLICT(project_id, entity_id, field) DO UPDATE SET"
            "   value_json=excluded.value_json,"
            "   last_scene_order=excluded.last_scene_order,"
            "   version=state_current.version+1,"
            "   source_event_id=excluded.source_event_id,"
            "   updated_at=datetime('now')",
            (project_id, change["entity_id"], change["field"],
             _j(change["new"]), scene_order, event_id))

    def reject_event(self, project_id: str, event_id: str, reason: str) -> None:
        self.db.execute(
            "UPDATE events SET approval='rejected' "
            "WHERE project_id=? AND event_id=?", (project_id, event_id))
        self._audit(project_id, "event.reject", event_id, {"reason": reason})
        self.db.commit()

    # -- 状态读取（故事时间定位，§4.2）-----------------------------------

    def state(self, project_id: str, entity_id: str | None = None,
              field: str | None = None, *, scene_order: int | None = None):
        """读取头部状态；给定 scene_order 时只取该场次及之前的已批准状态。"""
        self._require_project_exists(project_id)
        if entity_id and field:
            row = self.db.execute(
                "SELECT value_json, last_scene_order, version, source_event_id"
                " FROM state_current WHERE project_id=? AND entity_id=? AND field=?",
                (project_id, entity_id, field)).fetchone()
            if row is None:
                return None
            if scene_order is not None and row["last_scene_order"] > scene_order:
                return None
            return json.loads(row["value_json"])

        sql = ("SELECT entity_id, field, value_json, last_scene_order,"
               " version, source_event_id FROM state_current"
               " WHERE project_id=?")
        params = [project_id]
        if entity_id:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if scene_order is not None:
            sql += " AND last_scene_order<=?"
            params.append(scene_order)
        sql += " ORDER BY entity_id, field"
        rows = self.db.execute(sql, params).fetchall()
        return [{"entity_id": r["entity_id"], "field": r["field"],
                 "value": json.loads(r["value_json"]),
                 "last_scene_order": r["last_scene_order"],
                 "version": r["version"],
                 "source_event_id": r["source_event_id"]} for r in rows]

    def state_as_of(self, project_id: str, scene_order: int) -> list[dict]:
        """按故事时间取该场次结束时的状态快照（§4.2 第 1 步）。"""
        return self.state(project_id, scene_order=scene_order)

    # -- 检索（§4.3）------------------------------------------------------

    def search(self, project_id: str, query: str, limit: int = 10) -> list[dict]:
        """FTS5 优先，中文分词能力不足时回退 LIKE 子串搜索。

        全部使用绑定参数，不拼接 SQL。
        """
        self._require_project_exists(project_id)
        results = self._search_fts(project_id, query, limit)
        if results:
            return results
        return self._search_like(project_id, query, limit)

    def _search_fts(self, project_id: str, query: str, limit: int) -> list[dict]:
        fts_query = _to_fts_query(query)
        if not fts_query:
            return []
        try:
            rows = self.db.execute(
                "SELECT c.chunk_id, c.content, c.scene_id, d.path,"
                " bm25(chunks_fts) AS score"
                " FROM chunks_fts"
                " JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id"
                " JOIN documents d ON d.document_id = c.document_id"
                " WHERE chunks_fts MATCH ? AND c.project_id = ?"
                " ORDER BY bm25(chunks_fts) LIMIT ?",
                (fts_query, project_id, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"chunk_id": r["chunk_id"], "content": r["content"],
                 "scene_id": r["scene_id"], "path": r["path"],
                 "score": r["score"]} for r in rows]

    def _search_like(self, project_id: str, query: str, limit: int) -> list[dict]:
        # 转义 LIKE 通配符，避免用户输入改变匹配语义
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            "SELECT c.chunk_id, c.content, c.scene_id, d.path"
            " FROM chunks c JOIN documents d ON d.document_id = c.document_id"
            " WHERE c.project_id=? AND c.content LIKE ? ESCAPE '\\'"
            " ORDER BY c.ordinal LIMIT ?",
            (project_id, f"%{escaped}%", limit)).fetchall()
        return [{"chunk_id": r["chunk_id"], "content": r["content"],
                 "scene_id": r["scene_id"], "path": r["path"],
                 "score": None} for r in rows]

    def evidence(self, project_id: str, chunk_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT c.chunk_id, c.content, c.source_start, c.source_end,"
            " c.scene_id, d.path, d.sha256, d.version"
            " FROM chunks c JOIN documents d ON d.document_id = c.document_id"
            " WHERE c.project_id=? AND c.chunk_id=?",
            (project_id, chunk_id)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def events_for_scene(self, project_id: str, scene_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT event_id, event_type, approval, certainty,"
            " evidence_chunk_id, changes_json, event_order"
            " FROM events WHERE project_id=? AND scene_id=?"
            " ORDER BY event_order, created_at", (project_id, scene_id)).fetchall()
        return [{"event_id": r["event_id"], "event_type": r["event_type"],
                 "approval": r["approval"], "certainty": r["certainty"],
                 "evidence_chunk_id": r["evidence_chunk_id"],
                 "changes": json.loads(r["changes_json"]),
                 "event_order": r["event_order"]} for r in rows]

    # -- 摘要 -------------------------------------------------------------

    def summary(self, project_id: str, scope: str, content: str,
                source_refs: list[str], *, status: str = "draft") -> str:
        sid = _new_id("sum")
        self.db.execute(
            "INSERT INTO summaries(summary_id, project_id, scope,"
            " source_refs_json, content, status) VALUES(?, ?, ?, ?, ?, ?)",
            (sid, project_id, scope, _j(source_refs), content, status))
        self.db.commit()
        return sid

    def mark_summaries_stale(self, project_id: str, source_ref: str) -> int:
        rows = self.db.execute(
            "SELECT summary_id, source_refs_json FROM summaries"
            " WHERE project_id=? AND status!='stale'", (project_id,)).fetchall()
        n = 0
        for r in rows:
            if source_ref in json.loads(r["source_refs_json"]):
                self.db.execute(
                    "UPDATE summaries SET status='stale' WHERE summary_id=?",
                    (r["summary_id"],))
                n += 1
        if n:
            self.db.commit()
        return n

    def active_summaries(self, project_id: str, scope: str | None = None):
        sql = ("SELECT summary_id, scope, content, status FROM summaries"
               " WHERE project_id=? AND status!='stale'")
        params = [project_id]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _invalidate_summaries_by_path(self, project_id: str, path: str) -> int:
        """同一 path 产生新版本时，依赖该路径任一历史版本块的摘要全部失效。

        这是 §5 的落点：改剧本后旧摘要不得继续作为权威上下文。
        """
        rows = self.db.execute(
            "SELECT c.chunk_id FROM chunks c"
            " JOIN documents d ON d.document_id = c.document_id"
            " WHERE c.project_id=? AND d.path=?", (project_id, path)).fetchall()
        refs = {r["chunk_id"] for r in rows}
        refs |= {r["document_id"] for r in self.db.execute(
            "SELECT document_id FROM documents WHERE project_id=? AND path=?",
            (project_id, path)).fetchall()}
        if not refs:
            return 0
        return self._stale_by_refs(project_id, refs)

    def _stale_by_refs(self, project_id: str, refs: set) -> int:
        rows = self.db.execute(
            "SELECT summary_id, source_refs_json FROM summaries"
            " WHERE project_id=? AND status!='stale'", (project_id,)).fetchall()
        n = 0
        for r in rows:
            if set(json.loads(r["source_refs_json"])) & refs:
                self.db.execute(
                    "UPDATE summaries SET status='stale' WHERE summary_id=?",
                    (r["summary_id"],))
                n += 1
        if n:
            self.db.commit()
        return n

    def _invalidate_summaries(self, project_id: str, document_id: str) -> int:
        """原文版本变更 → 引用该文档（或其任意块）的摘要置 stale（§5）。"""
        chunk_ids = {r["chunk_id"] for r in self.db.execute(
            "SELECT chunk_id FROM chunks WHERE project_id=? AND document_id=?",
            (project_id, document_id)).fetchall()}
        chunk_ids.add(document_id)
        return self._stale_by_refs(project_id, chunk_ids)

    # -- 内部工具 ---------------------------------------------------------

    def _require_project(self, project_id: str):
        self._require_project_exists(project_id)

    def _require_project_exists(self, project_id: str):
        row = self.db.execute(
            "SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise Conflict(f"项目不存在：{project_id}",
                           error_code="PROJECT_NOT_FOUND")

    def _chunk_exists_in_other_db(self, chunk_id: str,
                                  project_id: str) -> bool:
        """单库隔离下的降级判定：本库没有该块时，无法判断它是否属于别库。

        多项目共库（同一 Connection）时可直接按 project_id 判定；单库单项目
        部署下返回 False，让上层报 NOT_FOUND，避免把不存在的块误报成跨项目。
        """
        return False

    def _audit(self, project_id: str, action: str, target_id: str | None,
               details: dict | None) -> None:
        self.db.execute(
            "INSERT INTO audit_log(project_id, action, target_id, details_json)"
            " VALUES(?, ?, ?, ?)",
            (project_id, action, target_id,
             None if details is None else _j(details)))


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------

def _assert_safe_identifier(value: str, label: str) -> None:
    """防止目录穿越：project_id 只能包含安全字符（§10.1）。"""
    if not value or not all(ch.isalnum() or ch in "_-" for ch in value):
        raise ValueError(
            f"{label} 只能是字母/数字/下划线/连字符，拒绝：{value!r}")


def _to_fts_query(query: str) -> str:
    """把普通搜索词转成安全的 FTS5 表达式：逐词加双引号并转义内部引号。"""
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return ""
    return " ".join('"' + t + '"' for t in tokens)
