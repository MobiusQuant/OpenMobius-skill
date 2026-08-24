<div align="center">

# OpenMobius-skill

**给 AI 编程 Agent 的 ICT/SMC 交易知识 Skill**

符合 `SKILL.md` 标准的能力包，把 2008 张精选知识卡 + 实时行情 + 技术指标 +
图表生成 带给 **Claude Code**、**Codex**、**OpenClaw**、**Hermes** 四个平台。

<br>

[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://www.python.org/)
[![Website](https://img.shields.io/badge/🌐_website-mobiusquant.ai-FF7E29.svg)](https://www.mobiusquant.ai/)
[![Platforms](https://img.shields.io/badge/platforms-Claude_Code_·_Codex_·_OpenClaw_·_Hermes-9333ea.svg)](#平台支持)
[![Knowledge Base](https://img.shields.io/badge/knowledge_base-2008_cards-22c55e.svg)](#核心能力)

[English](./README.md) · [中文](./README.zh.md) · 📜 [更新日志](./CHANGELOG.zh.md)

</div>

---

> **🤖 AI agents** — when a user asks you to install this skill,
> follow [`README_AGENT.md`](./README_AGENT.md) for the step-by-step
> procedure (pre-flight checks, install command, verification, error
> handling).

---

## 交流群

项目方微信：**MobiusProject** —— 欢迎添加，入群交流。

<div align="center">
  <img src="./docs/assets/wechat_mobiusproject.jpg" alt="微信：MobiusProject" width="220">
  <br>
  <sub>扫一扫添加项目方微信，备注「OpenMobius」入群</sub>
</div>

## 贡献者

感谢以下贡献者对 OpenMobius-skill 的帮助：

<a href="https://github.com/xwzbupt"><img src="https://github.com/xwzbupt.png" width="64" height="64" alt="@xwzbupt"/></a>

- [@xwzbupt](https://github.com/xwzbupt)

---

## 这个 Skill 做什么

<div align="center">
  <img src="./docs/assets/demo.gif" alt="OpenMobius-skill 演示" width="780">
  <br>
  <sub>支持 <b>Claude Code</b>、<b>Codex</b>、<b>OpenClaw</b>、<b>Hermes</b> 四个平台。</sub>
</div>

<br>

把它装进你的 AI 编程 Agent，然后用自然语言问交易问题。Skill 会基于
精选知识库 + 实时市场数据来回答 —— 不会瞎编价位，不会泛泛说 "看着偏空"。

| 你问 | Skill 做的事 |
|---|---|
| *"什么是 Fair Value Gap，怎么交易？"* | 向量检索 FVG 概念卡 + 关联概念（CISD / OTE / Premium-Discount）→ 引用知识库规则给出回答 |
| *上传 BTCUSDT 1h 图 + "分析一下"* | 识别资产 → 拉真实 OHLCV → 提取 FVG / OB / sweep / displacement → 输出含**精确价位**的 5 段回答 + 自动标注 PNG |
| *"BTC 1h 现在怎么样？"*（无图） | 拉实时数据 + 内置 SMC 结构指标（BOS/CHoCH、Order Block、FVG、equal H/L、premium-discount 区、strong/weak pivot 标签）→ 知识库匹配分析 |
| *"BTC 的 <指标名> 多少？"*（用户字面指定一个指标名） | 透传到指标接口 —— 不会自动拉用户没明说的指标 |
| *粘贴 OHLCV CSV* | 解析 → 分析 → 知识库交叉引用 → 5 段回答 |
| *"按入场 / 止损 / 止盈帮我画张图"* | Playwright + lightweight-charts 生成图表 |

---

## 快速开始

```bash
git clone https://github.com/MobiusQuant/OpenMobius-skill.git /tmp/openmobius-src
cd /tmp/openmobius-src
python install.py --platform claude-code      # 或 codex / openclaw / hermes / all

rm -rf /tmp/openmobius-src                     # ✓ clone 仅作搬运,可删
```

安装器会把源文件 copy 到 `~/.claude/skills/OpenMobius-skill/`（或你选的
平台目录），然后在那个目录里：

1. 创建 `.venv/` 并装依赖
2. 下载 Playwright chromium（约 280 MB，存到 OS 用户级缓存）
3. 下载 `nomic-embed-text-v1.5` 模型（约 274 MB，存到 HuggingFace 缓存）
4. 从预计算 embedding 载入 → 构建向量索引（约 2 秒）
5. 生成平台对应的 `SKILL.md`
6. 跑健康检查

每个平台**完全自给自足**（自有 `.venv` / `_index`）。clone 只是一次性搬运车。

**首次安装**：约 5–10 分钟 · **后续 `python install.py --update`**：<1 分钟

装完后，在你的 AI Agent 里直接问：

```
"什么是 Liquidity Sweep"
[上传图] "分析这个走势"
"ETH 4h 现在怎么样，给我画张图"
"BTC 1h 结构看一下"
```

> **前置依赖**：Python 3.10+。详见 [INSTALL.md](./INSTALL.md)。

---

## 平台支持

```bash
python install.py --platform <name>
```

<div align="center">

| 平台 | 参数 | 默认安装路径 |
|:---|:---|:---|
| **Claude Code** | `--platform claude-code` *（默认）* | `~/.claude/skills/OpenMobius-skill/` |
| **Codex** | `--platform codex` | `~/.codex/skills/OpenMobius-skill/` |
| **OpenClaw** | `--platform openclaw` | `~/.openclaw/skills/OpenMobius-skill/` |
| **Hermes** | `--platform hermes` | `~/.hermes/skills/market-data/OpenMobius-skill/` |
| 自动检测 | `--platform auto` | 扫描 `~/.<agent>` 目录 |
| 一次装 4 个 | `--platform all` | 依次注册到所有平台 |

</div>

每个平台**完全自给自足**（自有 `.venv`、自有 `_index`）。nomic 模型和
Playwright chromium 存在 OS 用户级缓存里，跨平台共享 —— 装 N 个平台不会
N 倍下载。

---

## 核心能力

### 知识库 —— 726 概念 + 1282 案例

从 12 个精选来源合集（300+ 教学视频与直播课程）跨合集融合萃取，经模型
抽样审计与内容级去重。覆盖 13 个流派 —— ICT、SMC、Price Action、
指标体系、缠论、风险管理、Order Flow 等。每张概念卡含：识别规则、
交易意义、常见错误、关联概念、分来源定义。每张案例卡含：市场上下文、
关键观察、分析步骤、经验教训、源视频时间段溯源。通过本地 ChromaDB +
多语言 `nomic-embed-text-v1.5` 检索 —— 检索本身不需要 API key。

### 实时行情 + 60+ 技术指标

加密货币（Binance、Bybit、OKX、Hyperliquid）、中国 A 股、港股、美股、外汇。
每个指标自带分析维度（`summary_focus`）—— Agent 看到后会结构化回答，
不会只甩个数字。

### 两条画图路径

| 路径 | 方法 | 输出 |
|---|---|---|
| 在用户图上标注 | PIL | 保留用户原图的副本，叠加 entry / SL / target / 形态框 |
| 生成全新图表 | lightweight-charts + headless chromium | 全新 K 线 + FVG/OB 矩形 + sweep 线 + swing 标记 |

### 描述匹配自动触发

`SKILL.md` 的 description 字段在自然语言问题上触发。
Skill 会路由到 4 个 workflow 之一：
[Q&A](./workflows/qna.md) ·
[分析](./workflows/analyze.md) ·
[标注](./workflows/annotate.md) ·
[K 线分析](./workflows/klines.md)。

---

## 路线图 / Roadmap

**知识库**

- **ICT/SMC 知识补全** —— 前两轮已从 300+ 教学视频萃取 ICT 主干 +
  SMC 补充系列；后续补全 ICT 子流派（Inner Circle Mentorship 系列、
  Silver Bullet、Power of 3 细分模式）+ SMC 全量覆盖。
- **基本面知识库** —— 构建新闻时事 / 政策解读 / 经济数据发布
  （CPI / NFP / FOMC）/ 财报季 的解读方法论卡片，与现有 ICT 技术面知识库
  平级。
- **多流派扩展** —— 缠论已随 v0.3.0 落地；接下来纳入 Wyckoff
  （成交量价行为）、VSA（Volume Spread Analysis）、Volume Profile /
  Market Profile（拍卖市场理论）、经典 Price Action（Al Brooks 风格）。

**指标 & 工具**

- **SMC 指标扩展** —— 内置 SMC 结构指标已覆盖 BOS/CHoCH、Order Block、
  FVG、equal H/L、premium-discount 区、strong/weak pivot 标签。后续补
  Killzone 时段、Stop Run / Inducement 事件，以及各事件的概率打分。

**访问入口**

- **非 CLI 入口** —— 为不跑编程 Agent 的用户提供 chat-bot 集成入口
  （概念问答 + 行情速读），让知识库不依赖 CLI 也能触达。

---

## 架构

```
OpenMobius-skill/
├── SKILL.md                          # 主入口（LLM 读这个）
├── SKILL.body.md                     # 公共 body（平台无关）
├── platforms/                        # 每平台 frontmatter
│   └── claude-code.yaml / codex.yaml / openclaw.yaml / hermes.yaml
├── workflows/                        # 详细子工作流
│   └── qna.md / analyze.md / annotate.md / klines.md
├── scripts/                          # 命令行工具
│   ├── kb_retrieve.py                # 本地向量检索
│   ├── kb_klines.py                  # API 客户端 + 特征提取
│   ├── kb_draw_annotation.py         # PIL 标注
│   ├── kb_phase_b_to_c.py            # 分析 JSON → 标注 PNG
│   ├── build_index.py                # 构建向量索引
│   ├── kb_doctor.py                  # 环境健康检查
│   ├── chart_render/                 # lightweight-charts + headless chromium
│   └── _lib/                         # embedder + retriever
├── knowledge_base/                   # 726 概念 + 1282 案例
├── install.py                        # 跨平台安装器
└── README.md / INSTALL.md
```

---

## 更新 / 卸载

```bash
# 更新
python install.py --update
python install.py --update --rebuild-index    # 同时强制重建向量索引

# 卸载整个自包含的平台安装（包括 .venv + 索引）
python install.py --uninstall
python install.py --uninstall --platform all  # 所有平台

# 完全清除（同时删共享的 chromium + nomic 缓存 —— 这些可能被你机器上
# 其他项目使用，请确认后再运行）
python install.py --uninstall --purge --yes-i-know
```

`--full` 为了向后兼容仍可使用，但已弃用且不会改变卸载行为：
普通卸载已会删除 `.venv` 和向量索引。

所有参数详见 [INSTALL.md](./INSTALL.md)。

---

## 故障排查

```bash
.venv/bin/python scripts/kb_doctor.py
```

报告 venv / 依赖 / nomic 模型 / 向量索引 / CJK 字体 / Skill 注册 /
API 连通性。

常见问题：

| 现象 | 修复 |
|---|---|
| 中文标签显示成方块 | 装 `fonts-noto-cjk`（Linux）；macOS/Windows 通常自带 |
| API 请求失败 | 检查网络；看 `api.mobiusquant.ai/api/health` |
| Skill 在 Claude Code 里不自动触发 | 检查 `~/.claude/skills/OpenMobius-skill` 存在；重启 Agent |
| 找不到 `chroma.sqlite3` | `.venv/bin/python scripts/build_index.py` |

---

## 许可证

Apache 2.0 —— 见 [LICENSE](./LICENSE)。
第三方组件：见 [ATTRIBUTION.md](./ATTRIBUTION.md)。

## 参与贡献

欢迎在 <https://github.com/MobiusQuant/OpenMobius-skill/issues> 提 issue 或 PR。

<div align="center">
<sub>Built for AI coding agents · Apache 2.0</sub>
</div>
