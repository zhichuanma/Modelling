# Prompt: Coach 年度仿真层 + 教学型 Notebook 04

## 0. 上下文

`mobility/coach/` 当前的能力上限是 `simulate_coach_journey`（**单 journey × 单 EV**），由 `scripts/run_coach_pipeline.py` v1 串行批量驱动。明确**没有**的能力（见 `CODE_REVIEW_RESPONSE.md` 第 88–103 行 "Known limitations"）：

1. journey-to-vehicle assignment——目前每条 journey 独立抽 EV，同一辆车可被分到时间重叠的 journey。
2. annual / year-long scheduling——上一条 journey 结束的 SoC 不被带入下一条。
3. 全量 fleet × feed-year 的批跑——只跑过 `--limit 3` smoke。
4. LSOA-level 聚合可视化——bus 03 notebook 的 E.5 没有 coach 版本。

本 prompt 的目标：**做出 coach 版的 03 notebook**，编号 `04_coach_annual_simulation.ipynb`，与此同时把上面 4 条能力差距补上。读者画像与 03 一致——资深能源/电网研究者，不熟 TxC、不熟此 codebase。

继承约束（仓库根 `AGENTS.md` 所有硬规则 + 上一轮 `TASKS.md` 的 9 条 global constraints）：

1. 不引入新的外部依赖。
2. **不改 bus 模块**任何代码（只读参考可以）。
3. **不改 `mobility/core/simulator.py`** 既有签名/逻辑。
4. **不改 `mobility/coach/sim_adapter.py`、`trip_chain_coach.py`、`feasibility.py`、`coach_fleet.py`** 既有逻辑（这些是上一轮刚 review 通过的；本 PR 只能在它们之上**新增**模块）。
5. **不改 `scripts/run_coach_pipeline.py`** ——它是 v1 的 single-journey 入口，annual 是另一个新入口。
6. 每个 Task 做完跑 `pytest tests/coach/ -x -q`，绿了才能进下一个 Task。再跑一次 `pytest tests/ -x -q` 确认无 regression（Task 4 完成后必跑一次 full suite，与上一轮规则一致）。
7. 一 Task 一 commit。英文 commit message，格式 `coach: <task n> <short description>`。
8. 不 `--no-verify` 跳 hooks。不 force push。不 rebase 既有 commit。不 `git push`。
9. Auto mode 下被 classifier 拦的命令不重试同一条；改用安全等价；连续 2 次拦截就跳过当前 task，把 "blocked action + intent + reason" 写到 `COACH_ANNUAL_RESPONSE.md` "Blocked by classifier" 段。
10. 跨午夜处理已经在上一轮做完（`end_h > 24` semantics）——本 PR 不动这一层。
11. 任何 "我觉得这个设计不对但 prompt 没提到" 的想法 → 写到 `COACH_ANNUAL_RESPONSE.md` "Out of scope observations" 段，不要动手。

---

## 1. 必须讲清楚的三件事（参考 03 notebook 的 1.1/1.2/1.3）

### 1.1 出行模型——coach 的概念阶梯

bus 那边是 `GTFS feed → service_id → block_id → trip → DailySchedule → feed-year expansion`。coach 这边等价物**部分缺失**，必须显式声明并 ad-hoc 构造：

```
TxC inventory (CSV)
  → TxC XML per service
      → operating profile (active days / day-of-week / bank holiday)   # Task 1 解析
      → vehicle journey (= 一段连续行驶 origin → destination)
  → journey_id (= file_name :: vehicle_journey_code)
  → coach_chain_id (= 同一假想车辆一天里串起来的 journey 集合)         # Task 2 构造
  → DailySchedule (复用 trip_chain_coach.journey_to_daily_schedules)
  → 年度展开：operating_profile_active_dates × chain template = 365 个 DailySchedule
```

与 bus 的本质不对称必须在 notebook markdown 显式承认：

- bus 的 `block_id` **是 GTFS 数据自带的**——它是 operator 真实排班；
- coach 的 `coach_chain_id` **是本 simulation 构造的**——它是我们用 first-fit / nearest-end 启发式把 journeys 拼到假想车辆上的结果，**不代表真实 coach operator 的车辆运营计划**。

