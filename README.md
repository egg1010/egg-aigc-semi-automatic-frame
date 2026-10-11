# egg-aigc-semi-automatic-frame

半自动影视 AIGC 框架：把剧本理解、资产连续性、镜头设计、图片／视频提示词与受控重构拆分为可复用的 Markdown 模块，由智能体（Agent）按调度器执行。

## 启动方式

让智能体运行 目录下的 `全局调度器.md`（重构独立入口：`设计与重构.md`）。

## 目录结构

- `规范/` —— 项目规范、异常设计、自定义拆分格式、多集要求、流程状态机、数据契约
- `文本模块/` —— 剧本理解、文本拆分、资产表
- `图片提示词模块/` —— 人物/场景/道具资产图提示词
- `视频模块/` —— 分镜提示词（图片/文本/混合模式）、基础格式、重构基础模块
- `通用模块/` —— 材质/皮肤/光线/色彩 四库 ＋ 联动模块 ＋ 光线设计 ＋ 图片分析限制
- `工具/` —— 校验器（状态转移与结构校验）、回归测试
- `runtime/` —— 记忆层与校验层（SQLite 存储、候选提取、检索、上下文组装、Schema 校验）
- `schemas/` —— 结构化输出 JSON Schema（剧本分析／事件提案／资产清单／分镜）
- 工作区侧：`状态/`（流程状态与连续性台账）、`data/`（受控记忆库，不入版本库）、`异常/`

## 执行保障（0.0.4-beta 新增）

- **状态存储层（SQLite 记忆库）**：`data/projects/<project_id>/project.db` —— 原文与分块、实体与场次、事件与当前状态、摘要、审计日志；事实可**逐条追溯到原文块**
- **防污染机制**：AI 只产候选，不写库；事实变更须带原文证据，经「预期旧值核对 → 事务提交」；冲突或证据不足的提案**不得**进入已确认事实
- **certainty 分级**：`explicit`（可进审批队列）／`inferred`（默认不进正式状态，需用户确认）／`unknown`（不得进入）
- **检索与上下文预算**：精确状态优先于语义检索；上下文按「剧本原文 > 规则 > 已确认状态 > 历史证据 > 摘要 > 泛语义」排优先级，超预算**主动提示**而非静默裁剪
- **校验层（Schema 化）**：JSON Schema ＋ 引用真实性 ＋ 状态连续性 ＋ 授权分级；第 7 步槽位门禁**委派**既有 `工具/校验器.py`，行为不回退（`runtime/VALIDATORS.md`）
- **规则版本化**：规范以 `path + sha256` 标识；哈希变化使缓存失效并**强制重读**；未人工审核的新规范禁止用于正式交付

## 执行保障（0.0.3-beta 新增）

- **阶段状态机**：CREATED → SCRIPT_PARSED → ASSETS_INDEXED → IMAGE_PROMPTS_READY → VIDEO_PROMPTS_READY → REVIEWED → DONE，每阶段带输入/产物/校验/失败策略（`规范/流程状态机.md`）
- **状态落盘**：流程状态 ＋ 连续性台账两个版本化 JSON（工作区 `状态/`），支持断点续跑、冲突标记与版本防护
- **机器校验**：`工具/校验器.py` —— 槽位完整性／字数上限／资产引用／跨段引用禁令／镜头编号／阶段门禁
- **规范优先级**：安全/硬约束 > 项目规范 > 全局流程 > 模块规范 > 默认策略（`跟新规范.md`）
- **受控重构**：格式／风格／视听层／内容层 授权分级 ＋ 字段级差异清单 ＋ 版本防覆盖
- **提示词分层**：制作规格（ProductionSpec，对话内）与 模型提示词（ModelPrompt，交付文件）分离，压缩有预算与优先级

## 记忆层快速开始

```bash
python runtime/main.py init    --project film_001 --name "演示短片"
python runtime/main.py ingest  --project film_001 --path 剧本/第三场.txt --scene scene_3 --scene-order 3
python runtime/main.py extract --project film_001 --chunk <证据块id>     # 只产候选，不写库
python runtime/main.py state   --project film_001 --scene scene_3
python runtime/main.py verify  --stage storyboard --input 分镜.json --project film_001
python runtime/main.py rules   --diff                                    # 规范版本变化
python -m unittest discover -s runtime/tests                             # 全部测试
```

## 路线图（P2 · 按需落地）

- 向量检索与重排（Embedding 表，换模型需重建索引）
- 图片／视频服务适配器与能力声明（同一制作规格导出不同模型 Prompt）
- 事件重放与分支版本（历史回写的完整自动化）
- 多人协作与任务租约、监控面板

## 版本

见 `version` 文件。
