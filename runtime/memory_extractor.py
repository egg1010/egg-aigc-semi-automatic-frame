"""候选事实提取器 —— 架构改造方案 v2 §5（AI 记忆：候选与事实必须分离）

硬约束：
    本模块**只产出候选**，不做任何数据库写入。
    提取结果必须经由 validate → approve 才能成为已确认事实。

四类记忆（§5）：
    任务临时上下文  | 仅必要时   | 非权威
    工作摘要        | 可持久化   | 可重新生成
    项目长期知识    | 可持久化   | 有文件来源或人工批准
    已确认剧情事实  | 必须持久化 | 原文证据 + 规则验证 + 批准

certainty 分级：
    explicit —— 明确原文事实，可进入审批队列
    inferred —— AI 推断，默认不进入正式状态，需用户确认
    unknown  —— 无法判定，不得进入审批队列
"""

from __future__ import annotations

import hashlib
import json
import re

CERTAINTY_EXPLICIT = "explicit"
CERTAINTY_INFERRED = "inferred"
CERTAINTY_UNKNOWN = "unknown"

# 仅 explicit 允许自动进入审批队列（§5）
APPROVABLE_CERTAINTY = (CERTAINTY_EXPLICIT,)


class Candidate:
    """一条候选事实。不可变，且带证据与 certainty。"""

    __slots__ = ("entity_id", "entity_kind", "field", "expected", "new",
                 "certainty", "evidence_chunk_id", "quote", "rationale",
                 "event_type", "scene_id")

    def __init__(self, *, entity_id, field, new, certainty,
                 evidence_chunk_id=None, quote=None, expected=None,
                 entity_kind="prop", rationale=None, event_type="state_change",
                 scene_id=None):
        if certainty not in (CERTAINTY_EXPLICIT, CERTAINTY_INFERRED,
                             CERTAINTY_UNKNOWN):
            raise ValueError(f"certainty 非法：{certainty}")
        self.entity_id = entity_id
        self.entity_kind = entity_kind
        self.field = field
        self.expected = expected
        self.new = new
        self.certainty = certainty
        self.evidence_chunk_id = evidence_chunk_id
        self.quote = quote
        self.rationale = rationale
        self.event_type = event_type
        self.scene_id = scene_id

    @property
    def is_approvable(self) -> bool:
        """能否进入审批队列：必须 explicit 且有证据。"""
        return self.certainty in APPROVABLE_CERTAINTY and bool(
            self.evidence_chunk_id)

    def to_change(self) -> dict:
        return {"entity_id": self.entity_id, "field": self.field,
                "expected": self.expected, "new": self.new}

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "field": self.field,
            "expected": self.expected,
            "new": self.new,
            "certainty": self.certainty,
            "evidence_chunk_id": self.evidence_chunk_id,
            "quote": self.quote,
            "event_type": self.event_type,
            "scene_id": self.scene_id,
            "rationale": self.rationale,
        }

    def __repr__(self):
        return (f"Candidate({self.entity_id}.{self.field} "
                f"{self.expected!r}→{self.new!r}, {self.certainty})")


