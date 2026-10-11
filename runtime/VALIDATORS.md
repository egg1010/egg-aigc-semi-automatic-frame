# 校验层接口说明

依据：架构改造方案 v2 §6、§8.7
定位：**状态机不变，校验层变更** —— 本层在保留既有 `工具/校验器.py` 全部能力的前提下，
增加 JSON Schema 校验与九步校验次序，并把结果统一为结构化机读格式。

## 两层关系

| 层 | 文件 | 职责 |
|---|---|---|
| 结构门禁（既有） | `工具/校验器.py` | DS/WS/AT/P/GATE 检查项，工作区结构、槽位、门禁、状态文件 |
| Schema 校验（新增） | `runtime/validators.py` + `schemas/` | 模型输出结构、引用真实性、状态连续性、权限分级 |

`runtime/validators.py` **调用**既有校验器，不重新实现其检查逻辑；
既有校验器仍是结构门禁的唯一真值，行为不回退。

## 九步校验次序（§6.2）

```
1. 输出是否为有效 JSON
2. JSON Schema 是否通过
3. 引用的 project/scene/entity/asset/source 是否真实存在
4. 是否遗漏必需的原剧本动作、台词或镜头事实
5. 状态连续性检查：时间、道具归属、服装、位置
6. 规则权限检查：是否允许改剧情、是否允许改风格
7. 交付文件的格式槽位是否齐全        ← 委派 工具/校验器.py
8. 人工审批（需要时）
9. DB 事务提交，最后才发布交付物
```

第 9 步由 `runtime/memory_store.approve_event()` 在事务内完成，本层只负责放行或拦截。

## 可验证程度必须分级（§6.2）

- **硬约束**（机器判）：ID 存在、输出字段、道具归属、槽位齐全
- **软约束**（人工/评价模型评审）：「更有电影感」「镜头更优美」等美学判断，
  不得伪装成 Schema 可证明的约束

## 使用

```bash
# 校验一个模型输出对象
python runtime/validators.py check --project film_001 --stage storyboard \
    --input candidate.json

# 仅跑工作区结构门禁（等价于既有校验器）
python runtime/validators.py gate --workspace <项目工作区> --stage VIDEO_PROMPTS_READY

# 机器可读输出
python runtime/validators.py check --project film_001 --stage storyboard \
    --input candidate.json --json
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 通过（允许 INFO/WARN） |
| 1 | 存在 ERROR，禁止提交 |
| 2 | 配置或输入不可读 |
