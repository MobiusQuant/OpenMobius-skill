# 更新日志

本文件记录 **OpenMobius-skill** 的版本变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

English: [CHANGELOG.md](./CHANGELOG.md)

---

## [未发布]

### 新增

- 新增对 Claude Code、Codex、OpenClaw、Hermes、Cursor 和 WorkBuddy
  当前 Agent Skills 规范的兼容契约。Cursor 现支持 user/project 两级
  Skill；Codex 新增可选 `agents/openai.yaml` 界面元数据；WorkBuddy 新增
  确定性 ZIP 构建器，分别用于桌面端本地导入和开放平台发布提交。
- 新增三层检索：兼容用融合 `canonical`、可归属 `school`、原子级精确来源
  `evidence`。`kb_retrieve.py` 支持 School/source/type/排除项硬过滤交集、
  School 别名、范围诊断以及规范术语/别名精确置顶。
- 新增多流派 analysis profile 编排。默认为 strict ICT/SMC；
  显式 School 不会静默回退；Phase 1 支持知识问答 compare，
  缺少原生行情分析器时会在拉数据和生成产物前 fail closed。
- 新增包含 15 个可检索标签的机器可读 School 注册表、JSON Schema 与确定性
  evidence builder。当前语料可生成 2,144 条 School 投影和 18,645 条
  精确来源证据；无法归属的融合内容会按原因跳过并统计。
- 新增安全 v2 索引升级：版本化 manifest、输入 fingerprint、staging
  校验/回滚与 doctor 检查。School/evidence 记录现在使用独立 document
  embedding，并通过按内容哈希、模型隔离的增量缓存复用；父卡向量继承仅保留为
  显式应急/测试选项。
- 新增经过校验的原生 v2 float32 release seed，按内容哈希首位拆为 16 个分片。
  构建时依次使用持久缓存、精确 seed 命中并仅计算剩余 miss；受保护的导出器会
  拒绝不完整缓存，并只原子发布经过完整复验的 seed。
- 新增 v2 混合检索：在硬过滤范围内分别生成 BM25 与语义候选，再通过 RRF
  融合；规范术语/别名精确命中仍置顶，并限制同一 canonical parent 的重复证据。
  纯 lexical 模式无需加载 embedding 模型。
- 新增版本化检索评测集与 CLI，统计 Recall@K、MRR、School/source purity、
  fail-closed、父卡重复率及耗时，并纳入 `auto` 发布基线，使发布前的检索变化
  可量化回归。
- 新增带校验和的 WorkBuddy 紧凑语料，仅依赖 Python 标准库即可无损重建全部
  2,144 条 School 投影与 18,645 条精确来源 evidence，并保留 School/source
  硬过滤 lexical 检索。生成包中的两项计数来自同一份已校验 build result，
  不再依赖可能过期的文字常量。

### 变更

- Skill slug 与安装目录统一为小写 `openmobius-skill`，同时保留
  **OpenMobius-skill** 作为产品和仓库名称。Codex 改为安装到
  `~/.agents/skills`；OpenClaw 遵循 `OPENCLAW_STATE_DIR`；Hermes 遵循
  `HERMES_HOME`；Cursor 本地 user skill 安装到 `~/.cursor/skills`。
- 在 Linux/macOS 上，`--platform all` 表示 5 个有官方本地发现路径的 host：
  Claude Code、Codex、OpenClaw、Hermes 和 Cursor。本版本不宣称在 Windows
  支持 OpenClaw/Hermes；Windows 用户需显式选择 Claude Code、Codex 或 Cursor。
- WorkBuddy 配置现明确区分本地 ZIP 导入、已发布版本的技能市场安装，以及开放
  平台发布。其公开文档未定义可供第三方写入并自动发现的固定目录；因此显式
  `--target-dir` 仅用于开发者暂存/校验，且不再报告“安装成功”。确定性构建器按
  更保守的 3,000,000 字节边界执行官方文档规定的 3 MB 上限；紧凑包省略
  canonical/向量产物，并对这些不支持的路由 fail closed。
- Nomic embedding 模型现固定到经过校验的不可变 revision 与权重摘要，禁用
  模型仓库远程代码，并把 Sentence Transformers / Transformers 的支持范围
  更新到当前主版本。

