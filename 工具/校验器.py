#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
egg-aigc 校验器（轻量执行 / 校验层）

用法：
  python 工具/校验器.py check   <工作区> [--stage 阶段] [--unit 单元] [--json]
  python 工具/校验器.py init    <工作区> --unit 单元 [--unit 单元2 ...]
  python 工具/校验器.py advance <工作区> --unit 单元 --stage 阶段 [--mode 模式] [--artifact 名称=相对路径 ...] [--force]
  python 工具/校验器.py fact    <工作区> --unit 单元 --id ID --value 内容 --source 来源 [--subject 主体 --field 字段] [--confidence 级别] [--verified]
  python 工具/校验器.py fail    <工作区> --unit 单元 --reason 原因 [--stage 阶段] [--action 终止|挂起|已询问用户]
  python 工具/校验器.py selftest
  python 工具/校验器.py checks

职责：状态转移（init / advance / fact / fail）+ 结构校验（check）。只写状态文件；不写业务交付物。
契约：字段规则见 规范/数据契约.md；阶段与门禁见 规范/流程状态机.md。
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

SCHEMA_VERSION = "1.0"
STAGES = ["CREATED", "SCRIPT_PARSED", "ASSETS_INDEXED", "IMAGE_PROMPTS_READY",
          "VIDEO_PROMPTS_READY", "REVIEWED", "DONE"]
MODES = ["asset_reference", "text_only", "hybrid"]
CONFIDENCE = ["confirmed", "inferred", "unknown"]

STATE_DIR = "状态"
FLOW_STATE_FILE = "流程状态.json"
LEDGER_FILE = "连续性台账.json"

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
IMG_RE = re.compile(r"[\w\u4e00-\u9fff\-.()（） ]+\.(?:png|jpg|jpeg|webp|bmp|gif|tif|tiff)", re.I)
SHOT_RE = re.compile(r"【镜头\s*(\d+)\s*】")
SLOT_RE = re.compile(r"【[^】]+】")

REQUIRED_SLOTS = ["【场景】", "【站位设计】", "【人物】", "【影调】",
                  "【人物情绪时间轴】", "【逻辑约束】", "【负面提示词】", "【声音时间轴】"]
OPTIONAL_SLOTS = ["【资产参考】", "【道具】", "【拍摄参数】"]
SLOT_ORDER = ["【资产参考】", "【场景】", "【站位设计】", "【人物】", "【道具】", "【拍摄参数】",
              "【影调】", "【人物情绪时间轴】", "【逻辑约束】", "【负面提示词】", "【声音时间轴】"]

CROSS_REF_ERROR = [r"与第\s*\d+\s*段一致", r"同第\s*\d+\s*段", r"延续第\s*\d+\s*段",
                   r"衔接第\s*\d+\s*段", r"见第\s*\d+\s*段", r"如第\s*\d+\s*段",
                   r"同链"]
CROSS_REF_WARN = [r"同上一段", r"同前段", r"与上一段", r"承接上一场", r"同上一场"]

DEFAULT_CONFIG = {
    "char_limit": 9950,
    "asset_table_patterns": ["资产表"],
    "image_prompt_table_patterns": ["人物场景提示词表", "资产图提示词"],
    "prompt_name_include": "提示词",
    "prompt_name_exclude": ["资产图", "人物场景", "特殊版说明"],
    "cross_ref_error": CROSS_REF_ERROR,
    "cross_ref_warn": CROSS_REF_WARN,
    "seatmap_word": "站位图",
    "state_dir": STATE_DIR,
    "asset_dir_names": ["资产图", "资产"],
    "text_exts": [".txt", ".md"],
    "max_depth": 6,
}

CHECK_DOCS = [
    ("DS-00", "INFO", "状态未初始化提示（首次运行 init）"),
    ("DS-01", "ERROR", "流程状态.json 结构：stage/枚举/stages_done/artifacts 字段"),
    ("DS-02", "ERROR", "连续性台账.json 结构：facts 字段/confidence 枚举/conflicts"),
    ("DS-03", "WARN", "状态记录的产物路径在磁盘存在"),
    ("WS-01", "ERROR", "单元文件夹存在（--unit 时）"),
    ("WS-02", "ERROR", "单元内存在分段文本（门禁 SCRIPT_PARSED 起）"),
    ("WS-03", "WARN", "资产图目录存在性（图片模式相关）"),
    ("AT-01", "ERROR", "资产表文件存在（门禁 ASSETS_INDEXED 起）"),
    ("AT-02", "WARN", "资产表分区含 场景/人物/道具"),
    ("P-01", "ERROR", "提示词槽位完整性（必写槽位）"),
    ("P-02", "ERROR", "单段提示词字数上限（默认 9950，可在 校验配置.json 覆盖）"),
    ("P-03", "ERROR", "图片引用仅限【资产参考】节；声明文件可寻（WARN）"),
    ("P-04", "ERROR", "跨段引用禁令（与第N段一致等）"),
    ("P-05", "WARN", "提示词出现「站位图」字样（2D 站位图不应进入正文）"),
    ("P-06", "ERROR", "镜头编号：存在/重复（ERROR）与连续（WARN）"),
    ("P-07", "ERROR", "【负面提示词】节非空"),
    ("P-08", "WARN", "槽位顺序符合契约"),
    ("GATE-01", "ERROR", "阶段必需产物缺失（按 --stage 判定）"),
    ("GATE-02", "ERROR", "模式一致性：声明 asset_reference 但资产图目录为空"),
]