class MemoryExtractor:
    """候选事实提取器。

    extract_candidates(text, evidence) 只返回候选列表，**绝不修改 SQLite**。
    生产环境应接模型，这里提供 deterministic 规则版作为可测试基线。
    """

    def __init__(self, *, entity_resolver=None):
        # entity_resolver: 把名称解析为 entity_id，生产由上层注入
        self._resolve = entity_resolver or (lambda name, kind: name)

    # -- 主接口 -----------------------------------------------------------

    def extract_candidates(self, text: str,
                           evidence: list[dict]) -> list[Candidate]:
        """从原文提取候选事实。

        evidence 每项：{"chunk_id", "content", "scene_id"?, "quote"?}
        返回值全部是候选，**不得直接当作已确认事实使用**。
        """
        candidates: list[Candidate] = []
        for ev in evidence:
            chunk_id = ev.get("chunk_id")
            content = ev.get("content") or ""
            scene_id = ev.get("scene_id")
            candidates.extend(self._extract_transfer(content, chunk_id, scene_id))
            candidates.extend(self._extract_initial(content, chunk_id, scene_id))
            candidates.extend(self._extract_inferred(content, chunk_id, scene_id))
        return self._drop_unhedged_duplicates(candidates)

    @staticmethod
    def _drop_unhedged_duplicates(candidates: list[Candidate]) -> list[Candidate]:
        """确定性规则不得吞掉带模糊词的表述。

        若同一 span 既被降级规则命中、又被确定性规则命中，保留 inferred 版本，
        否则「钥匙可能属于陈明」会被误判成 explicit（§5 不得把推断包装成事实）。
        """
        hedged_spans = {c.quote for c in candidates
                        if c.certainty == CERTAINTY_INFERRED and c.quote}
        out = []
        for c in candidates:
            if c.certainty == CERTAINTY_EXPLICIT and c.quote:
                if any(c.quote in span for span in hedged_spans):
                    continue
            out.append(c)
        return out

    # -- 规则实现（可被模型替换）------------------------------------------

    # 「甲把乙交给丙」→ 持有权转移
    _TRANSFER = re.compile(
        r"(?P<who>[\u4e00-\u9fa5]{2,4})将(?P<what>[\u4e00-\u9fa5]{1,6})"
        r"交给(?P<to>[\u4e00-\u9fa5]{2,4})")

    def _extract_transfer(self, text, chunk_id, scene_id):
        out = []
        for m in self._TRANSFER.finditer(text):
            what, who, to = m.group("what"), m.group("who"), m.group("to")
            out.append(Candidate(
                entity_id=self._resolve(what, "prop"),
                entity_kind="prop",
                field="owner",
                new=self._resolve(to, "character"),
                certainty=CERTAINTY_EXPLICIT,
                evidence_chunk_id=chunk_id,
                quote=m.group(0),
                event_type="prop_transfer",
                scene_id=scene_id,
            ))
        return out

    # 「某物属于某人」→ 初始归属
    _BELONG = re.compile(
        r"(?P<what>[\u4e00-\u9fa5]{1,6})(?P<verb>属于|归)(?P<who>[\u4e00-\u9fa5]{2,4})")

    def _extract_initial(self, text, chunk_id, scene_id):
        out = []
        for m in self._BELONG.finditer(text):
            what, who = m.group("what"), m.group("who")
            out.append(Candidate(
                entity_id=self._resolve(what, "prop"),
                entity_kind="prop",
                field="owner",
                expected=None,
                new=self._resolve(who, "character"),
                certainty=CERTAINTY_EXPLICIT,
                evidence_chunk_id=chunk_id,
                quote=m.group(0),
                event_type="initial_state",
                scene_id=scene_id,
            ))
        return out

    # 「大概是/可能是/似乎」→ 只能推断，不得直接入正式状态（§5）
    _HEDGE = re.compile(
        r"(?P<what>[\u4e00-\u9fa5]{1,6})(?:大概|可能|似乎|应该是)"
        r"(?P<verb>属于|在|是|归)(?P<who>[\u4e00-\u9fa5]{1,6})")

    def _extract_inferred(self, text, chunk_id, scene_id):
        out = []
        for m in self._HEDGE.finditer(text):
            out.append(Candidate(
                entity_id=self._resolve(m.group("what"), "prop"),
                entity_kind="prop",
                field="owner",
                new=m.group("who"),
                certainty=CERTAINTY_INFERRED,
                evidence_chunk_id=chunk_id,
                quote=m.group(0),
                rationale="原文含不确定表述，仅作推断候选",
                event_type="state_change",
                scene_id=scene_id,
            ))
        return out


# ---------------------------------------------------------------------------
# 候选→事件提案的组装与准入门禁
# ---------------------------------------------------------------------------

def group_into_proposals(candidates: list[Candidate]) -> dict:
    """把候选分桶：可审批 / 待用户确认 / 丢弃。

    这条门禁是 §5「AI 推断默认不进入正式状态」的落点。
    """
    approvable, needs_confirmation, discarded = [], [], []
    for c in candidates:
        if c.certainty == CERTAINTY_EXPLICIT and c.evidence_chunk_id:
            approvable.append(c)
        elif c.certainty == CERTAINTY_INFERRED:
            needs_confirmation.append(c)
        else:
            # unknown，或 explicit 却无证据 → 不得进入审批队列
            discarded.append(c)
    return {
        "approvable": [c.to_dict() for c in approvable],
        "needs_confirmation": [c.to_dict() for c in needs_confirmation],
        "discarded": [c.to_dict() for c in discarded],
    }


def proposal_idempotency_key(project_id: str, scene_id: str, event_type: str,
                             changes: list[dict]) -> str:
    """稳定幂等键：同一事实重复提取不产生重复事件（§10.2）。"""
    payload = json.dumps(
        {"p": project_id, "s": scene_id, "t": event_type,
         "c": sorted(changes, key=lambda x: (x.get("entity_id", ""),
                                             x.get("field", "")))},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "idem_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def to_event_proposals(candidates: list[Candidate], project_id: str) -> list[dict]:
    """把 explicit 候选组装成 propose_event 可直接消费的提案参数。

    重要：本函数只返回参数，不写库。写库由 memory_store 在事务内完成。
    """
    grouped = group_into_proposals(candidates)
    proposals = []
    for item in grouped["approvable"]:
        changes = [{"entity_id": item["entity_id"], "field": item["field"],
                    "expected": item["expected"], "new": item["new"]}]
        proposals.append({
            "project_id": project_id,
            "scene_id": item["scene_id"],
            "event_type": item["event_type"],
            "changes": changes,
            "evidence_chunk_id": item["evidence_chunk_id"],
            "idempotency_key": proposal_idempotency_key(
                project_id, item["scene_id"], item["event_type"], changes),
            "certainty": item["certainty"],
            "quote": item["quote"],
        })
    return proposals
