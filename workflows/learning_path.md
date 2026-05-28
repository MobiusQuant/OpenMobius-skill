# Workflow: Guided Learning Path (SMC/ICT 学习路径)

A structured, progress-tracked curriculum that turns the knowledge base
(665 concept cards + 1246 case cards) into a staged learning journey.
This workflow is a **thin orchestration layer** — it reuses the existing
concept cards (`knowledge_base/concepts/*.json`), case cards
(`knowledge_base/cases/*.json`), and `kb_retrieve.py`. It adds **no new
data** and **no API calls** (this is not a market-analysis workflow, so
the freshness mandate does NOT apply).

## When this workflow applies

- User wants to *learn the system*, not ask a one-off question:
  "带我学 SMC" / "学习路径" / "我想系统学 ICT" / "从头教我" /
  "learn SMC step by step" / "teach me the basics" / "我该从哪学起"
- User asks to **continue / resume** learning: "接着上次学" / "继续学" /
  "我学到哪了" / "continue learning"
- User asks for a **quiz / self-test**: "测一下我" / "出几道题" / "quiz me"

## When NOT to use

- One-off concept question ("什么是 FVG") → `qna.md`
- Chart / asset+timeframe / OHLCV → `analyze.md` / `klines.md`
- User wants to annotate an image → `annotate.md`

---

## The curriculum (7 stages, mapped to real concept cards)

Each stage lists concept **slugs** that exist verbatim as
`knowledge_base/concepts/<slug>.json`. Teach a concept by **reading its
card directly** (deterministic, no vector noise) and synthesizing from
`definition` / `identification_rules` / `trading_implication` /
`common_mistakes`. Cite rule numbers, per `qna.md` conventions.

| Stage | 名称 / Theme | Concept slugs (read these cards) |
|---|---|---|
| **0** | 地基 · 市场结构 | `market_structure`, `break_of_structure`, `change_of_character`, `market_structure_shift`, `protected_high_low` |
| **1** | 流动性 · 价格为何而动 | `liquidity`, `buyside_sellside_liquidity`, `liquidity_sweep`, `equal_highs`, `equal_lows`, `inducement` |
| **2** | PD Array · 在哪进场 | `order_block`, `fair_value_gap`, `breaker_block`, `mitigation_block`, `premium_and_discount`, `equilibrium`, `fibonacci_retracement`, `optimal_trade_entry_ote` |
| **3** | 确认与执行 | `displacement`, `top_down_analysis`, `daily_bias`, `basic_order_block_entry`, `a_ict_entry_checklist`, `smart_money_technique` |
| **4** | 时间维度 | `kill_zone`, `london_session`, `new_york_session`, `asia_session`, `power_of_3_amd`, `session_based_price_action` |
| **5** | 风控与心态 | `risk_management`, `risk_to_reward_ratio`, `position_sizing`, `stop_loss`, `break_even_win_rate`, `backtesting`, `market_psychology` |
| **6** | 实战整合 | *(no new concepts)* — case study + live structural read of a real chart |

**Ordering rule**: stages 0→1→2 are foundational and must be done in
order; 3→4 can interleave; stage 5 should run in parallel from day one;
stage 6 is woven throughout. If a concept slug is ever missing on disk,
fall back to `kb_retrieve.py "<concept name>" --top-k 1` and tell the
user the card name may have shifted.

---

## Progress tracking

Progress lives in a single JSON file at the **skill root**:
`${SKILL_DIR}/.learning_progress.json` (git-ignored; it is per-learner
state, never committed). `${SKILL_DIR}` is the directory containing
`SKILL.md`.

### Schema

```json
{
  "version": 1,
  "started_at": "2026-05-27T08:00:00Z",
  "last_updated": "2026-05-27T08:30:00Z",
  "current_stage": 0,
  "stages": {
    "0": {"status": "in_progress", "concepts_seen": ["market_structure"], "quiz_best": null},
    "1": {"status": "locked",      "concepts_seen": [],                   "quiz_best": null}
  },
  "notes": []
}
```

- `status` ∈ `locked | in_progress | completed`. A stage is `completed`
  when all its concept cards have been taught AND the user passed its
  quiz (≥ 70%, i.e. `quiz_best` ≥ 0.7).
- `quiz_best` is the best fractional score (0.0–1.0) the learner has hit
  on that stage's quiz, or `null` if never taken.
- `concepts_seen` accumulates slugs already taught, so resume skips them.

### Reading / writing progress

1. On entry, **Read** `${SKILL_DIR}/.learning_progress.json`.
   - If it does not exist → this is a first session. Create it with
     stage 0 `in_progress`, all others `locked`, then greet the learner
     with the curriculum overview (the 7-stage table above, in their
     language) and ask where to start.
2. After teaching a concept, **append** its slug to the stage's
   `concepts_seen` and bump `last_updated` (Write the whole file back).
3. After a quiz, update `quiz_best`; if passed and all concepts seen,
   set the stage `completed`, unlock the next stage (`locked` →
   `in_progress`), and bump `current_stage`.
4. **Never fabricate progress** — only record what actually happened in
   the conversation.

---

## Steps

### Step 1: Load progress + figure out intent

Read the progress file. Decide which of these the user wants:

- **Overview / first time** → show the 7-stage table, explain the
  ordering rule, recommend starting at the learner's `current_stage`,
  and ask: 学新内容 / 复习 / 直接测验 / 跳到某阶段?