这条 caveat 之所以重要：bus 的"home depot = mode(end_lsoa) per block"是用真实 block 反推 depot；coach 这边同样的归因方式得到的是 "假想 chain 的 home LSOA"，对真实充电基础设施的可信度比 bus 那边低一档。

### 1.2 充电逻辑——coach 版本

物理模型与 bus 同（LaTeX inline）：

$$
SOC_{t+1} = \mathrm{clip}\!\left[SOC_t - \frac{E_{journey,t}}{B} + \frac{P_{park,t} \cdot \Delta t}{B},\ 0,\ 1\right]
$$

Notebook B.5 必须展示：

- **参数来源表**：`battery_kwh / consumption_kwh_per_km / terminus_charge_kw / pre_journey_dwell_h / soc_init / warm_up_days`，每行写来源。`terminus_charge_kw` 默认 50 kW（来自 `mobility/coach/sim_adapter.py:DEFAULT_TERMINUS_CHARGE_KW`），`pre_journey_dwell_h` 默认 6 h，`soc_init=None` → auto-derive（来自 Task 4 的工作）。
- **`terminus_charge_kw` 抽象一句话**："车回到一个能慢充的终点站"，**当前不区分 depot 与 en-route fast charger**——这与 bus 的 depot_terminus 抽象一致，但 coach 这边连 layover 充电都还没引入。out-of-scope 见 `CODE_REVIEW_RESPONSE.md` 已记录的第 1 条。
- **infeasibility 四类**（与 bus 同；具体名称以 `mobility/coach/feasibility.py` 实际返回字段为准；如有差异，notebook 必须使用 coach 模块**实际**的字段名，不要照抄 bus）。

### 1.3 LSOA 层充电需求归因

与 03 E.5 完全相同的规则，**用 coach chain 替换 bus block**：

> 每个 coach_chain 的 home LSOA 定义为其 feed-year 内 `end_lsoa` 的众数；该 chain 的 `terminus_charge_kw` 视为**该 LSOA 内一个 synthetic terminus charger 的额定功率**。LSOA terminus 总容量 = 所有 home 在该 LSOA 的 chain 的 `terminus_charge_kw` 之和。该容量地图**完全从 simulation 反推**，公共充电桩（OCM）**未纳入**。

`docs/coach_annual_next_steps.md` 给三类放宽：公共桩 eligibility、real coach depot inventory（如能拿到 operator 自有 depot）、cross-modal contention with bus/car。

---

## 2. Task 列表（顺序执行；每 Task 一 commit）

### Task 1 — TxC operating profile → feed-year calendar

**目标**：从 TxC XML 解析 vehicle journey 的 operating profile（哪些 day-of-week 活跃、bank holiday 行为、`OperatingPeriod` 的 start/end date），转成"per-journey 活跃日期集合"。

- 新文件：`mobility/coach/calendar.py`。
- 公开 API：
  - 常量 `COACH_FEED_YEAR_START`、`COACH_FEED_YEAR_END`——优先取 TxC `OperatingPeriod` 的并集；若无法解析，fallback 用 bus 的 `FEED_YEAR_START/END`（直接 `from mobility.bus.calendar import FEED_YEAR_START, FEED_YEAR_END`，**不要复制粘贴常量值**）。
  - `parse_operating_profile(xml_path: Path) -> dict[str, list[date]]`——key=`vehicle_journey_code`，value=活跃日期 list。
  - `build_journey_date_index(journeys: pd.DataFrame, root: Path) -> pd.DataFrame`——返回 `(journey_id, date)` 长表，date 范围在 `[COACH_FEED_YEAR_START, COACH_FEED_YEAR_END]`。
- **fallback 规则**：如果 TxC 中找不到 operating profile（很多 coach service 的 profile 缺失），该 journey 用"周一到周日全部活跃"作为兜底，并在返回 frame 增加一列 `profile_source ∈ {"txc", "fallback_uniform"}`。notebook A.5 必须显式展示这列的分布。
- 新文件：`tests/coach/test_calendar.py`——一个合成 TxC fragment + 一个 fallback case，断言：日期范围正确、`profile_source` 列存在且枚举值合法、bank holiday 行为（如果实现）有显式断言。
- 不要 import bus 的 `service_date_index`——构建逻辑可以参考但不能复用 / 不能 import。