def log(msg):
    print(msg)


def read_text(path):
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def dump_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def now_iso():
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def walk_files(root, cfg, skip_dirs=("状态", "异常", ".git", ".workbuddy", "node_modules")):
    out = []
    root = os.path.abspath(root)
    base_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, files in os.walk(root):
        depth = dirpath.count(os.sep) - base_depth
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        if depth >= cfg["max_depth"]:
            dirnames[:] = []
        for fn in files:
            out.append(os.path.join(dirpath, fn))
    return out


def read_config(ws, config_path=None):
    cfg = dict(DEFAULT_CONFIG)
    path = config_path or (os.path.join(ws, "校验配置.json") if ws else None)
    if path and os.path.isfile(path):
        try:
            user = load_json(path)
            if isinstance(user, dict):
                cfg.update(user)
        except (OSError, ValueError):
            log("[WARN] CFG-01 校验配置.json 无法解析，使用默认配置：%s" % path)
    return cfg


class Report(object):
    def __init__(self):
        self.issues = []

    def add(self, level, code, msg):
        self.issues.append((level, code, msg))

    def err(self, code, msg):
        self.add("ERROR", code, msg)

    def warn(self, code, msg):
        self.add("WARN", code, msg)

    def info(self, code, msg):
        self.add("INFO", code, msg)

    def counts(self):
        c = {"ERROR": 0, "WARN": 0, "INFO": 0}
        for level, _code, _msg in self.issues:
            c[level] = c.get(level, 0) + 1
        return c

    def sorted_issues(self):
        rank = {"ERROR": 0, "WARN": 1, "INFO": 2}
        return sorted(self.issues, key=lambda i: (rank.get(i[0], 9), i[1]))


def line_of(content, pos):
    return content[:pos].count("\n") + 1


# ---------------------------------------------------------------- 状态校验

def check_state(ws, cfg, rep):
    """校验 状态/流程状态.json 与 连续性台账.json。返回 (flow_dict or None, unit_mode_map)"""
    flow = None
    mode_map = {}
    state_dir = os.path.join(ws, cfg["state_dir"])
    flow_path = os.path.join(state_dir, FLOW_STATE_FILE)

    if not os.path.isfile(flow_path):
        rep.info("DS-00", "状态未初始化（首次运行请执行 init 或按 流程状态机 自动创建）")
    else:
        try:
            flow = load_json(flow_path)
        except (OSError, ValueError) as e:
            rep.err("DS-01", "流程状态.json 无法解析：%s" % e)
            flow = None
        if isinstance(flow, dict):
            units = flow.get("units")
            if not isinstance(units, dict):
                rep.err("DS-01", "流程状态.json 缺 units 对象")
                units = {}
            for name, u in units.items():
                if not isinstance(u, dict):
                    rep.err("DS-01", "单元 %s 记录不是对象" % name)
                    continue
                stage = u.get("stage")
                if stage not in STAGES:
                    rep.err("DS-01", "单元 %s stage 非法：%r（必须为 %s）" % (name, stage, "/".join(STAGES)))
                done = u.get("stages_done")
                if not isinstance(done, list) or any(s not in STAGES for s in done):
                    rep.err("DS-01", "单元 %s stages_done 非法：%r" % (name, done))
                mode = u.get("video_input_mode")
                if mode is not None and mode not in MODES:
                    rep.err("DS-01", "单元 %s video_input_mode 非法：%r" % (name, mode))
                if mode:
                    mode_map[name] = mode
                arts = u.get("artifacts", {})
                if not isinstance(arts, dict):
                    rep.err("DS-01", "单元 %s artifacts 不是对象" % name)
                else:
                    for a_name, a in arts.items():
                        if not isinstance(a, dict) or not a.get("path"):
                            rep.err("DS-01", "单元 %s 产物 %s 缺 path" % (name, a_name))
                            continue
                        p = os.path.join(ws, str(a["path"]))
                        if not os.path.exists(p):
                            rep.warn("DS-03", "单元 %s 产物 %s 路径不存在：%s" % (name, a_name, a["path"]))
                for key in ("failures", "history"):
                    if key in u and not isinstance(u[key], list):
                        rep.err("DS-01", "单元 %s %s 不是数组" % (name, key))

    ledger_path = os.path.join(state_dir, LEDGER_FILE)
    if not os.path.isfile(ledger_path):
        rep.info("DS-00", "连续性台账未初始化（首次运行请执行 init）")
    else:
        try:
            ledger = load_json(ledger_path)
        except (OSError, ValueError) as e:
            rep.err("DS-02", "连续性台账.json 无法解析：%s" % e)
            ledger = None
        if isinstance(ledger, dict):
            facts = ledger.get("facts", [])
            if not isinstance(facts, list):
                rep.err("DS-02", "台账 facts 不是数组")
            else:
                need = ("id", "subject", "field", "value", "source", "confidence", "verified", "version")
                for i, f in enumerate(facts):
                    if not isinstance(f, dict):
                        rep.err("DS-02", "facts[%d] 不是对象" % i)
                        continue
                    for k in need:
                        if k not in f:
                            rep.err("DS-02", "facts[%d] 缺字段 %s（id=%s）" % (i, k, f.get("id", "?")))
                    if f.get("confidence") not in CONFIDENCE:
                        rep.err("DS-02", "facts[%d] confidence 非法：%r" % (i, f.get("confidence")))
                    if "version" in f and not isinstance(f["version"], int):
                        rep.err("DS-02", "facts[%d] version 必须为整数" % i)
            conflicts = ledger.get("conflicts", [])
            if not isinstance(conflicts, list):
                rep.err("DS-02", "台账 conflicts 不是数组")
    return flow, mode_map