- **Continue** → summarize "你上次学到 阶段 N（<theme>），已掌握
  <concepts_seen>", then resume at the next un-taught concept.
- **Jump** ("直接学 PD Array" / "跳到阶段 4") → honor it, but if it
  skips foundational stages, note the dependency ("阶段 2 需要先理解
  阶段 0 的结构语言，要不要先快速过一遍？").
- **Quiz only** → go to Step 4 for the requested stage.

### Step 2: Teach a stage (concept by concept)

For each un-taught concept slug in the current stage:

1. **Read** `knowledge_base/concepts/<slug>.json`.
2. Synthesize a focused mini-lesson in the **user's language** (English
   technical terms stay English — FVG / Order Block / BOS / CHoCH …):
   - **一句话定义** (from `definition`)
   - **怎么识别** — 2-4 numbered rules (from `identification_rules`),
     cite as "规则 N"
   - **怎么用** (from `trading_implication`)
   - **最容易踩的坑** — 1-2 items (from `common_mistakes`)
   - **和谁相关** (from `related_concepts`) — link forward to later stages
3. Keep each concept lesson tight (the learner is studying, not reading a
   wall of text). Offer "懂了，下一个" / "再展开讲讲" / "看个真实例子".
4. After the concept, **append the slug** to `concepts_seen` and save.

Teach concepts in the card order listed for the stage. Do NOT dump all
concepts at once — one concept per turn unless the user says "一次全讲".

### Step 3: Case practice (cement each concept)

When the user wants a real example for a concept (or proactively, after
teaching it):

1. Read the concept card's **`illustrated_by_cases`** list — each entry
   has a `card_id` that maps **directly** to
   `knowledge_base/cases/<card_id>.json`. Read 1-2 of those case cards.
2. Walk the learner through the case's `context` → `observation` →
   `analysis_steps` → `lessons`, tying each step back to the rule just
   taught.
3. If `illustrated_by_cases` is empty, fall back to
   `kb_retrieve.py "<concept> <feature>" --type case --top-k 2`.

### Step 4: Quiz (随堂测验)

After all of a stage's concepts are taught (or on demand), run a quiz:

1. Generate **3-5 questions grounded in the stage's concept cards** —
   pull them from the cards' `identification_rules` and
   `common_mistakes`. Good question types:
   - 判别题: "这根 K 线收盘突破前低但没收回前高 — 是 BOS 还是 CHoCH？"
   - 填空/选择: "premium 区利于做 ___，discount 区利于做 ___。"
   - 纠错题: present a common mistake from a card, ask "错在哪？"
   - 案例题: paste a case `observation`, ask "下一步该看什么信号？"
2. Ask questions **one at a time**, wait for the answer, then grade
   against the card (cite the rule that settles it). Be encouraging but
   precise — wrong answers get the correct reasoning, not just "错了".
3. Tally the fractional score. Update `quiz_best`. If ≥ 0.7 and all
   concepts seen → mark stage `completed`, unlock next, bump
   `current_stage`, and congratulate + preview the next stage.
4. If < 0.7 → point to the specific concepts to review, keep the stage
   `in_progress`.

**No fabrication**: every quiz question and every grade must trace to a
specific concept card's rule. If unsure of the answer, say so rather
than bluffing.

### Step 5: Stage 6 — real-world integration

Stage 6 has no new concepts. Drive it by:
- Pulling 2-3 cases spanning multiple concepts via
  `kb_retrieve.py "<multi-concept query>" --type case --top-k 3`, and
- Offering a **live structural read**: hand off to `klines.md`
  ("BTC 4h" / "茅台日线") so the learner applies the framework to fresh
  data + an annotated chart. Remember the venue limits: 美股/港股/外汇
  只有日线，A股和加密才有日内。

---

## Output format

This is a teaching dialogue, not the market-analysis 5-section template.
Use clear `##` headers adapted to the moment, e.g.:

```markdown
## 📍 你的进度 / Your progress
阶段 N（<theme>）· 已掌握 X/Y 概念 · 下一个：<concept>

## 📖 本节概念 / This concept: <Term>
定义 / 识别规则（规则 1…）/ 怎么用 / 常见坑 / 相关概念

## 🎯 随堂测验 / Quiz   (when quizzing)
…

## ⏭️ 下一步 / Next
懂了下一个 · 看真实案例 · 来个小测验 · 换个阶段
```

Always end a teaching turn with concrete next-step options so the learner
knows how to drive.

## Constraints

1. **No new data, no API calls** — pure orchestration over existing cards.
2. **No fabrication** — teach only what the cards say; cite rule numbers
   and card terms. If a slug is missing, fall back to `kb_retrieve`.
3. **Language** (shared rule) — Chinese prose / English technical terms.
4. **Progress integrity** — only record concepts actually taught and
   quizzes actually taken; write the JSON back after each change.
5. **Pace** — one concept per turn by default; never wall-of-text the
   whole stage unless asked.

## Tool reference

```bash
# Teach a concept — read its card directly (deterministic)
cat knowledge_base/concepts/<slug>.json     # via Read tool

# Case practice — concept card's illustrated_by_cases gives card_ids:
cat knowledge_base/cases/<card_id>.json      # via Read tool
# fallback:
.venv/bin/python scripts/kb_retrieve.py "<concept> <feature>" --type case --top-k 2

# Progress file (Read / Write tools)
${SKILL_DIR}/.learning_progress.json
```