### Task 2 — Journey → vehicle chain 配对

**目标**：把同一天内"时间不重叠 + 终点-下一段起点距离合理"的 journey 串起来，赋一个 `coach_chain_id`。这是 coach 版的 "block 构造"。

- 新文件：`mobility/coach/chain_builder.py`。
- 算法（v1，明确写在 docstring）：**first-fit by start_h**——把同一 `operator_code` 内 同一天 active 的 journey 按 `start_h` 升序，依次贪心分配到现有 chain：
  - 若某 chain 的最后一条 journey 的 `end_h + transit_buffer_h` ≤ 候选 journey 的 `start_h`，且 last.`end_lat/lon` 与 candidate.`start_lat/lon` 距离 ≤ `max_relocation_km`，则加入。
  - 否则新开 chain。
- 公开 API：
  - `build_coach_chains(journeys: pd.DataFrame, date_index: pd.DataFrame, *, transit_buffer_h: float = 0.5, max_relocation_km: float = 50.0) -> pd.DataFrame`——返回 `(journey_id, date, coach_chain_id, position_in_chain)` 长表。
  - chain id 格式 `f"{operator_code}_{date.isoformat()}_{chain_seq:03d}"`。
- 新文件：`tests/coach/test_chain_builder.py`——3 case：
  - 两条 journey 时间不冲突且地理近 → 串成一个 chain。
  - 时间重叠 → 拆成两个 chain。
  - 地理距离超过 `max_relocation_km` → 拆成两个 chain。
- **明确不做**：vehicle blocking optimization、operator 真实排班还原、考虑车辆 SoC 的约束式拼接。这些走 next-steps。

### Task 3 — Chain × year → DailySchedule list

**目标**：把 chain 在年度活跃日期上展开，复用 `trip_chain_coach.journey_to_daily_schedules`（**不许改它**）。inactive day 注入 24h `terminus_dwell` parking event 以维持 SoC 连续。

- 新文件：`mobility/coach/year_schedule.py`。
- 公开 API：`chain_to_year_schedules(chain_journeys: pd.DataFrame, active_dates: Iterable[date], *, pre_journey_dwell_h: float = 6.0, terminus_dwell_purpose: str = "terminus_dwell") -> list[DailySchedule]`。
- 顺序：feed-year 每一天 → 若 chain 在该天 active：把该天的 journey 序列喂给现有 `journey_to_daily_schedules` 得到 1 个 DailySchedule；若 inactive：构造一个全天 24h `terminus_dwell` 的占位 DailySchedule（与 bus 的 `inactive day = 24h depot_terminus` 等价）。
- 单元测试：`tests/coach/test_year_schedule.py`——active + inactive 各一天，断言：年度 DailySchedule 数量 = `(FEED_YEAR_END - FEED_YEAR_START).days + 1`；inactive 天的 schedule 没有 trip、只有一个 24h `terminus_dwell` parking event；active 天的 trips 不为空。
- **不要扩展 `trip_chain_coach.py` 的对外签名**。

### Task 4 — Chain × year SOC simulator

**目标**：写 `simulate_coach_chain_year`，与 `mobility/bus/annual_simulation.py::simulate_block_year` 在职责上对等。

- 新文件：`mobility/coach/annual_simulation.py`。
- 公开 API：
  - `simulate_coach_chain_year(chain_id: str, chain_journeys: pd.DataFrame, ev_spec, active_dates, *, warm_up_days: int = 0, soc_init: float | None = None, terminus_charge_kw: float = 50.0, chemistry: str = DEFAULT_CHEMISTRY) -> dict`——返回 `{schedules, soc, load_kw, energy_charged_kwh, total_kwh, infeasibility_reasons, ...}`。
  - `simulate_coach_fleet_year(chains_df, fleet_df, journeys_df, *, seed: int, **kw) -> tuple[pd.DataFrame, pd.DataFrame]`——返回 `(per_chain_df, load_profile_df)`，schema 必须与 bus annual outputs 平行：
    - `per_chain_df`: `chain_id, ev_id, total_kwh, energy_charged_kwh, terminus_charge_kw, n_active_days, soc_floor_hit_h_min, feasible, infeasibility_reasons`
    - `load_profile_df`: `chain_id, date, hour, load_kw`（或与 bus 完全相同的长表 schema——以 bus 实际为准）。
