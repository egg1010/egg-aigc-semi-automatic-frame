-- egg-aigc-semi-automatic-frame 记忆层初始 Schema
-- 迁移版本：1
-- 依据：架构改造方案 v2 §3.1
--
-- 设计原则（§1.1 单一事实来源）：
--   原文 documents/chunks  = 最初事实来源，不得由摘要覆盖
--   事件 events            = 剧情状态变更依据，保留原始证据
--   状态 state_current     = 已批准事件的物化投影，不是原始事实
--   摘要 summaries         = 可重新计算的派生数据
--
-- 注意：SQLite 里的规则/原文是「数据」，不是可执行指令（防止提示词注入）

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 项目隔离
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- 原文：完整剧本 / 规则及修订
-- 同一 path 内容变化时生成新 version，不覆盖旧行
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    path        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    sha256      TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, path, sha256),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_documents_project_path
    ON documents(project_id, path);

-- ---------------------------------------------------------------------------
-- 检索切块：按 集→场→台词/动作段 分层切，保留字符偏移以便回原文定位
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    scene_id        TEXT,
    source_start    INTEGER NOT NULL,
    source_end      INTEGER NOT NULL,
    content         TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_project_scene
    ON chunks(project_id, scene_id, ordinal);

-- ---------------------------------------------------------------------------
-- FTS5 全文索引（中文分词能力有限，检索层保留 LIKE 回退，见 §4.3）
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    project_id UNINDEXED,
    content,
    tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, project_id, content)
    VALUES (new.chunk_id, new.project_id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
    INSERT INTO chunks_fts(chunk_id, project_id, content)
    VALUES (new.chunk_id, new.project_id, new.content);
END;

-- ---------------------------------------------------------------------------
-- 实体：人物 / 道具 / 位置 / 资产
-- 主键必须带 project_id：不得仅凭「钥匙」这个名称跨项目当主键（§3.1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    project_id  TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('character','prop','location','asset')),
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(project_id, entity_id),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- 场次：稳定的剧情顺序，历史回写保护依赖 scene_order（§3.2 第 6 条）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenes (
    project_id  TEXT NOT NULL,
    scene_id    TEXT NOT NULL,
    scene_order INTEGER NOT NULL,
    title       TEXT,
    PRIMARY KEY(project_id, scene_id),
    UNIQUE(project_id, scene_order),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- 事件：审批后的状态变化依据
-- evidence_chunk_id 指向真实原文：摘要不能替代原文
-- idempotency_key 防重复应用；approval 三态
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    scene_id            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_order         INTEGER NOT NULL DEFAULT 0,
    evidence_chunk_id   TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    changes_json        TEXT NOT NULL CHECK(json_valid(changes_json)),
    certainty           TEXT NOT NULL DEFAULT 'explicit'
                        CHECK(certainty IN ('explicit','inferred','unknown')),
    approval            TEXT NOT NULL DEFAULT 'proposed'
                        CHECK(approval IN ('proposed','approved','rejected')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at         TEXT,
    UNIQUE(project_id, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY(project_id, scene_id) REFERENCES scenes(project_id, scene_id),
    FOREIGN KEY(evidence_chunk_id) REFERENCES chunks(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_events_project_scene
    ON events(project_id, scene_id, event_order);

-- ---------------------------------------------------------------------------
-- 当前状态：已批准事件的物化投影（故事进度「头部状态」）
-- value_json 受 json_valid 约束；source_event_id 保留可追溯性
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_current (
    project_id      TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    field           TEXT NOT NULL,
    value_json      TEXT NOT NULL CHECK(json_valid(value_json)),
    last_scene_order INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    source_event_id TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(project_id, entity_id, field),
    FOREIGN KEY(project_id, entity_id)
        REFERENCES entities(project_id, entity_id),
    FOREIGN KEY(source_event_id) REFERENCES events(event_id)
);

-- ---------------------------------------------------------------------------
-- 分层摘要：可失效派生数据。原文 sha256 变化或关联事件撤销 → stale
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS summaries (
    summary_id      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    scope           TEXT NOT NULL,
    source_refs_json TEXT NOT NULL CHECK(json_valid(source_refs_json)),
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','verified','stale')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_summaries_project_scope
    ON summaries(project_id, scope, status);

-- ---------------------------------------------------------------------------
-- 工作流阶段检查点：可恢复状态机
-- pending → running → awaiting_review → completed；failed → pending（重试）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id      TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','awaiting_review','completed','failed')),
    attempt     INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json)),
    error_json  TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
    actor       TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    PRIMARY KEY(run_id, step_id),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_project
    ON workflow_steps(project_id, run_id, status);

-- ---------------------------------------------------------------------------
-- 审计日志：每次状态提交、审批、失败都留痕
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_id   TEXT,
    details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audit_project_action
    ON audit_log(project_id, action, created_at);

-- ---------------------------------------------------------------------------
-- 迁移版本标记（§10.3）
-- ---------------------------------------------------------------------------
PRAGMA user_version = 1;