# ---------------------------------------------------------------- 结构校验

def find_unit_dir(ws, unit):
    if not unit:
        return None
    exact = os.path.join(ws, unit)
    if os.path.isdir(exact):
        return exact
    for name in os.listdir(ws):
        p = os.path.join(ws, name)
        if os.path.isdir(p) and unit in name:
            return p
    return None


def find_asset_dir(unit_dir, cfg):
    if not unit_dir:
        return None
    for name in cfg["asset_dir_names"]:
        p = os.path.join(unit_dir, name)
        if os.path.isdir(p):
            return p
    return None


def count_images(folder):
    n = 0
    if folder and os.path.isdir(folder):
        for dirpath, _dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(IMG_EXTS):
                    n += 1
    return n


def collect_texts(unit_dir, cfg, max_depth=3):
    out = []
    if not unit_dir:
        return out
    base = os.path.abspath(unit_dir)
    base_depth = base.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, files in os.walk(base):
        depth = dirpath.count(os.sep) - base_depth
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for fn in files:
            if os.path.splitext(fn)[1].lower() in cfg["text_exts"]:
                out.append(os.path.join(dirpath, fn))
    return out


def find_named_files(ws, cfg, patterns, exts=None, max_depth=4):
    hits = []
    for fp in walk_files(ws, cfg):
        base = os.path.basename(fp)
        if exts and os.path.splitext(base)[1].lower() not in exts:
            continue
        rel_depth = fp[len(os.path.abspath(ws)):].count(os.sep)
        if rel_depth > max_depth:
            continue
        for pat in patterns:
            if pat in base:
                hits.append(fp)
                break
    return hits


def check_structure(ws, cfg, rep, unit):
    unit_dir = find_unit_dir(ws, unit) if unit else None
    if unit:
        if not unit_dir:
            rep.err("WS-01", "未找到单元文件夹：%s" % unit)
        else:
            texts = collect_texts(unit_dir, cfg)
            if not texts:
                rep.err("WS-02", "单元 %s 内未找到分段文本" % unit)
    asset_dir = find_asset_dir(unit_dir, cfg) if unit_dir else None
    img_n = count_images(asset_dir)
    if unit and unit_dir and not asset_dir:
        rep.warn("WS-03", "单元 %s 未找到 资产图/资产 目录（文本模式可忽略）" % unit)
    return unit_dir, asset_dir, img_n


# ---------------------------------------------------------------- 提示词校验

def check_prompt_file(path, content, cfg, rep, mode=None, img_index=None, asset_dir=None, ws=None):
    rel = os.path.relpath(path, ws) if ws else path
    # P-01 槽位完整性
    present = {}
    for m in SLOT_RE.finditer(content):
        present.setdefault(m.group(0), m.start())
    for slot in REQUIRED_SLOTS:
        if slot not in present:
            rep.err("P-01", "%s 缺少必写槽位 %s" % (rel, slot))
    if "【资产参考】" not in present and mode == "asset_reference":
        rep.err("P-01", "%s 模式 asset_reference 但缺少【资产参考】" % rel)
    for slot in OPTIONAL_SLOTS:
        if slot not in present and slot != "【资产参考】":
            rep.info("P-01", "%s 缺少按需槽位 %s（无则忽略）" % (rel, slot))
    if not SHOT_RE.search(content):
        rep.err("P-06", "%s 未找到【镜头N】标记" % rel)

    # P-02 字数
    limit = int(cfg.get("char_limit", 9950))
    n = len(content)
    if n > limit:
        rep.err("P-02", "%s 字数超限：%d > %d" % (rel, n, limit))

    # P-03 图片引用：仅限【资产参考】节
    ref_span = None
    mr = re.search(r"【资产参考】", content)
    if mr:
        nxt = SLOT_RE.search(content, mr.end())
        ref_span = (mr.end(), nxt.start() if nxt else len(content))
    declared = []
    for m in IMG_RE.finditer(content):
        inside = ref_span is not None and ref_span[0] <= m.start() < ref_span[1]
        if inside:
            declared.append(os.path.basename(m.group(0).strip()))
        else:
            rep.err("P-03", "%s 第 %d 行 正文出现图片引用（仅允许在【资产参考】）：%s"
                    % (rel, line_of(content, m.start()), m.group(0).strip()))
    if ref_span is not None:
        body = content[:ref_span[0]] + content[ref_span[1]:]
        if "@" in body:
            rep.warn("P-03", "%s 正文出现 @ 引用符（应只用资产名）" % rel)
    if img_index is not None:
        for fn in declared:
            if fn.lower() not in img_index:
                rep.warn("P-03", "%s 声明的资产图未找到：%s" % (rel, fn))

    # P-04 跨段引用
    for pat in cfg.get("cross_ref_error", CROSS_REF_ERROR):
        for m in re.finditer(pat, content):
            rep.err("P-04", "%s 第 %d 行 跨段引用：%s" % (rel, line_of(content, m.start()), m.group(0)))
    for pat in cfg.get("cross_ref_warn", CROSS_REF_WARN):
        for m in re.finditer(pat, content):
            rep.warn("P-04", "%s 第 %d 行 疑似跨段引用：%s" % (rel, line_of(content, m.start()), m.group(0)))

    # P-05 站位图字样
    word = cfg.get("seatmap_word", "站位图")
    hits = [m for m in re.finditer(re.escape(word), content)]
    if hits:
        rep.warn("P-05", "%s 出现「%s」字样 %d 处（第 %d 行起，2D 站位图信息应转成纯文本）"
                 % (rel, word, len(hits), line_of(content, hits[0].start())))

    # P-06 镜头编号
    nums = [int(m.group(1)) for m in SHOT_RE.finditer(content)]
    if nums:
        dup = sorted({x for x in nums if nums.count(x) > 1})
        if dup:
            rep.err("P-06", "%s 镜头编号重复：%s" % (rel, ",".join(map(str, dup))))
        if sorted(set(nums)) != list(range(1, max(nums) + 1)):
            rep.warn("P-06", "%s 镜头编号不连续：%s" % (rel, ",".join(map(str, sorted(set(nums))))))

    # P-07 负面提示词非空
    mn = re.search(r"【负面提示词】", content)
    if mn:
        nxt = SLOT_RE.search(content, mn.end())
        seg = content[mn.end(): nxt.start() if nxt else len(content)].strip()
        if not seg:
            rep.err("P-07", "%s 【负面提示词】节为空" % rel)

    # P-08 槽位顺序
    idx = []
    for slot in SLOT_ORDER:
        if slot in present:
            idx.append((slot, present[slot]))
    seq = [s for s, _ in sorted(idx, key=lambda x: x[1])]
    expect = [s for s in SLOT_ORDER if s in present]
    if seq != expect:
        rep.warn("P-08", "%s 槽位顺序与契约不一致：%s" % (rel, "→".join(seq)))