- 实现要点：
  - 内部循环必须复用 `mobility/core/simulator.py::simulate_single_ev`（**不要重写 SoC 推进逻辑**）。
  - `warm_up_days` 跑 chain 的前 N 个 active day 直到 SOC 在日间收敛（与 bus 同 idiom）；smoke 默认 0，production 14。
  - **不要扩展 `sim_adapter.simulate_coach_journey` 的对外签名**——chain 层的循环走新的代码路径。
- 测试：`tests/coach/test_annual_simulation.py`——构造一个 2-journey chain + 365 天 calendar，断言：`per_chain_df` 行数 = 1；`load_profile_df` 行数 ≈ `365 × STEPS_PER_DAY`（与 bus 等价 assertion 一致）；`energy_charged_kwh > 0`；跨午夜 chain（含 `end_h > 24` 的 journey）SoC 轨迹连续无跳变。
- **Task 4 完成后必须跑一次 `pytest tests/ -x -q` full suite**，与 `TASKS.md` 上一轮约定一致。

### Task 5 — LSOA 归因辅助函数

**目标**：给定 `journeys_df`（含 `start_lsoa / end_lsoa`），把每个 chain 的 home LSOA、年度 `sim_kwh_year`、`terminus_total_kw` 聚合好。

- 新文件：`mobility/coach/lsoa_attribution.py`。
- 公开 API：
  - `chain_home_lsoa(journeys: pd.DataFrame, chains: pd.DataFrame) -> pd.Series`——index=chain_id, value=home_lsoa（= mode of `end_lsoa` over the chain's journeys）。
  - `lsoa_view(per_chain_df: pd.DataFrame, chain_to_lsoa: pd.Series, *, hours_per_year: int = 8760) -> pd.DataFrame`——columns: `lsoa_code, n_home_chains, sim_kwh_year, terminus_total_kw, ceiling_kwh_year, gap_ratio`，按 `sim_kwh_year` 降序。
- **不要引入 utilization 系数**；ceiling = `terminus_total_kw × 8760`，与 03 E.5 风格一致。
- **不读** `outputs/charger_registry.parquet`；terminus capacity 完全从 `per_chain_df` 自洽聚合。
- 测试：`tests/coach/test_lsoa_attribution.py`——3-chain 合成 frame，断言 mode-of-end_lsoa 规则、gap_ratio 计算正确、降序排列。
- 如果 `journeys_df` 缺 `start_lsoa / end_lsoa` 列（当前 coach `data_loader` 输出**不带 LSOA**），先查 bus side 的 `attach_lsoa`——可以**只读参考**，但不能 import bus；在 coach 这边写一个轻量版（仅 nearest-LSOA 查表），新增到 `mobility/coach/stop_geometry.py` 的下方而**不要**改既有函数；若实现成本超过半天 → stop，写到 "Blocked" 段，notebook E.5 改用 `end_stop_atco_code` 代替 LSOA 做归因（degrade gracefully）。

### Task 6 — CLI 入口：`scripts/run_coach_annual_pipeline.py`

**目标**：annual-layer 的 CLI 等价物。**不要改** 既有的 `scripts/run_coach_pipeline.py`。

- 新文件：`scripts/run_coach_annual_pipeline.py`。
- 顶部 docstring 必须写：`"Scope: feed-year coach chain simulation. Reads journeys + builds chains + builds calendar + simulates each chain across the feed year. Does NOT do operator-real vehicle blocking; chain assignment is a first-fit heuristic (see chain_builder.py)."`
- argparse：`--journeys-parquet`、`--stop-sequences-parquet`、`--fleet-path`、`--per-chain-out`、`--load-profile-out`、`--seed`、`--warm-up-days`、`--limit`（chain 数级别的 cap，用于 smoke）、`--n-workers`（默认 1；非 1 时 warn 但仍串行，与 v1 风格一致）。
- 顶层 try / except + logging，照 bus `scripts/build_all_blocks.py` / `run_bus_pipeline.py` 的风格。**不要 import 任何 bus 模块**。
- 测试：`tests/coach/test_run_coach_annual_pipeline.py`——`--limit 2 --warm-up-days 0` smoke，断言两个输出 parquet 存在且 `per_chain_df` 恰好 2 行。

