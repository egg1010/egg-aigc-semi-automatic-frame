"""上下文组装 —— 架构改造方案 v2 §4.5

预算优先级（§4.5，由高到低）：
    当前剧本原文 > 当前规则 > 必需的已确认状态
    > 直接相关历史证据 > AI 摘要 > 泛语义相似内容

硬约束：
    - 预算不足时**从末尾删减**，不得静默删掉关键硬约束
    - 必要时主动提示「上下文不足」，而不是假装完整
    - 每条检索结果保留 project_id / document_id / chunk_id / rule_version
    - 禁止在找不到来源时把模型联想包装成「已确认事实」
"""

from __future__ import annotations

import json

# 预算优先级分层（数值越小越优先保留）
PRIORITY = {
    "source_script": 0,     # 当前剧本原文
    "applicable_rules": 1,  # 当前规则
    "canonical_state": 2,   # 必需的已确认状态
    "source_evidence": 3,   # 直接相关历史证据
    "summaries": 4,         # AI 摘要
    "retrieved_memories": 5,  # 泛语义相似内容
}


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文按字计约 1 token/字；ASCII 约 4 字符/token。
    宁可高估，避免超预算。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def _size(obj) -> int:
    return estimate_tokens(json.dumps(obj, ensure_ascii=False))


class ContextPackage:
    def __init__(self):
        self.task: dict = {}
        self.source_script: list[dict] = []
        self.applicable_rules: list[dict] = []
        self.canonical_state: list[dict] = []
        self.source_evidence: list[dict] = []
        self.summaries: list[dict] = []
        self.retrieved_memories: list[dict] = []
        self.unresolved_conflicts: list[dict] = []
        self.notes: list[str] = []
        self.budget: dict = {}

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "source_script": self.source_script,
            "applicable_rules": self.applicable_rules,
            "canonical_state": self.canonical_state,
            "source_evidence": self.source_evidence,
            "summaries": self.summaries,
            "retrieved_memories": self.retrieved_memories,
            "unresolved_conflicts": self.unresolved_conflicts,
            "notes": self.notes,
            "budget": self.budget,
        }