### 修复

- School inventory 现可在只读 Skill mount 上工作：优先读取经校验的
  manifest 计数，或以确定性方式推导旧 v2 计数，无需为写入而打开 Chroma。
- POSIX 读取端现以只读 descriptor 打开已初始化的外置锁文件，因此可在
  Codex read-only sandbox 中正常取得共享代际锁；锁基础设施错误不再被误报为
  真实锁竞争。WorkBuddy 不可变紧凑包还会用文件大小和 SHA-256 绑定全部运行时
  知识输入；只有该清单校验通过时，才允许在 sandbox 禁止首次初始化锁文件时
  使用只读 fallback。
- 修复 legacy-only 索引升级在 canonical cards 已变化时仍可能直接返回成功的
  路径。standalone 安装/更新现在只有在非空 v2 双集合与 SQLite 数据库均通过
  校验后才会清除 fail-closed generation marker；marker 缺失会被拒绝，
  不再被当成已完成代际。
- `kb_doctor.py` 改为平台无关：校验当前副本的 frontmatter、小写
  slug 与可选 expected directory，不再假定 Claude Code home 布局。
- 统一卸载命令与文档语义：普通卸载会删除整个自包含的平台目标目录；
  `--full` 现已明确标记为弃用的兼容性空操作。
- knowledge card 投影与 Chroma 提升现合并为一个持久事务，包含完整性校验
  journal、确定性崩溃恢复和跨进程 fail-fast 代际锁。读取端从 scope 解析直到
  结果序列化都持有同一代际读锁，不会把旧索引数据与刚提升或正在恢复的卡片混用。
- standalone 安装/更新改为同步原子镜像：目标保护会拒绝过宽、重叠、符号链接或
  无关目录；上游删除会同步，同时保留运行时和用户自有数据；中断切换可根据已校验
  journal 恢复。安装/更新、卸载、建索引、检索与 WorkBuddy 导出现在共用同一把
  外置知识库锁。
- 损坏、不可读、非对象或空的 v2 knowledge card 现会 fail closed，不再静默跳过。
  只读检索直接使用已有 Chroma tenant/database 而不创建状态，并将 SQLite 查询的
  临时存储保留在内存中以适配 immutable sandbox；对于明确不携带索引的包，仍保留
  显式 lexical fallback。
- WorkBuddy 紧凑导出不再跟随指向所选源码树外部的 School 注册表、别名表、
  卡片目录、卡片文件、组合输入或输出目标符号链接；未完成知识代际与超限构建
  也不会覆盖已有输出。
- 将旧版“仅 card School”投影特例限定到实际的缠论导入结构，防止来源信息不完整
  的 ICT/SMC 内容进入严格 School 范围。
- 不同 card 文件出现重复 canonical identity 时会 fail closed，不再把内容静默
  合并到首个文件的来源路径下。
- 安装/卸载示例改用唯一的 `mktemp`/GUID 目录，并只删除捕获到的精确路径，
  避免固定临时目录名与既有数据冲突。

---

## [0.3.0] — 2026-07-05

### 📚 知识库 —— 全量重建与扩容

1. **知识库扩容：665 概念 + 1,246 案例 → 726 概念 + 1,282 案例**。
   现由 12 个精选来源合集（300+ 教学视频与直播课程）经过带审计的
   跨合集融合流水线统一生成。

2. **新增流派：缠论。** 58 张概念卡 + 52 张案例卡，覆盖笔 / 中枢 /
   买卖点在美股上的应用 —— 缠论问题现在有专属卡片支撑，不再退化成
   泛泛的价格行为回答。

3. **新增来源合集**：直播实战课系列（黄金 / 原油日内交易，153 张
   案例）、SMC Strong/Weak 补充系列及多个 SMC 补充播放列表。

4. **概念卡全部来自审计版融合。** 所有概念卡均出自跨合集融合 + 模型
   抽样审计流程；每张卡保留分来源定义（`definition_per_source`），
   不同流派的细微差异不会被平均掉。

5. **案例内容级去重。** 跨合集的重复提取（同视频、同时间段）通过
   时间段重叠匹配 + 资产一致性护栏去除 —— 去掉约 280 张双语重复卡，
   **零内容丢失**（对照 v0.2.0 核验：概念 665/665、案例唯一内容
   890/890 全部承接）。