### Task 7 — Notebook `04_coach_annual_simulation.ipynb` + narrative build script

**目标**：教学型 notebook，结构镜像 03，每一节解释相应的 coach-side 建模决策。

- 新文件：`notebooks/_build_04_coach_annual_narrative.py`——参考 `notebooks/_build_03_bus_annual_narrative.py` 的结构（`md(...) / code(...)` helper，从 cells list 写 `.ipynb`）。
- 由该 build script 写出的 `notebooks/04_coach_annual_simulation.ipynb` 目录：

| 段 | 类型 | 内容 |
|---|---|---|
| **0. 这个 notebook 在做什么** | md | 3 段：研究问题、coach 管线 ASCII 图（含"chain 是构造的，不是数据给的"caveat）、关键 caveat（feed-year、warm-up、synthetic terminus）|
| **A. TxC Operating Profile → Feed-Year Calendar** | md + code | 调 `parse_operating_profile` / `build_journey_date_index`，显示 `profile_source` 分布柱图 |
| **A.5 出行模型逻辑** | md + code | 概念阶梯 markdown + 打印 protagonist chain 的 3 条 journey + 一天 timeline（与 bus A.5 完全等价） |
| **B. Pick a Protagonist Chain** | md + code | 选一个 active-day 数适中（如 200–300 天）、journey 数 2–5 的 chain；1 句话说明选取标准 |
| **B.5 充电逻辑** | md + code | LaTeX SOC 公式 + 参数表（见 §1.2）+ infeasibility 4 reasons 表 + `terminus_charge_kw` 抽象说明 |
| **C. Single Chain Annual SOC** | md + code | 调 `simulate_coach_chain_year`，画 protagonist chain 的 feed-year SOC 轨迹 + 充电事件叠加 |
| **D. Small Fleet Annual Load** | md + code | 取 5–10 个 chain，调 `simulate_coach_fleet_year`，画总 load profile（24h × 7-day heatmap + 月度累计柱图） |
| **E. Annual Story Slices** | md + code | 4 张图：seasonal 月度能耗、weekday vs weekend、SOC 触底 chain 占比、cross-midnight chain 的能耗对比 |
| **E.5 LSOA → Synthetic Terminus Map** | md + code | 假设声明（§1.3）+ `lsoa_view` 聚合 + 3 图（top-50 bar / service density scatter / saturation scatter）+ 缺口 top-10 表 |
| **F. Honest Labels** | md + code | DataFrame 列出所有简化假设——必须包含："chain assignment is heuristic", "LSOA attribution = mode(end_lsoa)", "terminus capacity synthesized", "no public charger", "no utilization", "no operator real blocking"，并 link 到 `docs/coach_annual_next_steps.md` |
| Wall-clock 行 | code | `print(f"notebook executed in {time.time() - NOTEBOOK_START:.1f}s")` |

- **写作风格规则**（与 03 完全一致）：每个 markdown cell 首句 = 这段在解释什么；中文叙事为主，关键术语英文括注；不写 "Now we …" / "Let's …"；所有数字结论挂 caveat；E.5 markdown 必须显式 link 到 `docs/coach_annual_next_steps.md`。
- 所有图 `figsize=(12, 4.5)`、`dpi=110`、关 top/right spine（与 03 一致）。
- **不要把 03 的代码 copy 进来**——所有 cell 内容必须从 coach 模块导入。
- Notebook 必须能在 < 120 秒内跑完（smoke：`--limit ~10` chains + `warm_up_days=0`）。如果超时，先在 setup cell 设小 `CHAIN_LIMIT`。

### Task 8 — `docs/coach_annual_next_steps.md` + `COACH_ANNUAL_RESPONSE.md`

新增 `docs/coach_annual_next_steps.md`，至少含 5 个 numbered next-step items：