def check_prompts(ws, cfg, rep, unit, mode, unit_dir=None):
    img_index = {}
    for fp in walk_files(ws, cfg):
        if fp.lower().endswith(IMG_EXTS):
            img_index.setdefault(os.path.basename(fp).lower(), fp)
    include = cfg.get("prompt_name_include", "提示词")
    excludes = cfg.get("prompt_name_exclude", [])
    scope_root = unit_dir if unit_dir else ws
    files = []
    for fp in walk_files(scope_root, cfg):
        base = os.path.basename(fp)
        ext = os.path.splitext(base)[1].lower()
        if ext not in (".txt", ".md"):
            continue
        if include not in base:
            continue
        if any(x in base for x in excludes):
            continue
        files.append(fp)
    for fp in files:
        check_prompt_file(fp, read_text(fp), cfg, rep, mode=mode, img_index=img_index, ws=ws)
    return files


# ---------------------------------------------------------------- 阶段门禁

def gate_check(ws, cfg, rep, stage, unit, unit_dir, asset_dir, img_n, mode):
    if stage in ("SCRIPT_PARSED", "ASSETS_INDEXED", "IMAGE_PROMPTS_READY", "VIDEO_PROMPTS_READY"):
        if not unit:
            rep.err("GATE-01", "阶段门禁 %s 需要 --unit 指定单元" % stage)
        elif not unit_dir:
            rep.err("GATE-01", "阶段 %s 门禁：单元文件夹缺失" % stage)
        else:
            texts = collect_texts(unit_dir, cfg)
            if not texts:
                rep.err("GATE-01", "阶段 %s 门禁：单元内无分段文本" % stage)
    if stage in ("ASSETS_INDEXED", "IMAGE_PROMPTS_READY", "VIDEO_PROMPTS_READY"):
        tables = find_named_files(ws, cfg, cfg["asset_table_patterns"], exts=(".txt", ".md", ".json"))
        if not tables:
            rep.err("GATE-01", "阶段 %s 门禁：未找到资产表文件（含「资产表」的文件）" % stage)
        else:
            for t in tables[:3]:
                content = read_text(t)
                for section in ("场景", "人物", "道具"):
                    if section not in content:
                        rep.warn("AT-02", "%s 缺少「%s」分区" % (os.path.relpath(t, ws), section))
    if stage in ("IMAGE_PROMPTS_READY", "VIDEO_PROMPTS_READY"):
        tables = find_named_files(ws, cfg, cfg["image_prompt_table_patterns"], exts=(".txt", ".md", ".xlsx", ".docx"))
        if not tables:
            rep.err("GATE-01", "阶段 %s 门禁：未找到资产图提示词表（含「人物场景提示词表」的文件）" % stage)
    if stage == "VIDEO_PROMPTS_READY":
        if unit and mode == "asset_reference" and img_n == 0:
            rep.err("GATE-02", "单元 %s 声明 asset_reference 但资产图目录为空/缺图" % unit)


# ---------------------------------------------------------------- 命令实现

