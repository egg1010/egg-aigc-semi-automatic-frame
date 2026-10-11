"""统一入口 —— egg-aigc 记忆层 CLI

设计原则（用户定：状态机不变，校验层变，状态存储变）：
    - 状态机仍由既有 规范/流程状态机.md + 工具/校验器.py（init/advance/fact/fail/check）管理
    - 本入口只负责**状态存储**（SQLite 记忆层）与**校验层**（Schema + 引用 + 连续性）
    - 不重复实现状态推进逻辑，避免两套状态机

典型用法：
    # 初始化项目与原文
    python runtime/main.py init --project film_001 --name "演示短片"

    # 导入剧本并切块
    python runtime/main.py ingest --project film_001 --path 剧本/第三场.txt \
        --scene scene_3 --scene-order 3

    # 从原文提取候选事实（不写库）
    python runtime/main.py extract --project film_001 --chunk chunk_xxx

    # 查看状态与证据
    python runtime/main.py state --project film_001 --scene scene_3
    python runtime/main.py timeline --project film_001 --entity 钥匙

    # 检索
    python runtime/main.py search --project film_001 --query 钥匙

    # 校验模型输出
    python runtime/main.py verify --stage storyboard --input out.json \
        --project film_001

    # 规则版本
    python runtime/main.py rules --diff
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DEFAULT_DATA_ROOT = os.path.join(ROOT, "data")


def _open(args):
    from memory_store import MemoryDB
    data_root = args.data_root or DEFAULT_DATA_ROOT
    path = MemoryDB.project_db_path(data_root, args.project)
    return MemoryDB(path)


def cmd_init(args) -> int:
    from memory_store import MemoryDB, Conflict
    data_root = args.data_root or DEFAULT_DATA_ROOT
    path = MemoryDB.project_db_path(data_root, args.project)
    db = MemoryDB(path)
    try:
        db.project(args.project, args.name or args.project)
        print(f"已初始化项目 {args.project}")
        print(f"数据库：{path}")
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


def cmd_ingest(args) -> int:
    """写入原文并切块。优先按场切，保留字符偏移。"""
    db = _open(args)
    try:
        with open(args.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        rel = args.rel_path or args.path
        doc = db.document(args.project, rel, content)

        if args.scene:
            db.scene(args.project, args.scene, args.scene_order)
        chunk = db.chunk(args.project, doc, content, scene_id=args.scene,
                         ordinal=0, source_start=0, source_end=len(content))
        print(json.dumps({"document_id": doc, "chunk_id": chunk,
                          "path": rel, "chars": len(content)},
                         ensure_ascii=False, indent=2))
    except (OSError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


def cmd_extract(args) -> int:
    """从原文提取候选事实。**不写库**，输出分桶结果供人工裁决。"""
    from memory_extractor import MemoryExtractor, group_into_proposals
    db = _open(args)
    try:
        ev = db.evidence(args.project, args.chunk)
        if ev is None:
            print(f"[ERROR] 证据块不存在：{args.chunk}", file=sys.stderr)
            return 2
        ex = MemoryExtractor()
        cands = ex.extract_candidates("", [{
            "chunk_id": ev["chunk_id"], "content": ev["content"],
            "scene_id": ev["scene_id"]}])
        grouped = group_into_proposals(cands)
        print(json.dumps(grouped, ensure_ascii=False, indent=2))
        if grouped["needs_confirmation"]:
            print("\n注意：needs_confirmation 中的推断事实需用户确认后才可提交。",
                  file=sys.stderr)
    finally:
        db.close()
    return 0


def cmd_state(args) -> int:
    db = _open(args)
    try:
        if args.scene:
            from retrieval import Retrieval
            rows = Retrieval(db).state_as_of_scene(args.project, args.scene)
        else:
            rows = db.state(args.project, args.entity)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_timeline(args) -> int:
    from retrieval import Retrieval
    db = _open(args)
    try:
        print(json.dumps(Retrieval(db).entity_timeline(args.project, args.entity),
                         ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_search(args) -> int:
    db = _open(args)
    try:
        print(json.dumps(db.search(args.project, args.query, args.limit),
                         ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_verify(args) -> int:
    import validators as V
    db = None
    if args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()
    try:
        if args.project:
            db = _open(args)
        res = V.validate(None, stage=args.stage, db=db, project_id=args.project,
                         authorization=args.authorization,
                         workspace=args.workspace, gate_stage=args.gate_stage,
                         raw_text=raw)
    finally:
        if db is not None:
            db.close()
    V._emit(res, args.json)
    return 0 if res.ok else 1


def cmd_rules(args) -> int:
    from knowledge_loader import KnowledgeLoader
    kl = KnowledgeLoader(ROOT)
    if args.diff:
        approved = kl.load_snapshot(args.snapshot)
        d = kl.diff_against(approved) if approved else {
            "note": "尚无已审核快照，用 --save 生成", "has_change": True,
            "current": kl.snapshot()}
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if args.save:
        p = kl.save_snapshot(args.snapshot)
        print(f"已保存规范快照：{p}")
        return 0
    print(json.dumps(kl.snapshot(), ensure_ascii=False, indent=2))
    return 0


def cmd_pending(args) -> int:
    """列出待审批事件与未解决冲突。"""
    db = _open(args)
    try:
        rows = db.db.execute(
            "SELECT e.event_id, e.scene_id, e.event_type, e.certainty,"
            " e.evidence_chunk_id, e.changes_json"
            " FROM events e WHERE e.project_id=? AND e.approval='proposed'",
            (args.project,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["changes"] = json.loads(d.pop("changes_json"))
            out.append(d)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="egg-aigc 记忆层入口（状态存储 + 校验层；状态机沿用既有实现）")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--project", required=True, help="项目 ID")
        sp.add_argument("--data-root", help="数据根目录（默认 data/）")

    s = sub.add_parser("init", help="初始化项目数据库")
    add_common(s)
    s.add_argument("--name", help="项目名称")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ingest", help="导入原文并切块")
    add_common(s)
    s.add_argument("--path", required=True, help="原文文件路径")
    s.add_argument("--rel-path", help="记录用相对路径（默认取 --path）")
    s.add_argument("--scene", help="场次 ID")
    s.add_argument("--scene-order", type=int, default=1, help="场次顺序")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("extract", help="从原文提取候选事实（不写库）")
    add_common(s)
    s.add_argument("--chunk", required=True, help="证据块 ID")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("state", help="查看已确认状态")
    add_common(s)
    s.add_argument("--scene", help="取该场结束时的状态快照")
    s.add_argument("--entity", help="只看某实体")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("timeline", help="查看实体状态变更轨迹")
    add_common(s)
    s.add_argument("--entity", required=True)
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("search", help="检索原文")
    add_common(s)
    s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("verify", help="校验模型输出")
    s.add_argument("--stage", required=True)
    s.add_argument("--input", help="待校验 JSON；缺省读 stdin")
    s.add_argument("--project")
    s.add_argument("--data-root")
    s.add_argument("--authorization")
    s.add_argument("--workspace")
    s.add_argument("--gate-stage")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("rules", help="规范版本清单 / 快照 / 差异")
    s.add_argument("--diff", action="store_true", help="对比已审核快照")
    s.add_argument("--save", action="store_true", help="保存当前快照")
    s.add_argument("--snapshot", help="快照文件路径")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("pending", help="列出待审批事件")
    add_common(s)
    s.set_defaults(func=cmd_pending)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