1. **Operator-real vehicle blocking**——替换 first-fit 启发式 chain 为真实 operator 排班（若能拿到 CPT / DfT / operator 自报数据）。
2. **Per-event terminus matching**——给每个 `terminus_dwell` event 附 `location_lsoa`，做类似 cars `station_matcher` 的 Huff 分配。
3. **Public charger eligibility for coach**——定义 coach 可用 OCM 桩规则（≥150 kW DC + 适合长停留），把公共桩纳入供给侧。
4. **Real coach depot inventory**——若可获取 operator 自有 depot list，替换 synthetic terminus map。
5. **Cross-modal contention**——bus/coach/car 共享一张 national charger registry。

在仓库根新增 `COACH_ANNUAL_RESPONSE.md`（结构照 `CODE_REVIEW_RESPONSE.md`）：

- 每 Task 的 commit hash + 一行摘要表。
- 测试命令与结果（每 Task 后 `pytest tests/coach/` 的输出 + Task 4 后 full suite 输出）。
- "Out of scope observations" 段——发现但没动手的问题。
- "Known limitations" 段——pipeline annual v1 显式不做的事（沿用上一轮 v1 limitations 的格式：no real operator blocking, end-of-chain SoC not carried into next chain's next day 等）。
- "Blocked by classifier" 段——若有。

---

## 3. 实现细节速查表

### 3.1 模块导入图（最终状态）

```
mobility/coach/
  calendar.py            # Task 1, new
  chain_builder.py       # Task 2, new
  year_schedule.py       # Task 3, new
  annual_simulation.py   # Task 4, new
  lsoa_attribution.py    # Task 5, new
  data_loader.py         # unchanged
  sim_adapter.py         # unchanged
  trip_chain_coach.py    # unchanged
  feasibility.py         # unchanged
  coach_fleet.py         # unchanged
  selection.py           # unchanged
  distance.py / stop_geometry.py / txc_parser.py  # unchanged (Task 5 may append a function to stop_geometry; do NOT modify existing functions)

scripts/
  run_coach_pipeline.py           # unchanged (v1, single-journey)
  run_coach_annual_pipeline.py    # Task 6, new

notebooks/
  04_coach_annual_simulation.ipynb       # Task 7, new
  _build_04_coach_annual_narrative.py    # Task 7, new

docs/
  coach_annual_next_steps.md             # Task 8, new

COACH_ANNUAL_RESPONSE.md                 # Task 8, new at repo root
```

### 3.2 公开 API 不变性 grep 列表（CI 自检 idiom）

PR 描述里跑这几个 grep 证明没动既有公开 API：

```
grep -n "def simulate_coach_journey" mobility/coach/sim_adapter.py
grep -n "def journey_to_daily_schedules" mobility/coach/trip_chain_coach.py
grep -n "def journey_feasibility" mobility/coach/feasibility.py
grep -n "def sample_coach_ev\|def load_coach_fleet" mobility/coach/coach_fleet.py
```

每条都应该精确返回上一轮 commit 的同一行函数签名。

### 3.3 测试矩阵

| 文件 | 来自 Task | 必含断言 |
|---|---|---|
| `test_calendar.py` | 1 | profile_source 枚举合法、日期范围、fallback 行为 |
| `test_chain_builder.py` | 2 | 时间冲突拆分、地理距离拆分、正常串接 |
| `test_year_schedule.py` | 3 | DailySchedule 数 = feed-year 天数、inactive 天纯 dwell |
| `test_annual_simulation.py` | 4 | per_chain 行数 / load_profile 行数 / 跨午夜 SoC 连续 |
| `test_lsoa_attribution.py` | 5 | mode-of-end_lsoa、gap_ratio、降序 |
| `test_run_coach_annual_pipeline.py` | 6 | `--limit 2` 双 parquet 存在且 2 行 |

整套必须保持 `pytest tests/coach/ -x -q` 在每个 Task commit 之前是绿的。

---

## 4. 验收

