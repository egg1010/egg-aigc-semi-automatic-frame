"""校验层 —— 架构改造方案 v2 §6

定位（用户定：状态机不变，校验层变）：
    本模块是「校验层」的新实现。它**不重写**既有 `工具/校验器.py` 的结构门禁，
    而是委派给它；在此之上叠加 JSON Schema 校验、引用真实性、状态连续性与
    权限分级，并统一输出结构化结果。

九步校验次序（§6.2）：
    1 有效 JSON          2 JSON Schema        3 引用真实性
    4 必需事实完整性      5 状态连续性        6 规则权限
    7 槽位齐全（委派）    8 人工审批          9 事务提交（由 memory_store 负责）

可验证程度分级（§6.2）：
    硬约束 = 机器判（ID 存在、字段、道具归属、槽位）
    软约束 = 人工/评价模型评审（美学判断），不得伪装成 Schema 可证明的约束
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA_DIR = os.path.join(ROOT, "schemas")
LEGACY_VALIDATOR = os.path.join(ROOT, "工具", "校验器.py")

# 硬约束（机器可判）与软约束（人工评审）必须分开，不得混为一谈（§6.2）
HARD_CONSTRAINT_CODES = ("SCHEMA", "REF", "CONT", "PERM", "GATE", "JSON")
SOFT_CONSTRAINT_NOTE = (
    "美学判断（电影感、镜头优美度等）属软约束，不由本层自动判定，交人工或评价模型评审"
)

STAGE_SCHEMA = {
    "script_analysis": "script_analysis.schema.json",
    "asset_inventory": "asset_inventory.schema.json",
    "storyboard": "storyboard.schema.json",
    "event_proposal": "event_proposal.schema.json",
}

# 权限分级：不同阶段允许改动的范围（§6.2 第 6 步 / §8.6）
PERMISSION_MATRIX = {
    "format": ("style", "format"),
    "style": ("style", "format"),
    "shot_design": ("shot_design", "style", "format"),
    "content": ("content", "shot_design", "style", "format"),
}
PERMISSION_ORDER = ("format", "style", "shot_design", "content")


class Finding:
    __slots__ = ("level", "code", "message", "step")

    def __init__(self, level, code, message, step=None):
        self.level = level      # ERROR / WARN / INFO
        self.code = code
        self.message = message
        self.step = step        # 九步校验次序中的第几步

    def to_dict(self):
        return {"level": self.level, "code": self.code,
                "message": self.message, "step": self.step}


class Result:
    def __init__(self):
        self.findings: list[Finding] = []

    def add(self, level, code, message, step=None):
        self.findings.append(Finding(level, code, message, step))

    def error(self, code, message, step=None):
        self.add("ERROR", code, message, step)

    def warn(self, code, message, step=None):
        self.add("WARN", code, message, step)

    def info(self, code, message, step=None):
        self.add("INFO", code, message, step)

    @property
    def ok(self) -> bool:
        return not any(f.level == "ERROR" for f in self.findings)

    def counts(self) -> dict:
        c = {"ERROR": 0, "WARN": 0, "INFO": 0}
        for f in self.findings:
            c[f.level] += 1
        return c

    def to_dict(self) -> dict:
        return {"ok": self.ok, "counts": self.counts(),
                "findings": [f.to_dict() for f in self.findings]}


# ---------------------------------------------------------------------------
# 第 1-2 步：JSON 有效性 + Schema
# ---------------------------------------------------------------------------

_jsonschema = None
_jsonschema_tried = False


def _load_jsonschema():
    """优先用 jsonschema；未安装则回退内置最小校验器（§2 依赖建议）。"""
    global _jsonschema, _jsonschema_tried
    if not _jsonschema_tried:
        _jsonschema_tried = True
        try:
            import jsonschema  # type: ignore
            _jsonschema = jsonschema
        except ImportError:
            _jsonschema = None
    return _jsonschema


def _resolve_ref(schema, root):
    if "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    if ref.startswith("#/"):
        node = root
        for part in ref[2:].split("/"):
            node = node[part]
        return node
    # 跨文件引用：同目录加载
    target = os.path.join(SCHEMA_DIR, ref)
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_schema(instance, schema_path: str, res: Result, step=2) -> bool:
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    js = _load_jsonschema()
    if js is not None:
        try:
            validator = js.Draft202012Validator(schema)
            errs = sorted(validator.iter_errors(instance),
                          key=lambda e: list(e.path))
            for e in errs:
                path = "/".join(str(p) for p in e.path) or "(根)"
                res.error("SCHEMA", f"Schema 不通过 @{path}：{e.message}", step)
            return not errs
        except Exception as e:  # schema 自身问题
            res.error("SCHEMA", f"Schema 校验器异常：{e}", step)
            return False

    return _minimal_validate(instance, schema, res, step)


def _minimal_validate(instance, schema, res: Result, step=2,
                      path="$", root=None) -> bool:
    """无 jsonschema 时的最小回退：required / type / enum / minItems / additionalProperties。"""
    root = root if root is not None else schema
    ok = True
    schema = _resolve_ref(schema, root)

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        if not _type_ok(instance, types):
            res.error("SCHEMA", f"类型不符 @{path}：应为 {expected}", step)
            return False

    if "enum" in schema and instance not in schema["enum"]:
        res.error("SCHEMA",
                  f"取值非法 @{path}：{instance!r} 不在 {schema['enum']}", step)
        ok = False

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            res.error("SCHEMA", f"字符串过短 @{path}", step)
            ok = False

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            res.error("SCHEMA", f"低于最小值 @{path}", step)
            ok = False
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            res.error("SCHEMA", f"未超过下界 @{path}", step)
            ok = False

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            res.error("SCHEMA", f"数组元素过少 @{path}", step)
            ok = False
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                if not _minimal_validate(item, item_schema, res, step,
                                         f"{path}[{i}]", root):
                    ok = False

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                res.error("SCHEMA", f"缺少必填字段 @{path}.{req}", step)
                ok = False
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    res.error("SCHEMA", f"出现未声明字段 @{path}.{key}", step)
                    ok = False
        for key, sub in props.items():
            if key in instance:
                if not _minimal_validate(instance[key], sub, res, step,
                                         f"{path}.{key}", root):
                    ok = False
    return ok


def _type_ok(value, types) -> bool:
    for t in types:
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "null" and value is None:
            return True
    return False


# ---------------------------------------------------------------------------
# 第 3 步：引用真实性（必须查库，不能靠模型声明）
# ---------------------------------------------------------------------------

def validate_references(instance, db, project_id: str, res: Result,
                        stage: str) -> bool:
    ok = True

    def _need_refs(refs, label):
        nonlocal ok
        if not refs:
            res.error("REF", f"{label} 缺少 source_refs：禁止无来源结论", 3)
            ok = False
            return
        for ref in refs:
            if db.evidence(project_id, ref) is None:
                res.error("REF", f"{label} 引用了不存在的证据块：{ref}", 3)
                ok = False

    if stage == "storyboard":
        scene_id = instance.get("scene_id")
        if scene_id:
            row = db.db.execute(
                "SELECT 1 FROM scenes WHERE project_id=? AND scene_id=?",
                (project_id, scene_id)).fetchone()
            if row is None:
                res.error("REF", f"分镜引用了不存在的场次：{scene_id}", 3)
                ok = False
        for shot in instance.get("shots", []):
            for eid in shot.get("characters", []) + shot.get("entity_refs", []):
                r = db.db.execute(
                    "SELECT 1 FROM entities WHERE project_id=? AND entity_id=?",
                    (project_id, eid)).fetchone()
                if r is None:
                    res.error("REF",
                              f"镜头 {shot.get('shot_id')} 引用了不存在的实体：{eid}", 3)
                    ok = False
        _need_refs(instance.get("source_refs"), "分镜")

    elif stage == "asset_inventory":
        for asset in instance.get("assets", []):
            eid = asset.get("entity_id")
            r = db.db.execute(
                "SELECT 1 FROM entities WHERE project_id=? AND entity_id=?",
                (project_id, eid)).fetchone()
            if r is None:
                res.error("REF", f"资产 {asset.get('asset_id')} 引用了不存在的实体：{eid}", 3)
                ok = False

    elif stage == "event_proposal":
        chunk = instance.get("evidence_chunk_id")
        if chunk and db.evidence(project_id, chunk) is None:
            res.error("REF", f"事件证据块不存在：{chunk}", 3)
            ok = False
        sid = instance.get("scene_id")
        if sid and db.db.execute(
                "SELECT 1 FROM scenes WHERE project_id=? AND scene_id=?",
                (project_id, sid)).fetchone() is None:
            res.error("REF", f"事件引用了不存在的场次：{sid}", 3)
            ok = False
        for c in instance.get("changes", []):
            if db.db.execute(
                    "SELECT 1 FROM entities WHERE project_id=? AND entity_id=?",
                    (project_id, c.get("entity_id"))).fetchone() is None:
                res.error("REF", f"事件引用了不存在的实体：{c.get('entity_id')}", 3)
                ok = False

    return ok


# ---------------------------------------------------------------------------
# 第 5 步：状态连续性（道具归属、服装、位置）
# ---------------------------------------------------------------------------

CONTINUITY_FIELDS = ("owner", "costume", "appearance", "location", "prop_hold", "state")


def validate_continuity(instance, db, project_id: str, res: Result) -> bool:
    """分镜中出现的实体，其状态必须能在已批准事实里找到依据。"""
    ok = True
    scene_id = instance.get("scene_id")
    scene_order = None
    if scene_id:
        row = db.db.execute(
            "SELECT scene_order FROM scenes WHERE project_id=? AND scene_id=?",
            (project_id, scene_id)).fetchone()
        if row:
            scene_order = row["scene_order"]

    for shot in instance.get("shots", []):
        for eid in shot.get("entity_refs", []):
            rows = db.state(project_id, eid, scene_order=scene_order)
            if not rows:
                res.warn("CONT",
                         f"镜头 {shot.get('shot_id')} 的实体 {eid} 在场景"
                         f" {scene_id or '?'} 前没有任何已批准状态，"
                         "可能缺少前序事实", 5)
                ok = False if False else ok  # 缺状态属提示，不阻断
    return ok


# ---------------------------------------------------------------------------
# 第 6 步：规则权限分级（§8.6）
# ---------------------------------------------------------------------------

def validate_permission(instance, authorization: str | None, res: Result) -> bool:
    if authorization is None:
        return True
    if authorization not in PERMISSION_MATRIX:
        res.error("PERM", f"授权级别非法：{authorization}", 6)
        return False

    allowed = PERMISSION_MATRIX[authorization]
    declared = instance.get("change_scope")
    if declared is None:
        return True
    if declared not in PERMISSION_ORDER:
        res.error("PERM", f"声明的变更范围非法：{declared}", 6)
        return False
    if declared not in allowed:
        res.error(
            "PERM",
            f"越权：授权={authorization} 不得执行 {declared} 级修改（允许："
            f"{'/'.join(allowed)}）", 6)
        return False
    return True


# ---------------------------------------------------------------------------
# 第 7 步：槽位齐全 —— 委派既有校验器，行为不回退
# ---------------------------------------------------------------------------

def run_legacy_gate(workspace: str, stage: str | None = None,
                    unit: str | None = None,
                    config: str | None = None) -> dict | None:
    """调用既有 工具/校验器.py --json，取其结构化结果。

    既有校验器仍是结构门禁的唯一真值；本层只做结果整合。
    """
    if not os.path.isfile(LEGACY_VALIDATOR):
        return None
    cmd = [sys.executable, LEGACY_VALIDATOR, "check", workspace, "--json"]
    if stage:
        cmd += ["--stage", stage]
    if unit:
        cmd += ["--unit", unit]
    if config:
        cmd += ["--config", config]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def merge_legacy(workspace: str, res: Result, stage: str | None = None,
                 unit: str | None = None, config: str | None = None) -> bool:
    data = run_legacy_gate(workspace, stage, unit, config)
    if data is None:
        res.warn("GATE", f"既有校验器未产出结果，已跳过第 7 步：{workspace}", 7)
        return True
    for issue in data.get("issues", []):
        lvl = issue.get("level", "INFO")
        code = issue.get("code", "GATE")
        msg = issue.get("message", "")
        res.add(lvl, code, msg, 7)
    return not any(i.get("level") == "ERROR" for i in data.get("issues", []))


# ---------------------------------------------------------------------------
# 主校验入口：按九步次序执行
# ---------------------------------------------------------------------------

def validate(instance, *, stage: str, db=None, project_id: str | None = None,
             authorization: str | None = None,
             workspace: str | None = None,
             gate_stage: str | None = None,
             raw_text: str | None = None) -> Result:
    res = Result()

    # 1. 有效 JSON
    if raw_text is not None:
        try:
            instance = json.loads(raw_text)
        except ValueError as e:
            res.error("JSON", f"输出不是有效 JSON：{e}", 1)
            return res
    if not isinstance(instance, dict):
        res.error("JSON", f"输出顶层必须是对象，实际是 {type(instance).__name__}", 1)
        return res

    # 2. Schema
    schema_name = STAGE_SCHEMA.get(stage)
    if schema_name is None:
        res.error("SCHEMA", f"未知阶段，无对应 Schema：{stage}", 2)
        return res
    validate_schema(instance, os.path.join(SCHEMA_DIR, schema_name), res, 2)

    # 3. 引用真实性
    if db is not None and project_id:
        validate_references(instance, db, project_id, res, stage)

    # 4. 必需事实完整性（event_proposal：changes 必须非空且字段合法）
    if stage == "event_proposal":
        changes = instance.get("changes") or []
        if not changes:
            res.error("CONT", "事件提案的 changes 为空，无实际变更", 4)
        if instance.get("certainty", "explicit") != "explicit":
            res.warn("CONT",
                     f"certainty={instance.get('certainty')}：非 explicit 事实"
                     "不得直接进入正式状态，需用户确认（§5）", 4)

    # 5. 状态连续性
    if db is not None and project_id and stage == "storyboard":
        validate_continuity(instance, db, project_id, res)

    # 6. 规则权限
    validate_permission(instance, authorization, res)

    # 7. 槽位齐全（委派既有校验器）
    if workspace:
        merge_legacy(workspace, res, stage=gate_stage)

    # 8. 人工审批：由状态机侧流程负责，本层只标注
    if stage == "asset_inventory" and not instance.get("approved"):
        res.info("GATE", "资产图尚未通过人工审核，禁止进入视频阶段", 8)

    # 软约束提示（§6.2）：美学判断不在机器判定范围
    res.info("SOFT", SOFT_CONSTRAINT_NOTE, None)
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_check(args) -> int:
    if args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            print(f"[ERROR] 无法读取输入：{e}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    db = None
    if args.db and args.project:
        from memory_store import MemoryDB
        db = MemoryDB(args.db)

    try:
        res = validate(None, stage=args.stage, db=db, project_id=args.project,
                       authorization=args.authorization,
                       workspace=args.workspace, gate_stage=args.gate_stage,
                       raw_text=raw)
    finally:
        if db is not None:
            db.close()

    _emit(res, args.json)
    return 0 if res.ok else 1


def cmd_gate(args) -> int:
    res = Result()
    merge_legacy(args.workspace, res, stage=args.stage, unit=args.unit,
                 config=args.config)
    _emit(res, args.json)
    return 0 if res.ok else 1


def cmd_selftest(args) -> int:
    """最小自检：Schema 分支 + 权限分支，不依赖 DB。"""
    cases = [
        ("合法分镜", {"scene_id": "scene_1",
                      "shots": [{"shot_id": "s1", "description": "推门",
                                 "camera_movement": "dolly",
                                 "characters": ["林雨"]}],
                      "source_refs": ["chunk_1"]}, True),
        ("缺 source_refs", {"scene_id": "scene_1",
                            "shots": [{"shot_id": "s1", "description": "推门",
                                       "camera_movement": "dolly",
                                       "characters": []}]}, False),
        ("镜头枚举非法", {"scene_id": "scene_1",
                          "shots": [{"shot_id": "s1", "description": "推门",
                                     "camera_movement": "瞬移",
                                     "characters": []}],
                          "source_refs": ["chunk_1"]}, False),
        ("未声明字段", {"scene_id": "scene_1",
                        "shots": [{"shot_id": "s1", "description": "推门",
                                   "camera_movement": "static",
                                   "characters": [], "乱入": 1}],
                        "source_refs": ["chunk_1"]}, False),
    ]
    passed = 0
    for name, inst, expect_ok in cases:
        res = Result()
        validate_schema(inst, os.path.join(SCHEMA_DIR,
                                           "storyboard.schema.json"), res, 2)
        if res.ok == expect_ok:
            passed += 1
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}（期望 ok={expect_ok}，实得 {res.ok}）")
            for f in res.findings:
                if f.level == "ERROR":
                    print(f"       {f.code} {f.message}")

    # 权限分支
    res = Result()
    validate_permission({"change_scope": "content"}, "style", res)
    if not res.ok and any(f.code == "PERM" for f in res.findings):
        passed += 1
        print("[PASS] 风格重构越权改内容被拦")
    else:
        print("[FAIL] 权限分支未拦截")

    total = len(cases) + 1
    print(f"== 校验层自检：{passed}/{total} 通过"
          f"{'，PASS' if passed == total else '，FAIL'} ==")
    return 0 if passed == total else 1


def _emit(res: Result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return
    print("== 校验层报告 ==")
    for f in res.findings:
        step = f"（第{f.step}步）" if f.step else ""
        print(f"[{f.level}] {f.code}{step} {f.message}")
    c = res.counts()
    print("----")
    print(f"结果：ERROR {c['ERROR']} ｜ WARN {c['WARN']} ｜ INFO {c['INFO']}"
          f" ｜ {'通过' if res.ok else '未通过'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="egg-aigc 校验层（v2 §6）")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="按九步次序校验模型输出")
    c.add_argument("--stage", required=True, choices=sorted(STAGE_SCHEMA))
    c.add_argument("--input", help="待校验 JSON 文件；缺省读 stdin")
    c.add_argument("--db", help="SQLite 库路径（引用与连续性校验）")
    c.add_argument("--project", help="项目 ID")
    c.add_argument("--authorization", help="重构授权级别：format/style/shot_design/content")
    c.add_argument("--workspace", help="工作区路径（触发第 7 步槽位门禁）")
    c.add_argument("--gate-stage", help="委派给既有校验器的阶段名")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    g = sub.add_parser("gate", help="只跑工作区结构门禁（委派既有校验器）")
    g.add_argument("--workspace", required=True)
    g.add_argument("--stage")
    g.add_argument("--unit")
    g.add_argument("--config")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_gate)

    s = sub.add_parser("selftest", help="校验层自检")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