def cmd_check(args):
    ws = os.path.abspath(args.workspace)
    if not os.path.isdir(ws):
        log("[ERROR] CFG-01 工作区不存在：%s" % ws)
        return 2
    cfg = read_config(ws, args.config)
    rep = Report()
    flow, mode_map = check_state(ws, cfg, rep)
    unit = args.unit
    mode = mode_map.get(unit) if unit else None
    unit_dir, asset_dir, img_n = check_structure(ws, cfg, rep, unit)
    if unit and flow is None:
        pass
    check_prompts(ws, cfg, rep, unit, mode, unit_dir=unit_dir)
    if args.stage:
        gate_check(ws, cfg, rep, args.stage, unit, unit_dir, asset_dir, img_n, mode)
    counts = rep.counts()

    if args.json:
        out = {
            "workspace": ws,
            "stage": args.stage,
            "unit": unit,
            "issues": [{"level": l, "code": c, "message": m} for l, c, m in rep.sorted_issues()],
            "counts": counts,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        log("== egg-aigc 校验报告 ==")
        log("工作区：%s" % ws)
        if args.stage:
            log("阶段：%s ｜ 单元：%s" % (args.stage, unit or "（全部）"))
        for level, code, msg in rep.sorted_issues():
            log("[%s] %s %s" % (level, code, msg))
        log("----")
        log("结果：ERROR %d ｜ WARN %d ｜ INFO %d ｜ 退出码 %d"
            % (counts["ERROR"], counts["WARN"], counts["INFO"], 1 if counts["ERROR"] else 0))
    return 1 if counts["ERROR"] else 0


def _empty_flow_unit():
    return {
        "stage": "CREATED",
        "stages_done": ["CREATED"],
        "stages_skipped": [],
        "video_input_mode": None,
        "artifacts": {},
        "failures": [],
        "history": [{"time": now_iso(), "stage": "CREATED", "action": "初始化", "result": "created"}],
    }


def cmd_init(args):
    ws = os.path.abspath(args.workspace)
    if not os.path.isdir(ws):
        log("[ERROR] CFG-01 工作区不存在：%s" % ws)
        return 2
    cfg = read_config(ws, args.config)
    state_dir = os.path.join(ws, cfg["state_dir"])
    os.makedirs(state_dir, exist_ok=True)
    flow_path = os.path.join(state_dir, FLOW_STATE_FILE)
    if os.path.isfile(flow_path):
        try:
            flow = load_json(flow_path)
        except (OSError, ValueError) as e:
            log("[ERROR] DS-01 流程状态.json 存在但无法解析：%s（请先修复）" % e)
            return 2
    else:
        flow = {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), "units": {}}
    added = []
    for unit in args.unit:
        if unit not in flow.setdefault("units", {}):
            flow["units"][unit] = _empty_flow_unit()
            added.append(unit)
    flow["updated_at"] = now_iso()
    dump_json(flow_path, flow)
    ledger_path = os.path.join(state_dir, LEDGER_FILE)
    if not os.path.isfile(ledger_path):
        dump_json(ledger_path, {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(),
                                "facts": [], "conflicts": []})
    log("已初始化状态：%s（新增单元：%s）" % (state_dir, "、".join(added) if added else "无（已存在）"))
    return 0


def cmd_advance(args):
    ws = os.path.abspath(args.workspace)
    cfg = read_config(ws, args.config)
    state_dir = os.path.join(ws, cfg["state_dir"])
    flow_path = os.path.join(state_dir, FLOW_STATE_FILE)
    if not os.path.isfile(flow_path):
        log("[ERROR] 状态未初始化：先执行 init")
        return 2
    flow = load_json(flow_path)
    units = flow.setdefault("units", {})
    unit = args.unit
    if unit not in units:
        log("[ERROR] 单元不存在：%s（先执行 init --unit %s）" % (unit, unit))
        return 2
    u = units[unit]
    new_stage = args.stage
    if new_stage not in STAGES:
        log("[ERROR] 阶段非法：%s" % new_stage)
        return 2
    cur = u.get("stage", "CREATED")
    if STAGES.index(new_stage) < STAGES.index(cur) and not args.force:
        log("[ERROR] 阶段只前进不回退：%s → %s；重跑请加 --force（版本 +1）" % (cur, new_stage))
        return 2
    u["stage"] = new_stage
    done = u.setdefault("stages_done", [])
    if new_stage not in done:
        done.append(new_stage)
    if args.mode:
        if args.mode not in MODES:
            log("[ERROR] 模式非法：%s（%s）" % (args.mode, "/".join(MODES)))
            return 2
        u["video_input_mode"] = args.mode
    for spec in args.artifact or []:
        if "=" not in spec:
            log("[WARN] 跳过无法解析的 --artifact：%s（格式 名称=相对路径）" % spec)
            continue
        name, rel = spec.split("=", 1)
        arts = u.setdefault("artifacts", {})
        old = arts.get(name)
        version = (old.get("version", 0) + 1) if isinstance(old, dict) else 1
        if name == "asset_images":
            arts[name] = {"path": rel, "complete": False}
        else:
            arts[name] = {"path": rel, "version": version, "verified": False}
        if not os.path.exists(os.path.join(ws, rel)):
            log("[WARN] DS-03 产物路径暂不存在：%s" % rel)
    u.setdefault("history", []).append(
        {"time": now_iso(), "stage": new_stage, "action": "advance", "result": "ok"})
    flow["updated_at"] = now_iso()
    dump_json(flow_path, flow)
    ledger_path = os.path.join(state_dir, LEDGER_FILE)
    if not os.path.isfile(ledger_path):
        dump_json(ledger_path, {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(),
                                "facts": [], "conflicts": []})
    log("已推进：%s → %s（模式：%s；产物：%s）"
        % (unit, new_stage, u.get("video_input_mode") or "-", ", ".join(args.artifact or []) or "-"))
    return 0


