"""规则加载与版本化 —— 架构改造方案 v2 §8.2 / §10.3

要求（用户定：状态机不变）：
    保留「每次运行都要确认最新规范」的原始目的，
    但把「禁止任何记忆文件」改为「受控记忆系统例外」。

核心机制：
    - 规则以 path + sha256 作为版本标识
    - 哈希变化 → 旧缓存失效并强制重读
    - 规范更新未人工审核时，禁止用旧版缓存做正式交付
    - 规则版本随上下文包下发给模型（applicable_rules）
"""

from __future__ import annotations

import hashlib
import json
import os
import time

# 参与版本化管理的规范/格式文件（与 跟新规范.md 的强制阅读清单对齐）
RULE_FILES = (
    "全局调度器.md",
    "跟新规范.md",
    "设计与重构.md",
    "规范/流程状态机.md",
    "规范/数据契约.md",
    "规范/异常设计.md",
    "通用模块/联动模块.md",
    "文本模块/理解模块.txt",
    "文本模块/文本拆分模块.md",
    "文本模块/资产表模块.md",
    "图片提示词模块/提示词基础格式.txt",
    "视频模块/基础格式.txt",
    "视频模块/重构基础模块.md",
)

CACHE_FILE = ".rule_cache.json"


class RuleChanged(Exception):
    """规则内容变化但尚未人工审核：禁止用旧缓存做正式交付。"""

    def __init__(self, message, *, path=None, old_hash=None, new_hash=None,
                 error_code="RULE_CHANGED_UNAPPROVED"):
        super().__init__(message)
        self.message = message
        self.path = path
        self.old_hash = old_hash
        self.new_hash = new_hash
        self.error_code = error_code

    def to_dict(self):
        return {"error_code": self.error_code, "message": self.message,
                "path": self.path, "old_hash": self.old_hash,
                "new_hash": self.new_hash}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class RuleVersion:
    __slots__ = ("path", "sha256", "content", "mtime")

    def __init__(self, path, sha256, content, mtime):
        self.path = path
        self.sha256 = sha256
        self.content = content
        self.mtime = mtime

    def to_ref(self) -> dict:
        """下发给模型的规则引用：只带路径与哈希，不带全文。"""
        return {"path": self.path, "sha256": self.sha256}

    def __repr__(self):
        return f"RuleVersion({self.path}, {self.sha256[:12]})"


class KnowledgeLoader:
    """规范加载器：哈希校验 + 缓存失效。

    缓存只用于避免重复读盘，**不允许**用旧哈希的内容冒充当前规范。
    """

    def __init__(self, root: str, *, cache_enabled: bool = True):
        self.root = os.path.abspath(root)
        self.cache_enabled = cache_enabled
        self._cache: dict[str, RuleVersion] = {}
        self._cache_path = os.path.join(self.root, CACHE_FILE)
        self._loaded_from_disk = False

    # -- 读取 -------------------------------------------------------------

    def load(self, rel_path: str, *, approved_hashes: dict | None = None,
             require_approved: bool = False) -> RuleVersion:
        """读取单个规则文件。

        require_approved=True 时，若哈希与 approved_hashes 记录不一致，
        抛 RuleChanged：不能用未审核的新规范做正式交付（§8.2）。
        """
        abs_path = self._resolve(rel_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"规范文件不存在：{rel_path}")

        disk_hash = sha256_file(abs_path)
        cached = self._cache.get(rel_path)

        if require_approved and approved_hashes is not None:
            approved = approved_hashes.get(rel_path)
            if approved and approved != disk_hash:
                raise RuleChanged(
                    f"规范 {rel_path} 内容已变化（{approved[:12]} → "
                    f"{disk_hash[:12]}），尚未人工审核，禁止用旧版缓存正式交付",
                    path=rel_path, old_hash=approved, new_hash=disk_hash)

        if cached is not None and cached.sha256 == disk_hash:
            return cached  # 未变化，复用缓存

        with open(abs_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        ver = RuleVersion(rel_path, disk_hash, content,
                          os.path.getmtime(abs_path))
        self._cache[rel_path] = ver
        return ver

    def load_many(self, rel_paths, **kw) -> dict:
        out = {}
        for p in rel_paths:
            try:
                out[p] = self.load(p, **kw)
            except (FileNotFoundError, RuleChanged) as e:
                out[p] = e
        return out

    def load_all(self, **kw) -> dict:
        return self.load_many(RULE_FILES, **kw)

    # -- 版本清单 ---------------------------------------------------------

    def snapshot(self) -> dict:
        """当前所有规范的 path→sha256 清单，用于人工审核留痕。"""
        snap = {}
        for rel in RULE_FILES:
            try:
                abs_path = self._resolve(rel)
                if os.path.isfile(abs_path):
                    snap[rel] = sha256_file(abs_path)
            except (OSError, ValueError):
                continue
        return snap

    def diff_against(self, approved: dict) -> dict:
        """对比已审核清单，返回变更明细。"""
        current = self.snapshot()
        changed, added, removed = {}, {}, {}
        for path, h in current.items():
            if path not in approved:
                added[path] = h
            elif approved[path] != h:
                changed[path] = {"old": approved[path], "new": h}
        for path in approved:
            if path not in current:
                removed[path] = approved[path]
        return {"changed": changed, "added": added, "removed": removed,
                "has_change": bool(changed or added or removed)}

    def invalidate(self, rel_path: str | None = None) -> int:
        """强制使缓存失效；返回失效条数。"""
        if rel_path is None:
            n = len(self._cache)
            self._cache.clear()
            return n
        return 1 if self._cache.pop(rel_path, None) else 0

    # -- 缓存文件（仅存哈希，不存内容）------------------------------------

    def save_snapshot(self, path: str | None = None) -> str:
        path = path or self._cache_path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "hashes": self.snapshot()}, fh,
                      ensure_ascii=False, indent=2)
        return path

    def load_snapshot(self, path: str | None = None) -> dict:
        path = path or self._cache_path
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("hashes", {})

    # -- 内部 -------------------------------------------------------------

    def _resolve(self, rel_path: str) -> str:
        """限制在工作区范围内，防目录穿越（§10.1）。"""
        abs_path = os.path.abspath(os.path.join(self.root, rel_path))
        if not abs_path.startswith(self.root):
            raise ValueError(f"路径越界：{rel_path}")
        return abs_path


class RuleCache:
    """带失效机制的规则缓存：只在哈希一致时复用。

    与 KnowledgeLoader 的区别：本类用于**跨阶段**缓存，强调「哈希变化即失效」。
    """

    def __init__(self, loader: KnowledgeLoader):
        self.loader = loader
        self._entries: dict[str, RuleVersion] = {}

    def get(self, rel_path: str) -> RuleVersion:
        ver = self.loader.load(rel_path)
        old = self._entries.get(rel_path)
        if old is not None and old.sha256 != ver.sha256:
            # 哈希变化 → 旧条目失效（同时提示依赖它的产物需重算）
            self._entries[rel_path] = ver
            raise RuleChanged(
                f"规范 {rel_path} 已更新，缓存失效，依赖它的产物需重新生成",
                path=rel_path, old_hash=old.sha256, new_hash=ver.sha256,
                error_code="RULE_CACHE_INVALIDATED")
        self._entries[rel_path] = ver
        return ver

    def peek(self, rel_path: str) -> RuleVersion | None:
        return self._entries.get(rel_path)