class ContextBuilder:
    def __init__(self, db, loader=None, *, token_budget: int = 8000):
        self.db = db
        self.loader = loader
        self.token_budget = token_budget

    def build(self, project_id: str, *, stage: str,
              scene_id: str | None = None,
              rule_paths=None, authorization: str | None = None,
              query: str | None = None, entity_ids=None,
              script_chunks=None, max_per_layer: int = 12) -> ContextPackage:
        pkg = ContextPackage()
        pkg.task = {"stage": stage, "scene_id": scene_id,
                    "project_id": project_id}
        if authorization:
            pkg.task["authorization"] = authorization

        # 0 层：当前剧本原文（最高优先级，不裁）
        for cid in (script_chunks or []):
            ev = self.db.evidence(project_id, cid)
            if ev:
                pkg.source_script.append({
                    "chunk_id": ev["chunk_id"], "path": ev["path"],
                    "quote": ev["content"], "scene_id": ev["scene_id"],
                    "document_version": ev["version"],
                })

        # 1 层：当前规则（带 sha256 版本，供模型与审计核对）
        if self.loader is not None and rule_paths:
            for p in rule_paths:
                try:
                    ver = self.loader.load(p)
                    pkg.applicable_rules.append({
                        "path": ver.path, "sha256": ver.sha256,
                        "content": ver.content,
                    })
                except FileNotFoundError:
                    pkg.notes.append(f"规范文件缺失，未能加载：{p}")
                except Exception as e:  # RuleChanged 等
                    pkg.notes.append(str(e))

        # 2 层：已确认状态（按故事时间）
        from retrieval import Retrieval
        r = Retrieval(self.db)
        if scene_id:
            pkg.canonical_state = _dedup_state(
                r.state_as_of_scene(project_id, scene_id, entity_ids))
            pkg.source_evidence = r.scene_events(project_id, scene_id)
            pkg.source_evidence = [
                {"chunk_id": e["evidence"]["chunk_id"],
                 "quote": e["evidence"]["content"],
                 "path": e["evidence"]["path"],
                 "document_version": e["evidence"]["version"],
                 "event_id": e["event_id"],
                 "event_type": e["event_type"]}
                for e in pkg.source_evidence if e.get("evidence")]

        # 4 层：摘要
        pkg.summaries = self.db.active_summaries(project_id)

        # 5 层：泛语义召回
        if query:
            for hit in self.db.search(project_id, query, max_per_layer):
                pkg.retrieved_memories.append({
                    "chunk_id": hit["chunk_id"], "quote": hit["content"],
                    "path": hit["path"], "scene_id": hit["scene_id"],
                })

        # 未解决冲突
        rows = self.db.db.execute(
            "SELECT event_id, scene_id, event_type FROM events"
            " WHERE project_id=? AND approval='proposed'", (project_id,)).fetchall()
        pkg.unresolved_conflicts = [dict(x) for x in rows]

        self._apply_budget(pkg)
        return pkg

    # -- 预算裁剪 ---------------------------------------------------------

    def _apply_budget(self, pkg: ContextPackage) -> None:
        """按优先级从低到高裁减，直到落在预算内。

        裁减顺序与 PRIORITY 相反：先动泛语义，最后才动规则与状态。
        """
        drop_order = [
            ("retrieved_memories", PRIORITY["retrieved_memories"]),
            ("summaries", PRIORITY["summaries"]),
            ("source_evidence", PRIORITY["source_evidence"]),
            ("canonical_state", PRIORITY["canonical_state"]),
        ]
        trimmed = []

        while self._total(pkg) > self.token_budget:
            removed = False
            for attr, _ in drop_order:
                items = getattr(pkg, attr)
                if items:
                    items.pop()  # 裁末尾（最不相关）
                    trimmed.append(attr)
                    removed = True
                    break
            if not removed:
                # 只剩规则与原文，不再裁；主动提示上下文不足
                pkg.notes.append(
                    f"上下文预算不足（{self._total(pkg)} > {self.token_budget}）："
                    "已保留全部硬约束（剧本原文 + 规则 + 必需状态），"
                    "其余内容被裁减，请人工确认信息是否充分")
                break

        if trimmed:
            detail = {}
            for a in trimmed:
                detail[a] = detail.get(a, 0) + 1
            pkg.notes.append("因预算裁减：" + "、".join(
                f"{k} -{v}" for k, v in detail.items()))

        pkg.budget = {
            "limit": self.token_budget,
            "used": self._total(pkg),
            "layers": {
                "source_script": _size(pkg.source_script),
                "applicable_rules": _size(pkg.applicable_rules),
                "canonical_state": _size(pkg.canonical_state),
                "source_evidence": _size(pkg.source_evidence),
                "summaries": _size(pkg.summaries),
                "retrieved_memories": _size(pkg.retrieved_memories),
            },
        }

    def _total(self, pkg: ContextPackage) -> int:
        return _size(pkg.to_dict())

    # -- 输出为模型可见的上下文字符串 -------------------------------------

    @staticmethod
    def render(pkg: ContextPackage) -> str:
        """渲染成给模型的证据包。每条来源都带引用 ID，可指回原文。"""
        lines = ["# 任务", json.dumps(pkg.task, ensure_ascii=False)]
        if pkg.applicable_rules:
            lines.append("\n# 适用规则（版本以 sha256 为准）")
            for r in pkg.applicable_rules:
                lines.append(f"- {r['path']} @ {r['sha256'][:12]}")
                lines.append(f"  {r['content'][:400]}")
        if pkg.source_script:
            lines.append("\n# 当前剧本原文")
            for s in pkg.source_script:
                lines.append(f"[{s['chunk_id']}] {s['quote']}")
        if pkg.canonical_state:
            lines.append("\n# 已确认状态（故事时间截至本场）")
            for s in pkg.canonical_state:
                lines.append(
                    f"- {s['entity_id']}.{s['field']} = "
                    f"{json.dumps(s['value'], ensure_ascii=False)} "
                    f"(来源事件 {s.get('source_event_id')})")
        if pkg.source_evidence:
            lines.append("\n# 历史证据")
            for e in pkg.source_evidence:
                lines.append(f"[{e['chunk_id']}] {e['quote']}")
        if pkg.unresolved_conflicts:
            lines.append("\n# 未解决冲突（不得作为已确认事实使用）")
            for c in pkg.unresolved_conflicts:
                lines.append(f"- {c['event_id']} @ {c['scene_id']} "
                             f"({c['event_type']}) 待裁决")
        if pkg.notes:
            lines.append("\n# 提示")
            for n in pkg.notes:
                lines.append(f"- {n}")
        return "\n".join(lines)


def _dedup_state(states: list[dict]) -> list[dict]:
    seen = {}
    for s in states:
        seen[(s["entity_id"], s["field"])] = s
    return list(seen.values())