6. **术语连续性。** v0.2.0 的全部规范术语与别名保留在
   `term_aliases.json` —— 旧写法（MSS、CSD、IFVG 变体等）依然能检索
   到新卡片。

### 📝 文档

- README（中英）、SKILL description、各平台 frontmatter 统一更新为
  新知识库口径；新增贡献者栏目。

---

## [0.2.0] — 2026-05-23

### ✨ 功能更新

1. **SMC 结构指标作为默认行情分析源。** 发起行情查询时，自动获取完整
   结构信号（BOS/CHoCH 事件、Order Block、Fair Value Gap、Equal H/L
   流动性、Premium/Discount 区域、Strong/Weak Pivot 标签），不再调用
   通用技术指标。

2. **行情图全自动绘制。** 结构层（Order Block、Fair Value Gap、
   BOS/CHoCH、关键水平等）全部由系统自动叠加到图上，模型不再手画
   坐标，速度更快、不会画错。

3. **新增成交量子面板。** 行情图底部增加成交量柱状图，K 线红绿配色。

4. **TradingView 风格图表。** 浅色主题为默认；bear Order Block 浅粉、
   bull Order Block 浅蓝；BOS/CHoCH 标签彩色显示，视觉风格对齐主流
   交易平台。

5. **数据新鲜度强制声明。** 每次行情回复必须包含"数据时点 + 拉取时刻
   + K 线年龄"，数据延迟时自动加 ⚠️ 提醒。

6. **数据源问题标准化回答。** 用户问"数据从哪来 / 是不是实时"时，
   统一走固定模板（Mobius Quant API），杜绝编造来源。

7. **知识库扩容。** 从 380 概念 + 584 案例 → **665 概念 + 1,246
   案例**。CHoCH、Strong/Weak Pivot、Protected High-Low 等 SMC 核心
   概念现在都有专门卡片支撑。

### 🎨 体验优化

1. **不再凭记忆答行情。** 用户问"BTC 怎么样"（不说"现在"也算），
   系统强制实时拉数据，避免回复过期价或训练知识里的旧数字。

2. **不再列具体指标名诱导。** 描述与示例中出现的通用技术指标名全部
   移除，让模型不会"主动想到"去拉这些。

3. **K 线默认数量：200 → 300。** 给 SMC 长周期计算留更充分的数据
   窗口，结构判读更稳定。

4. **图表标签更克制。** 右轴只保留关键价位（Strong High / Weak
   Low / 入场 / 止损 / 止盈）；Order Block 与 Fair Value Gap 直接
   画矩形不挤标签，整体更清爽。

5. **`--trade-setup` 简化用户级标注。** 模型只需写 entry/SL/target
   三条线的 JSON 文件，结构叠加层自动合并。

### 🐛 BUG 处理

1. **平台描述超长被截断。** claude-code yaml 描述超过 1,024 字符
   上限，导致 Codex 等平台拒绝加载或截断 skill，已精简到合规长度。

2. **图表标记互相覆盖。** 之前画多组 marker（BOS、CHoCH、EQH 等）
   时只显示最后一组，已修复为全部累积显示。

3. **图表偶发渲染崩溃。** 某些场景下传入空时间戳会导致图表整个
   挂掉，已加防御。

4. **K 线被压缩成一小段。** 当历史结构事件超出可见 K 线范围时，
   时间轴自动拉飞导致 K 线挤在角落，已修复时间裁剪逻辑。

5. **成交量柱无法区分红绿。** 之前成交量是单一颜色，已支持按 K 线
   涨跌染色。

6. **图表元素被错误截断。** 默认上限太低导致部分 Order Block /
   Fair Value Gap 不显示，已调高到合理上限。

7. **SMC 区域数据缺失。** 调指标时漏传参数导致 Premium / Discount /
   Equilibrium 区域不返回，已自动补齐。

---

## [0.1.0] — 2026-05-13

- 首发版本：从 130 个 ICT/SMC 教学视频萃取 380 概念 + 584 案例；
  四种交互模式（概念问答、图表分析、图表标注、K 线分析）；通过
  Playwright + lightweight-charts 生成行情图。