def cmd_fail(args):
    """记录失败/挂起：追加 failures[] 与 history[]。"""
    ws = os.path.abspath(args.workspace)
    cfg = read_config(ws, args.config)
    flow_path = os.path.join(ws, cfg["state_dir"], FLOW_STATE_FILE)
    if not os.path.isfile(flow_path):
        log("[ERROR] 状态未初始化：先执行 init")
        return 2
    flow = load_json(flow_path)
    unit = args.unit
    u = flow.get("units", {}).get(unit)
    if u is None:
        log("[ERROR] 单元不存在：%s" % unit)
        return 2
    stage = args.stage or u.get("stage", "CREATED")
    action = args.action or "终止"
    u.setdefault("failures", []).append(
        {"time": now_iso(), "stage": stage, "reason": args.reason, "action": action})
    u.setdefault("history", []).append(
        {"time": now_iso(), "stage": stage, "action": "fail", "result": action})
    flow["updated_at"] = now_iso()
    dump_json(flow_path, flow)
    log("已记录失败：%s ｜ %s ｜ %s（%s）" % (unit, stage, args.reason, action))
    return 0


def cmd_fact(args):
    """写入/更新连续性台账事实（同 id 覆盖并版本 +1）。"""
    ws = os.path.abspath(args.workspace)
    cfg = read_config(ws, args.config)
    ledger_path = os.path.join(ws, cfg["state_dir"], LEDGER_FILE)
    if not os.path.isfile(ledger_path):
        log("[ERROR] 台账未初始化：先执行 init")
        return 2
    ledger = load_json(ledger_path)
    facts = ledger.setdefault("facts", [])
    fid = args.id
    conf = args.confidence or "confirmed"
    if conf not in CONFIDENCE:
        log("[ERROR] confidence 非法：%s（%s）" % (conf, "/".join(CONFIDENCE)))
        return 2
    existing = next((f for f in facts if isinstance(f, dict) and f.get("id") == fid), None)
    if existing:
        existing.update({
            "value": args.value, "source": args.source, "confidence": conf,
            "version": int(existing.get("version", 1)) + 1, "updated_at": now_iso(),
        })
        if args.subject:
            existing["subject"] = args.subject
        if args.field:
            existing["field"] = args.field
        if args.verified:
            existing["verified"] = True
        existing.setdefault("verified", False)
        verb = "更新"
    else:
        if not args.subject or not args.field:
            log("[ERROR] 新事实需要 --subject 与 --field")
            return 2
        facts.append({
            "id": fid, "unit": args.unit, "subject": args.subject, "field": args.field,
            "value": args.value, "source": args.source, "confidence": conf,
            "verified": bool(args.verified), "version": 1, "updated_at": now_iso(),
        })
        verb = "新增"
    ledger["updated_at"] = now_iso()
    dump_json(ledger_path, ledger)
    log("已%s事实：%s = %s（%s，%s）" % (verb, fid, args.value, conf, args.source))
    return 0


def cmd_checks(_args):
    log("== 校验项清单 ==")
    for code, level, desc in CHECK_DOCS:
        log("%-8s %-5s %s" % (code, level, desc))
    return 0


# ---------------------------------------------------------------- 自检

def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _valid_prompt_text():
    return (
        "【资产参考】\n人物资产参考 ARES:ARES.png\n\n"
        "【场景】城堡大厅，夜，烛火。\n\n"
        "【站位设计】ARES 站画面中央，面朝门口。\n\n"
        "【人物】ARES 黑铜胸甲；情绪克制。\n\n"
        "【道具】长矛：青铜矛头，握于右手。\n\n"
        "【拍摄参数】35mm，f/2.8。\n\n"
        "【影调】拍摄体系：IMAX；色温：3200K；光源：壁炉火光。\n\n"
        "【人物情绪时间轴】镜头1：ARES 克制。\n\n"
        "【镜头1】00:00-00:05 抬眼\n拍：中景\n景别：中景\n动作：抬眼\n道具：长矛\n台词：\"Who goes there?\"（放缓）\n"
        "【镜头2】00:05-00:10 转身\n拍：近景\n景别：近景\n动作：转身\n道具：长矛\n台词：\"Stay back.\"（一字一顿）\n\n"
        "【逻辑约束】时间：夜。\n\n"
        "【负面提示词】画质：模糊；人物：变形。\n\n"
        "【声音时间轴】0-5s 环境声+台词。\n"
    )


