# distill-kb

**让 AI agent 用"蒸馏笔记 + 关键词检索"管理你的本地知识库——零依赖、可溯源、带失效治理。**

一条与主流路线相反的 bets：当几乎所有 agent 记忆方案都在卷 embedding 和向量库时，这个项目证明——**对于个人规模的本地知识库，一份纪律严格的蒸馏笔记，胜过一个黑盒相似度索引。**

> 设计立场受 [Lemmalog](https://github.com/JordyZomer/lemmalog)（Datalog agent 记忆引擎）启发：记忆应该可验证、可溯源、能声明失效。本项目是同一问题的"零基础设施"答案——不用规则引擎，用蒸馏纪律 + 两个 frontmatter 字段实现同类治理。

## 为什么不用向量库

| | 向量库路线 | distill-kb 路线 |
|---|---|---|
| 检索原理 | embedding 相似度（黑盒） | BM25 + 蒸馏时写好的"别称桥"（可解释） |
| 检索结果 | 一段"相关"文本，对错未知 | 结构化笔记，能直接回答问题、能追溯源文件 |
| 事实冲突 | 静默共存，你不知道 | **显式标注**，机器可枚举（`conflicts` 命令） |
| 结论被推翻 | 旧结论仍以"相关"面目出现 | **失效传播**：旧笔记打 `superseded_by`，检索旧说法第一命中即带失效声明 |
| 依赖 | embedding 服务 + 向量库 | **零。纯 Python 标准库，单文件脚本** |
| 语义改写 | 强 | 弱（靠别称桥部分弥补）——这是诚实的代价 |

适用判断：**几百篇以内的个人资料库、专有名词/数字密集（研报、群聊、会议纪要、技术笔记）、在意"记得对不对"胜过"记得多全"——选这里。** 百万级语料、自由语义搜索，请用向量库。

## 工作模型

```
放入资料 → agent 逐篇蒸馏成结构化笔记 → 提问时先检索笔记，再作答/汇总
```

```text
KnowledgeBase/            源资料（你按目录管理，脚本只读）
├── 投资/
├── 技术/
└── .kb/
    ├── notes/            蒸馏笔记（镜像源目录结构）
    └── state.json        sha256 增量状态
```

脚本负责机械的部分：增量扫描（sha256，增/改/删自动同步）、BM25 检索（中文 bigram + 英文词法）、标签/实体/主题聚合、冲突与失效枚举。**蒸馏由你的 agent 按 SOP 完成**——这正是设计：LLM 做它擅长的（理解、提炼、写别称桥），脚本做它擅长的（确定性、可审计）。

## 蒸馏笔记长什么样

每份资料一篇，frontmatter 供机器聚合，正文供检索与直接作答：

```markdown
---
source: 投资/2026-09-19_群聊_台积电CoWoS产能.md
topic: 投资
source_type: 群聊
date: 2026-09-19
tags: [台积电, TSMC, CoWoS, 先进封装, SoIC, HBM4, 扩产]
entities: [台积电, AMD, SK海力士]
superseded_by: 投资/2026-09-20_官方纪要_台积电扩产修正.md   # ← 被推翻时
conflicts_with: []                                          # ← 口径冲突时双向互写
---

# 蒸馏笔记：台积电 CoWoS 产能群聊

**一句话**：……

## 事实与观点
- [2026-09-19] 观点(大熊)：扩产计划不变
- [2026-09-20] 已失效(2026-09-20, 被…推翻)：扩产计划不变——官方口径为"不准确"…
- [2026-09-19] 传闻/未验证：设备商口径 2027 年 60-70k/月

## 问答锚点
- 问：台积电 CoWoS 扩产有变化吗？→ ⚠已被 2026-09-20 官方纪要推翻…

## 关键词
台积电、TSMC、CoWoS、先进封装、扩产…
```

四条核心纪律（完整版见 [SKILL.md](SKILL.md)）：

1. **事实与观点分家**：观点必须注明是谁说的，传闻必须标注未验证
2. **别称桥**：正文写 CoWoS，tags 必须带「先进封装」；正文写 NVDA，tags 带「英伟达」——BM25 的同义词短板用纪律补
3. **数字逐字保留**：不四舍五入、不把"计划"写成"已实现"
4. **失效传播 + 冲突互标**：新资料推翻旧结论 → 旧笔记标 `superseded_by`（不删除，历史可追溯）；说法冲突但均未被推翻 → 双向 `conflicts_with`

## 快速开始

```bash
# 零依赖：Python 3.9+ 标准库
git clone https://github.com/aloasut/distill-kb.git
export KB_ROOT=~/Documents/KnowledgeBase    # 你的知识库目录
python3 distill-kb/scripts/kb.py scan       # 增量扫描
python3 distill-kb/scripts/kb.py todo --cap 5   # 取一批待蒸馏
# → 把 todo 输出交给你的 agent（Claude Code / Hermes 等），按 SKILL.md 蒸馏
python3 distill-kb/scripts/kb.py mark <相对路径> done --title "短标题"
python3 distill-kb/scripts/kb.py search "你的问题关键词"
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `scan` | 增量扫描：新增/变更(自动回待蒸馏)/删除(连笔记一起删) |
| `todo [--cap N]` | 待蒸馏队列（失败优先），每轮小批量控制 token |
| `mark REL done\|failed\|pending` | 标记蒸馏结果 |
| `search QUERY [--top N]` | BM25 检索：笔记优先，未蒸馏原文兜底 |
| `tags` | 全库标签/实体/主题聚合索引 |
| `notes --tag/--entity/--topic` | 汇总入口：按日期升序列笔记，失效⚠/冲突⚡可见 |
| `conflicts` | 冲突标注对 + 已失效清单（汇总前必查） |
| `status` | 总览 + 知识一致性计数 |

## 验证记录（真实压测，非纸面设计）

9 类压力语料实测通过：群聊（观点归属）、研报 vs 群聊口径冲突、178KB 大报告深埋指标（120万预算/≥0.85召回/≤800ms 均可召回）、二进制 PDF（正确标失败不幻觉）、会议纪要（人+截止日问答）、别称改写检索（"先进封装产能"命中纯 CoWoS 笔记并排第一）、失效传播实战（官方纪要推翻群聊口径后，检索旧说法第一命中即带失效声明）。样例见 [examples/](examples/)。

## 定位与边界（诚实版）

- 这是一个 **agent skill**（给 Claude Code / Hermes Agent 等 harness 用），不是开箱即用的独立产品——蒸馏质量取决于你 agent 执行 SOP 的纪律
- 不做：向量检索、自动 OCR（扫描件会正确标 `failed` 等你接 OCR）、WebUI、置信度评分
- 单作者、个人规模验证（数百篇）。到千篇级若别称桥纪律开始漏召回，再考虑本地 embedding 混合检索

## License

MIT