- `python notebooks/_build_04_coach_annual_narrative.py` 重生 `04_coach_annual_simulation.ipynb`，cell 数 ≥ 22（A/A.5/B/B.5/C/D/E/E.5/F 各 1 md + 1 code = 18，加 setup cell、wall-clock、notebook-0 markdown 等）。
- `jupyter nbconvert --to notebook --execute --inplace notebooks/04_coach_annual_simulation.ipynb` < 120 秒。
- `pytest tests/coach/ -x -q` 在 HEAD 全绿，行数从 14 → ≥ 20。
- `pytest tests/ -x -q` 在 HEAD 不引入新 failure（上一轮已存在的 `tests/mobility/stage_6_8/test_home_charging.py` 失败可继续 ignore，但要在 RESPONSE 文件里复述并确认与本 PR 无关）。
- 上述 3.2 的 grep 全部命中（公开 API 未变）。
- `docs/coach_annual_next_steps.md` 存在、≥ 5 个 numbered items。
- 5-minute readability test（不需要执行，仅作 narrative 目标）：一个不熟此 codebase 的研究者应能回答：(a) 输入是什么？(b) chain 怎么来的、为什么是构造而非数据？(c) SoC 怎么变？(d) E.5 的 LSOA terminus 容量怎么聚合的、为什么不和公共桩对比？

---

## 5. 不要做的事（汇总）

- 不改 bus 任何模块、不改 core simulator、不改上一轮刚 review 过的 coach 既有模块（见 §0 约束 4）。
- 不改 `scripts/run_coach_pipeline.py`——annual 走新脚本。
- 不引入 geopandas / shapely / pyproj（如 Task 5 需要 LSOA 最近点查找，写最简版 numpy + KDTree 或直接 haversine，参考既有 `mobility/coach/distance.py` 风格）。
- 不要把 first-fit chain 启发式上升到"vehicle blocking optimization"——明确写在 docstring 和 honest labels 里它就是 first-fit。
- E.5 **不读** `outputs/charger_registry.parquet`，terminus 容量从 `per_chain_df` 自聚合。
- 不引入 utilization 系数；ceiling = `kW × 8760`。
- 不在 notebook 里 `pip install` / `df.to_csv`；输出 parquet 通过 CLI 完成，notebook 只**读**已存在的 parquet（如果不存在则在 setup cell 里调 `run_pipeline()` 现场生成 smoke）。
- 不 `--no-verify`、不 force push、不 push 到 remote、不 rebase 既有 commit。
- 不删既有 04 之外的 notebook。

---

## 6. PR description 模板

```markdown
## Summary
New coach annual simulation layer + pedagogical notebook 04, mirroring bus 03.

Closes the four v1 gaps documented in CODE_REVIEW_RESPONSE.md "Known limitations":
- Task 1: TxC operating profile parser → per-journey feed-year calendar
  (new `mobility/coach/calendar.py`).
- Task 2: First-fit `coach_chain_id` construction
  (new `mobility/coach/chain_builder.py`).
- Task 3: Chain × feed-year DailySchedule expansion
  (new `mobility/coach/year_schedule.py`).
- Task 4: `simulate_coach_chain_year` + `simulate_coach_fleet_year`
  (new `mobility/coach/annual_simulation.py`).
- Task 5: LSOA attribution helpers
  (new `mobility/coach/lsoa_attribution.py`).
- Task 6: `scripts/run_coach_annual_pipeline.py` CLI.
- Task 7: New `notebooks/04_coach_annual_simulation.ipynb` (A/A.5/B/B.5/C/D/E/E.5/F) +
  narrative build script. Mirrors notebook 03 structure with coach-specific
  caveats (chain is constructed, not data-given).
- Task 8: `docs/coach_annual_next_steps.md` + `COACH_ANNUAL_RESPONSE.md`.

## Verification
- pytest tests/coach/ -x -q → all green (rows from 14 → N)
- pytest tests/ -x -q → no new failures (pre-existing home_charging failure unchanged)
- python notebooks/_build_04_coach_annual_narrative.py → wrote .ipynb
- jupyter nbconvert --to notebook --execute --inplace ... → <120s
- Public-API grep checks (see prompt §3.2) all hit unchanged signatures.

## Public-API changes
None to existing modules. New modules added under `mobility/coach/` and `scripts/`.

## Deviations from AGENTS.md
None.
```

---

## 7. Failure handling

若任一 Task 的前置条件被证伪（TxC operating profile schema 不符预期、LSOA 数据缺失、bus calendar 常量改名等）：stop，把 blocker 写到 `COACH_ANNUAL_RESPONSE.md` "Blocked" 段，skip 该 Task，继续下一个 Task。不要猜。