def cmd_selftest(_args):
    tmp = tempfile.mkdtemp(prefix="eggaigc_selftest_")
    failures = []
    try:
        # fixture 1：合规工作区
        ws1 = os.path.join(tmp, "ws_ok")
        _write(os.path.join(ws1, "状态", "流程状态.json"), json.dumps({
            "schema_version": "1.0", "updated_at": now_iso(),
            "units": {"第1集": {"stage": "VIDEO_PROMPTS_READY",
                                "stages_done": ["CREATED", "SCRIPT_PARSED", "ASSETS_INDEXED",
                                                "IMAGE_PROMPTS_READY", "VIDEO_PROMPTS_READY"],
                                "stages_skipped": [], "video_input_mode": "asset_reference",
                                "artifacts": {}, "failures": [], "history": []}}},
            ensure_ascii=False))
        _write(os.path.join(ws1, "状态", "连续性台账.json"), json.dumps({
            "schema_version": "1.0", "updated_at": now_iso(), "facts": [{
                "id": "第1集.ARES.costume", "unit": "第1集", "subject": "人物:ARES",
                "field": "costume", "value": "黑铜胸甲", "source": "剧本:1-1",
                "confidence": "confirmed", "verified": True, "version": 1}],
            "conflicts": []}, ensure_ascii=False))
        _write(os.path.join(ws1, "资产表.txt"), "场景\n    x1集 大厅\n人物\n    ARES 男 黑铜胸甲\n道具\n    长矛 所属集 青铜\n")
        _write(os.path.join(ws1, "资产图提示词", "人物场景提示词表.txt"), "人物\n    ARES\n        提示词（略）\n")
        _write(os.path.join(ws1, "第1集", "资产图", "ARES.png"), "fake")
        _write(os.path.join(ws1, "第1集", "1", "提示词1.txt"), _valid_prompt_text())
        rep1 = Report()
        cfg = dict(DEFAULT_CONFIG)
        flow, mode_map = check_state(ws1, cfg, rep1)
        unit_dir, asset_dir, img_n = check_structure(ws1, cfg, rep1, "第1集")
        check_prompts(ws1, cfg, rep1, "第1集", "asset_reference", unit_dir=unit_dir)
        gate_check(ws1, cfg, rep1, "VIDEO_PROMPTS_READY", "第1集", unit_dir, asset_dir, img_n, "asset_reference")
        c1 = rep1.counts()
        if c1["ERROR"]:
            failures.append("fixture1 应有 0 ERROR，实际 %d：%s"
                            % (c1["ERROR"], [i for i in rep1.issues if i[0] == "ERROR"]))

        # fixture 2：问题提示词
        bad = _valid_prompt_text()
        bad = bad.replace("【声音时间轴】", "【删掉的声音】")
        bad = bad.replace("\"Stay back.\"（一字一顿）", "\"Stay back.\"（一字一顿，与第2段一致）")
        bad = bad.replace("【镜头2】", "【镜头2】\n拍：近景\n正文泄漏 shot.png\n【镜头2】")
        bad += "啊" * 11000
        _write(os.path.join(ws1, "第1集", "1", "提示词2.txt"), bad)
        rep2 = Report()
        check_prompts(ws1, cfg, rep2, "第1集", "asset_reference", unit_dir=unit_dir)
        codes2 = {c for _l, c, _m in rep2.issues}
        if "P-01" not in codes2:
            failures.append("fixture2 应报 P-01（缺槽位）")
        if "P-02" not in codes2:
            failures.append("fixture2 应报 P-02（超字数）")
        if not any(c == "P-04" and "与第2段" in m for _l, c, m in rep2.issues):
            failures.append("fixture2 应报 P-04（与第2段一致）")
        if not any(c == "P-03" and "shot.png" in m for _l, c, m in rep2.issues):
            failures.append("fixture2 应报 P-03（正文图片泄漏）")
        if not any(c == "P-06" and "重复" in m for _l, c, m in rep2.issues):
            failures.append("fixture2 应报 P-06（镜头重复）")

        # fixture 3：坏状态
        ws3 = os.path.join(tmp, "ws_bad")
        _write(os.path.join(ws3, "状态", "流程状态.json"), json.dumps({
            "schema_version": "1.0", "updated_at": now_iso(),
            "units": {"第1集": {"stage": "NOPE", "stages_done": ["BAD"],
                                "video_input_mode": "zzz", "artifacts": {}, "failures": [], "history": []}}},
            ensure_ascii=False))
        _write(os.path.join(ws3, "状态", "连续性台账.json"), json.dumps({
            "schema_version": "1.0", "updated_at": now_iso(),
            "facts": [{"id": "x", "subject": "s", "field": "f", "value": "v",
                       "source": "剧本:1-1", "confidence": "maybe", "verified": True, "version": 1}],
            "conflicts": []}, ensure_ascii=False))
        rep3 = Report()
        check_state(ws3, dict(DEFAULT_CONFIG), rep3)
        codes3 = {c for _l, c, _m in rep3.issues}
        if "DS-01" not in codes3 or "DS-02" not in codes3:
            failures.append("fixture3 应报 DS-01 与 DS-02，实际 %s" % sorted(codes3))

        # fixture 4：init 与 advance 往返
        ws4 = os.path.join(tmp, "ws_flow")
        os.makedirs(ws4, exist_ok=True)
        ns = argparse.Namespace(workspace=ws4, config=None, unit=["第2集"])
        if cmd_init(ns) != 0:
            failures.append("fixture4 init 失败")
        ns2 = argparse.Namespace(workspace=ws4, config=None, unit="第2集",
                                 stage="SCRIPT_PARSED", mode=None,
                                 artifact=["segments=第2集/"], force=False)
        if cmd_advance(ns2) != 0:
            failures.append("fixture4 advance 失败")
        rep4 = Report()
        flow4, _m = check_state(ws4, dict(DEFAULT_CONFIG), rep4)
        st = flow4["units"]["第2集"]["stage"] if flow4 else None
        if st != "SCRIPT_PARSED":
            failures.append("fixture4 推进后 stage 应为 SCRIPT_PARSED，实际 %s" % st)
        # 回退应被拒绝
        ns3 = argparse.Namespace(workspace=ws4, config=None, unit="第2集",
                                 stage="CREATED", mode=None, artifact=None, force=False)
        if cmd_advance(ns3) == 0:
            failures.append("fixture4 应拒绝阶段回退")
        # fact 新增 / 更新（版本 +1）/ fail 记录
        ns_f1 = argparse.Namespace(workspace=ws4, config=None, unit="第2集",
                                   id="第2集.ARES.costume", subject="人物:ARES", field="costume",
                                   value="黑铜胸甲", source="剧本:2-1", confidence=None, verified=False)
        if cmd_fact(ns_f1) != 0:
            failures.append("fixture4 fact 新增失败")
        ns_f2 = argparse.Namespace(workspace=ws4, config=None, unit="第2集",
                                   id="第2集.ARES.costume", subject=None, field=None,
                                   value="黑铜胸甲+绷带", source="剧本:2-2", confidence="confirmed", verified=True)
        if cmd_fact(ns_f2) != 0:
            failures.append("fixture4 fact 更新失败")
        ns_fail = argparse.Namespace(workspace=ws4, config=None, unit="第2集",
                                     reason="缺图", stage=None, action="挂起")
        if cmd_fail(ns_fail) != 0:
            failures.append("fixture4 fail 记录失败")
        led = load_json(os.path.join(ws4, "状态", "连续性台账.json"))
        f0 = led["facts"][0] if led.get("facts") else {}
        if f0.get("version") != 2 or f0.get("value") != "黑铜胸甲+绷带":
            failures.append("fixture4 fact 版本/值不符：%s" % f0)
        rep5 = Report()
        check_state(ws4, dict(DEFAULT_CONFIG), rep5)
        if rep5.counts()["ERROR"]:
            failures.append("fixture4 状态校验应无 ERROR：%s"
                            % [i for i in rep5.issues if i[0] == "ERROR"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        log("== selftest 结果：FAIL ==")
        for f in failures:
            log(" - " + f)
        return 1
    log("== selftest 结果：PASS（4 组用例全部通过）==")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        prog="校验器.py",
        description="egg-aigc 轻量执行/校验层：状态转移（init/advance）与结构校验（check）")
    parser.add_argument("action", nargs="?", default=None,
                        help="check / init / advance / selftest / checks；省略时按 check（首个参数视为工作区）")
    parser.add_argument("workspace", nargs="?", default=None, help="工作区路径")
    parser.add_argument("--stage", default=None, help="阶段（check 门禁 / advance 目标阶段）")
    parser.add_argument("--unit", action="append", default=None, help="单元名（可多次；check 取首个）")
    parser.add_argument("--mode", default=None, help="video_input_mode：asset_reference/text_only/hybrid")
    parser.add_argument("--artifact", action="append", default=None, help="产物 名称=相对路径（可多次）")
    parser.add_argument("--force", action="store_true", help="允许阶段回退（重跑，版本 +1）")
    parser.add_argument("--config", default=None, help="校验配置.json 路径（默认读 工作区/校验配置.json）")
    parser.add_argument("--json", action="store_true", help="check 输出 JSON")
    parser.add_argument("--reason", default=None, help="fail：失败原因（一句话）")
    parser.add_argument("--action", default=None, help="fail：处理 终止/挂起/已询问用户（默认 终止）")
    parser.add_argument("--id", default=None, help="fact：事实 id（unit.主体.字段）")
    parser.add_argument("--subject", default=None, help="fact：主体（人物:xx/道具:xx/场景:xx/时间线）")
    parser.add_argument("--field", default=None, help="fact：字段（costume/appearance/prop_hold/prop_state/location/state/timeline/relationship/end_state）")
    parser.add_argument("--value", default=None, help="fact：事实内容（一句话）")
    parser.add_argument("--source", default=None, help="fact：来源（剧本:x-a／用户:…／资产表／推断）")
    parser.add_argument("--confidence", default=None, help="fact：confidence（confirmed/inferred/unknown，默认 confirmed）")
    parser.add_argument("--verified", action="store_true", help="fact：标记用户已确认")
    args = parser.parse_args(argv)

    known = ("check", "init", "advance", "fail", "fact", "selftest", "checks")
    if args.action in known:
        action = args.action
    else:
        # 省略子命令：首参数视为工作区，动作按 check
        action = "check"
        if args.workspace is None and args.action is not None:
            args.workspace = args.action
        args.action = "check"

    if action == "selftest":
        return cmd_selftest(args)
    if action == "checks":
        return cmd_checks(args)

    if not args.workspace:
        parser.print_help()
        return 2
    args.unit = args.unit or []
    if action == "check":
        args.unit = args.unit[0] if args.unit else None
        return cmd_check(args)
    if action == "init":
        if not args.unit:
            log("[ERROR] init 需要 --unit")
            return 2
        return cmd_init(args)
    if action == "advance":
        if not args.unit or not args.stage:
            log("[ERROR] advance 需要 --unit 与 --stage")
            return 2
        args.unit = args.unit[0]
        return cmd_advance(args)
    if action == "fail":
        if not args.unit or not args.reason:
            log("[ERROR] fail 需要 --unit 与 --reason")
            return 2
        args.unit = args.unit[0]
        return cmd_fail(args)
    if action == "fact":
        if not args.unit or not args.id or not args.value or not args.source:
            log("[ERROR] fact 需要 --id --value --source（新事实另需 --subject 与 --field）")
            return 2
        args.unit = args.unit[0]
        return cmd_fact(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
