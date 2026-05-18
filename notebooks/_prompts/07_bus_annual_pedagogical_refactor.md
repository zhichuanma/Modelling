# Prompt: Notebook 03 教学型改造 — 把"年度 bus 仿真"讲给资深研究者听

## 0. 上下文

`notebooks/03_bus_annual_simulation.ipynb` 当前是执行型 smoke：A→F 每段调函数+一张图，假设读者已经懂 GTFS、block、SOC、depot_terminus、warmup。需要把它升级为 **可向资深能源/电网研究者（论文 reviewer / 导师场景）解释建模决策** 的版本，同时保留现有所有分析图，并新增 LSOA 层充电需求可视化。

继承约束：仓库根 `AGENTS.md` 所有硬规则。本 PR **不允许改** `mobility/core/simulator.py`、`mobility/bus/sim_adapter.py`、`mobility/bus/trip_chain_bus.py`、`mobility/bus/annual_simulation.py` 的既有逻辑。所有改动收敛在：

- `notebooks/03_bus_annual_simulation.ipynb`
- `notebooks/_build_03_bus_annual_narrative.py`
- 新增 `docs/bus_charging_next_steps.md`（next-steps 工作清单）

**读者画像**：资深研究者（懂能源系统/电网/统计），不熟 GTFS、不熟这个 codebase、不熟 transit SOC simulator 实现细节。每个建模决策值 1–2 句 motivation；不要 tutorial 式过度解释；中文叙事为主，关键术语英文括注。

---

## 1. 三件必须讲清楚的事

### 1.1 出行模型的建模逻辑

从原始 GTFS feed 到仿真输入的概念阶梯：

```
GTFS feed (calendar.txt + trips.txt + stop_times.txt)
  → service_id：定义"哪些日历日属于同一服务模式"（weekday / Sat / Sun / 特殊日）
  → block_id：把同一辆车一天里串起来的 trip 集合（native = GTFS 自带；inferred = 算法补全）
  → trip：一段连续行驶（出发站 → 到达站，含 distance_km、start_h、end_h、起讫 lat/lon、起讫 LSOA）
  → DailySchedule：trip 序列 + parking events（layover / depot_terminus）
  → 年度展开：service_id 在 feed-year 内的活跃日期集合 × block 模板 = 365 个 DailySchedule
```

Notebook 用一段 markdown + 一段代码演示：取 protagonist block 的 trips，打印 3 行原始 trip（看到 `block_id, service_id, trip_id, distance_km, start_h, end_h, start_lsoa, end_lsoa`），再用一句话说明 "block 在 inactive 日替换为 24h `depot_terminus` dwell 以保证 SOC 连续"。然后用 `protagonist_result['schedules'][0]` 取一个活跃日，画 horizontal timeline：trips 蓝色 bar，parking events 浅灰 bar，x 轴 = hour of day（标题："Protagonist block: one active service day timeline"）。

### 1.2 充电逻辑

物理模型一句话版本（LaTeX 内联）：

$$
SOC_{t+1} = \mathrm{clip}\!\left[SOC_t - \frac{E_{trip,t}}{B} + \frac{P_{park,t} \cdot \Delta t}{B},\ 0,\ 1\right]
$$

Notebook 解释：

- **参数来源表**：`battery_kwh`、`consumption_kwh_per_km`、`depot_charge_kw`（来自 `BEV_Bus_Coach_unique_with_params_with_AC.csv` 抽样）、`layover_charge_kw=0`（notebook 配置 `allow_layover_charging=False`）、`warm_up_days=0`（smoke 快跑；production 用 `WARMUP_DAYS=14`）、`soc_init=1.0`（day 0 满电）。每行一句来源。
- **`depot_terminus` 抽象**："车回到一个可以慢充的位置"，**当前不绑定具体经纬度**。这是简化假设——E.5 用 LSOA 把它落地。
- **warmup**：从 `soc_init=1.0` 起步与稳态偏差大；`warm_up_days=14` 让前两周循环跑直到 SOC 收敛，再开始记录正式年内 365 天。
- **infeasibility 四类**（来自 `mobility/bus/feasibility.py`，列表展示）：
  - `single_trip_exceeds_battery` — 单 trip 能耗 > 电池上限
  - `starts_below_min_required` — 第一段 trip 出发前 SOC 不够
  - `depot_only_insufficient` — 仅靠 depot 充电不足以覆盖当日总能耗
  - `midday_depletion` — 时间错配 / 中途无充电窗口

### 1.3 充电需求通过 LSOA 层接入 charging stations（self-consistent depot map）

**核心建模假设**（必须 markdown 首句声明）：

> 本研究当前阶段把每条 bus block 的 "home LSOA" 定义为其 feed-year 内 `end_lsoa` 的众数；该 block 的 `depot_charge_kw`（来自 vehicle spec）即视为**该 LSOA 内一个 synthetic depot 的额定功率**。这是 depot 在本仿真中的 operational definition——"block 末班停靠的地方就是充电的地方"。每个 LSOA 的 depot 总容量 = 所有 home 在该 LSOA 的 block 的 `depot_charge_kw` 之和。该容量地图**完全从 simulation 反推**，不依赖外部 depot inventory；公共充电桩（OCM）当前**未纳入** bus 充电基础设施，因为它们并非为 bus 这类大功率长停留场景设计。两类放宽（公共桩 eligibility、utilization & queueing、real depot inventory）见 [`docs/bus_charging_next_steps.md`](../../docs/bus_charging_next_steps.md)。

**归因规则要点**：

- `home_lsoa = mode(end_lsoa)` per block（首末等价：SOC 连续保证次日首班起点 ≈ 当日末班终点）
- 每个 block 的 `energy_charged_kwh` 与 `depot_charge_kw` 同时归到 home_lsoa
- 该归因**不替换** simulation 输入；纯 post-hoc 可视化

**E.5 输出三图 + 一表**：

1. **LSOA 层年度 bus 充电需求柱图**：top-50 LSOA 横轴 = LSOA code，纵轴 = GWh/年；长尾合并为 "others"
2. **LSOA 服务密度散点**：x = 该 LSOA 内 `n_home_blocks`，y = `sim_kwh_year`，点大小 ∝ `depot_total_kw`；揭示 "一个 LSOA 服务多少 block / 多少能耗"
3. **饱和度散点**：x = `depot_total_kw`，y = `sim_kwh_year`，对角线 = ceiling (`kW × 8760`)，gap_ratio > 0.5 的点用红色标记
4. **缺口 top-N 表**：`lsoa_code, n_home_blocks, sim_kwh_year, depot_total_kw, ceiling_kwh_year, gap_ratio`；按 sim_kwh_year 降序取前 10

**不引入 utilization 系数**；caption 直接写明 "ceiling = rated kW × 8760 (theoretical max)"。

---

## 2. Notebook 新结构

保留现有 A–F 全部 cell，**插入**新教学段。最终目录：

| 段 | 类型 | 内容 |
|---|---|---|
| **0. 这个 notebook 在做什么** | 新 markdown | 3 段：研究问题、方法骨架（管线 ASCII 图）、关键 caveat |
| **A. Feed-Year Calendar** | 现有 | 前面加 2 句 markdown 解释 service_id / calendar_dates |
| **A.5 出行模型逻辑** | 新 markdown + 1 cell | 概念阶梯 + 打印 protagonist block 3 行 trip + 一天 trips&parking timeline |
| **B. Pick a Protagonist Block** | 现有 | 前面加 1 句 markdown 说明为什么选 native + 40–250 km 区间 |
| **B.5 充电逻辑** | 新 markdown + 1 cell | SOC walk 公式 + 参数表 + depot_terminus 抽象 + warmup + infeasibility 4 reasons 表 |
| **C. Single Block Annual SOC** | 现有 | 前面加 1 句 markdown |
| **D. Small Fleet Annual Load** | 现有 | 前面加 1 句 markdown |
| **E. Annual Story Slices** | 现有四图全保留 | 每图前加 1 句 markdown 说明在看什么 |
| **E.5 LSOA → Synthetic Depot Map**（主分析） | 新 markdown + 3 cell | 假设声明 + home_lsoa 聚合 + 3 图 + 缺口表 |
| **E.6 M1 chain-mode diagnostics (transparency)** | 新 markdown + 2 cell | 镜像 notebook 01 §J：depot_registry confidence breakdown 4-panel + depot geo scatter；明确说明**只用于展示当前 inventory 数据现状，不参与 E.5 主分析** |
| **F. Honest Labels** | 现有 | 表里追加 5 行（见 §3.4） |
| Wall-clock 行 | 现有 | 不变 |

---

## 3. 关键实现细节

### 3.1 A.5 出行模型 cell

```python
# Markdown 后接：
trip_columns = ["trip_id", "start_stop_name", "end_stop_name",
                "start_h", "end_h", "distance_km", "start_lsoa", "end_lsoa"]
display(protagonist_block[trip_columns].head(3))

one_day = protagonist_result["schedules"][0]  # 第一个活跃日
fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
for trip in one_day.trips:
    ax.barh(0, trip.arrival_time - trip.departure_time,
            left=trip.departure_time, height=0.4, color="tab:blue", alpha=0.85)
for park in one_day.parking_events:
    ax.barh(1, park.end_time - park.start_time,
            left=park.start_time, height=0.4,
            color="lightgray" if park.location_purpose == "depot_terminus" else "khaki")
ax.set_yticks([0, 1]); ax.set_yticklabels(["trip", "parking"])
ax.set(title="Protagonist block: one active service day timeline",
       xlabel="hour of day", xlim=(0, 24))
ax.grid(alpha=0.25); plt.tight_layout(); plt.show()
```

如果 `protagonist_result["schedules"][0]` 是 inactive day（全 24h depot_terminus），改用第一个 `trips` 非空的 schedule。

### 3.2 B.5 充电逻辑 cell

```python
# Markdown: SOC 递推公式 (LaTeX) + 假设说明
parameter_table = pd.DataFrame([
    ("battery_kwh", protagonist_vehicle["battery_kwh"], "vehicle spec sample (BEV_Bus_Coach CSV)"),
    ("consumption_kwh_per_km", protagonist_vehicle["consumption_kwh_per_km"], "vehicle spec sample"),
    ("depot_charge_kw", protagonist_vehicle["depot_charge_kw"], "vehicle spec sample"),
    ("layover_charge_kw", 0.0, "notebook config (allow_layover_charging=False)"),
    ("warm_up_days", 0, "notebook smoke; production WARMUP_DAYS=14"),
    ("soc_init", 1.0, "fully charged on day 0"),
], columns=["param", "value", "source"])
display(parameter_table)

infeasibility_table = pd.DataFrame([
    ("single_trip_exceeds_battery", "单 trip 能耗 > 电池可用容量"),
    ("starts_below_min_required",   "第一段 trip 出发前 SOC 不足以完成该 trip"),
    ("depot_only_insufficient",     "仅 depot 充电时全天总能耗超过 soc_init×battery + depot 充电潜力"),
    ("midday_depletion",            "上述三条都不命中，但时间错配导致中途 SOC 触底"),
], columns=["reason", "meaning"])
display(infeasibility_table)
```

### 3.3 E.5 LSOA → Synthetic Depot Map cell（inline，不读 charger_registry.parquet）

```python
# 1) home LSOA per block — mode of end_lsoa
home_lsoa_by_block = (
    all_blocks.groupby("block_id")["end_lsoa"]
              .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else "")
)

# 2) demand × supply per LSOA — both derived purely from fleet_per_block
demand_df = (
    fleet_per_block
        .join(home_lsoa_by_block.rename("home_lsoa"), on="block_id")
        .reset_index()
)
lsoa_view = (
    demand_df.groupby("home_lsoa")
             .agg(n_home_blocks=("energy_charged_kwh", "size"),
                  sim_kwh_year=("energy_charged_kwh", "sum"),
                  depot_total_kw=("depot_charge_kw", "sum"))
             .sort_values("sim_kwh_year", ascending=False)
)
lsoa_view["ceiling_kwh_year"] = lsoa_view["depot_total_kw"] * 8760
lsoa_view["gap_ratio"] = lsoa_view["sim_kwh_year"] / lsoa_view["ceiling_kwh_year"]

# 3) Plots
# 图 1: top-50 bar of sim_kwh_year (长尾合并为 "others")
top_n = 50
top = lsoa_view.head(top_n).copy()
others_kwh = lsoa_view["sim_kwh_year"].iloc[top_n:].sum()
plot_df = pd.concat([top, pd.DataFrame({"sim_kwh_year": [others_kwh]}, index=["others"])])
fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
ax.bar(range(len(plot_df)), plot_df["sim_kwh_year"] / 1e6, color="tab:blue", alpha=0.85)
ax.set(title=f"Top-{top_n} home LSOAs by simulated annual bus charging energy",
       xlabel="LSOA rank", ylabel="GWh/year")
ax.grid(alpha=0.25); plt.tight_layout(); plt.show()

# 图 2: service density scatter
fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
ax.scatter(lsoa_view["n_home_blocks"], lsoa_view["sim_kwh_year"],
           s=lsoa_view["depot_total_kw"] / 5.0, alpha=0.5, color="tab:purple")
ax.set(title="LSOA service density: blocks vs annual demand (marker ∝ depot kW)",
       xlabel="n_home_blocks per LSOA", ylabel="sim_kwh_year")
ax.grid(alpha=0.25); plt.tight_layout(); plt.show()

# 图 3: saturation scatter
fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
red_mask = lsoa_view["gap_ratio"] > 0.5
ax.scatter(lsoa_view.loc[~red_mask, "depot_total_kw"],
           lsoa_view.loc[~red_mask, "sim_kwh_year"],
           alpha=0.5, color="tab:gray", label="gap_ratio ≤ 0.5")
ax.scatter(lsoa_view.loc[red_mask, "depot_total_kw"],
           lsoa_view.loc[red_mask, "sim_kwh_year"],
           alpha=0.8, color="tab:red", label="gap_ratio > 0.5")
kw_range = np.linspace(0, lsoa_view["depot_total_kw"].max(), 50)
ax.plot(kw_range, kw_range * 8760, color="k", lw=1.0, linestyle="--",
        label="ceiling = kW × 8760")
ax.set(title="LSOA depot saturation: annual demand vs theoretical ceiling",
       xlabel="depot_total_kw", ylabel="sim_kwh_year")
ax.legend(); ax.grid(alpha=0.25); plt.tight_layout(); plt.show()

# 表
display(lsoa_view.head(10).round(2))
```

**不读** `outputs/charger_registry.parquet`；depot 容量从 `fleet_per_block` 自己的 `depot_charge_kw` 列聚合得到。这避免了"合成 A 比合成 B"的循环验证。

### 3.4 E.6 M1 chain-mode diagnostics（transparency 配图，不入主分析）

**Markdown 首段必须显式说明**：

> 本节展示的是 operator-administrative 视角的 depot inventory（由 `scripts/run_bus_pipeline.py` 产生的 M1 outputs），其中 `depot_confidence` 大部分是 `low`，因为多数 agency 没有 TxC garage 数据，只能用 operator centroid 合成。E.5 的主分析**不依赖**这些数据，原因正是这一数据现状；E.6 在此呈现仅为对外透明度（"如果用现有 inventory 会怎样"）。完整 follow-up 路径见 `docs/bus_charging_next_steps.md` §4。

**实现要点**（与 notebook 01 §J 完全等价 cell；不要复用 / import，直接 inline 在 03 里）：

```python
M1_OUTPUT_DIR = REPO_ROOT / "outputs"
if (not (M1_OUTPUT_DIR / "resolution_summary.parquet").exists()
        and (M1_OUTPUT_DIR / "m1_smoke" / "resolution_summary.parquet").exists()):
    M1_OUTPUT_DIR = M1_OUTPUT_DIR / "m1_smoke"

m1_paths = {
    "depot_registry":     M1_OUTPUT_DIR / "depot_registry.parquet",
    "vehicles":           M1_OUTPUT_DIR / "vehicles.parquet",
    "vehicle_assignments":M1_OUTPUT_DIR / "vehicle_assignments.parquet",
    "vehicle_day_events": M1_OUTPUT_DIR / "vehicle_day_events.parquet",
    "resolution_summary": M1_OUTPUT_DIR / "resolution_summary.parquet",
}
missing_m1 = [name for name, path in m1_paths.items() if not path.exists()]
if missing_m1:
    display(pd.DataFrame({"missing_m1_output": missing_m1,
                          "expected_dir": str(M1_OUTPUT_DIR)}))
else:
    # 1) 4-panel: depots by confidence / fleet by depot / daily chains by depot /
    #    resolution levels by depot  —— 完全照搬 notebook 01 §J 第一个 code cell。
    # 2) 第二个 cell：top public charger station bar + depot geo scatter（按 confidence
    #    用 marker 区分 high="o" / medium="s" / low="^"），含底部 caption：
    #    "Virtual operator-centroid depots are illustrative only; LSOA attribution
    #     for these depots is low-confidence."
```

**约束**：
- E.6 cells 必须能在 M1 outputs 不存在时**优雅降级**（显示 missing-file table，不抛异常）；这与 notebook 01 §J 行为一致。
- E.6 任何图的数据都**不**反过来流入 lsoa_view / fleet_per_block 等 E.5 数据结构。
- E.6 markdown 与图标题中不得出现 "infrastructure gap" / "capacity adequacy" 等结论性措辞；只描述 inventory 现状。

### 3.5 Honest labels 追加

把 `F. Honest Labels` 的现有 DataFrame 末尾追加 5 行：

```python
extra_rows = [
    ("LSOA attribution rule", "home LSOA = mode of end_lsoa per block",
     "first-stop and last-stop equivalent given SOC continuity"),
    ("Depot inventory",       "synthesized from simulation",
     "one synthetic depot per block in its home LSOA; capacity = vehicle's depot_charge_kw"),
    ("Charging-supply scope", "depot only",
     "public OCM stations not yet integrated (see next-steps doc §1)"),
    ("Utilization",           "not modelled",
     "ceiling = depot_total_kw × 8760; queueing & utilization see next-steps doc §2"),
    ("Next steps",            "docs/bus_charging_next_steps.md",
     "follow-up PRs scoped there"),
]
honest_labels = pd.concat([honest_labels, pd.DataFrame(extra_rows, columns=honest_labels.columns)],
                          ignore_index=True)
```

---

## 4. 新增文件 `docs/bus_charging_next_steps.md`

```markdown
# Bus charging modelling — next steps

Status: 2026-05-13
Owner: zhichuanma

This list scopes the assumptions deliberately simplified in
`notebooks/03_bus_annual_simulation.ipynb` E.5. Each item is a follow-up PR.

## 1. Public charger eligibility for bus
Current: only synthetic per-block depots are considered. Public OCM chargers
are excluded because they are not spec'd for bus dwell patterns / power.
Next: define an eligibility rule (e.g. ≥150 kW DC + accessible site type)
and split public stations into "bus-eligible" vs "car-only".

## 2. Utilization & queueing
Current: ceiling computed as `depot_total_kw × 8760` (theoretical max).
Next: introduce per-station-kind utilization or full queueing
(M/G/c or discrete-event) so kWh demand competes for finite charge slots
across overlapping block end times.

## 3. Per-event station matching (cars-style Huff)
Current: charging is attributed at the LSOA level post-hoc, with one
synthetic depot per block.
Next: attach `location_lsoa` to each `depot_terminus` parking event and
extend `mobility.cars.station_matcher` style Huff to bus depots,
producing per-event `matched_station_id` and contention with car demand.

## 4. Real depot inventory
Current: depot map is synthesized — one depot per block at the block's
home_lsoa, capacity = the vehicle's depot_charge_kw. This means the depot
map is a reflection of the timetable, not of real infrastructure.
Next: replace synthetic depots with operator-reported depot inventory
(`outputs/depot_registry.parquet` provides one depot per agency from TxC
garages + virtual operator-centroid fallback; CPT / DfT / OS open data can
extend coverage), allowing one physical depot to serve multiple blocks,
capturing real geographic clustering, and exposing under-served operators
whose actual depot capacity is below the synthesized ceiling.

## 5. Cross-modal contention
Current: cars and bus consume separate registries.
Next: unified national charger registry, with bus, coach, car, LGV demands
all running through the same station capacity in time.
```

---

## 5. 写作风格规则

- 每个新 markdown cell **首句 = 这段在解释什么**；后续 1–3 句给 motivation 或 caveat；不超过 6 句。
- 公式用 inline LaTeX。
- 不写 "Now we …" / "Let's …" 学生腔；用陈述句。
- 关键术语首次出现时英文+中文括注一次，之后只用英文：`block_id (车辆运营块)`。
- 所有数字结论后挂一句 caveat：feed-year 不是 2025、smoke 4 blocks 不是 fleet、E.5 的 depot 容量是 simulation-self-consistent 合成而非真实 inventory。
- E.5 markdown 必须显式 link 到 `docs/bus_charging_next_steps.md`。

---

## 6. 不要做的事

- 不要改 `mobility/core/simulator.py`、`sim_adapter.py`、`trip_chain_bus.py`、`annual_simulation.py` 既有逻辑。
- 不要把 `home_lsoa` 归因规则替换 simulation 输入；它只是 post-hoc 可视化。
- 不要在 notebook 里 `pip install` / `df.to_csv` / `df.to_parquet`。
- 不要引入 geopandas / shapely / pyproj 到 Modelling runtime。
- 不要扩大 `simulate_block_year` / `simulate_fleet_year` 的对外签名。
- 不要在 E.5 引入 utilization 系数；理论上限即 `kW × 8760`，把 utilization 显式留给 next-steps。
- E.5 **不读** `outputs/charger_registry.parquet`；depot 容量从 `fleet_per_block` 自己的 `depot_charge_kw` 列聚合得到，避免"合成 A 比合成 B"的循环验证。
- E.6 可以读 `outputs/{depot_registry,vehicles,vehicle_assignments,vehicle_day_events,resolution_summary}.parquet`（或 `outputs/m1_smoke/` 下同名 fallback），但**仅作 transparency 展示**；任何 E.6 中的数值禁止流回 E.5 的聚合或缺口表。
- 不要 import `mobility.coach.*`。
- 不要 `--no-verify` 跳 hooks。
- 不要把现有四张分析图删掉或合并。
- 不要新建 `mobility/bus/annual_lsoa_aggregation.py`；所有聚合 inline 在 notebook（便于读者直接看到逻辑）。

---

## 7. 验收

- `python notebooks/_build_03_bus_annual_narrative.py` 重生 notebook，cell 数从 ~17 增至 ~29（新 12 cell：3 段教学 markdown + A.5 markdown + A.5 code + B.5 markdown + B.5 code + E.5 markdown + E.5 code + E.6 markdown + E.6 code × 2）。
- `jupyter nbconvert --to notebook --execute --inplace notebooks/03_bus_annual_simulation.ipynb` < 100 秒（E.6 多读 5 个 parquet，加 10s 上限可接受；M1 outputs 缺失时 E.6 走 missing-file fallback，不抛异常）。
- 所有新增图 figsize=(12, 4.5) dpi=110，关 top/right spine（与现有风格一致）。
- 公开 API 无新签名变化（`grep "def simulate_block_year\|def simulate_fleet_year"` 函数签名不变）。
- `docs/bus_charging_next_steps.md` 存在且至少包含 5 个 numbered next-step items。
- Five-minute readability test（不需 Codex 执行，仅作 narrative 完整性目标）：一个非本项目研究者应能回答：（a）"输入是什么？"（b）"SOC 怎么变？"（c）"年度结果里 LSOA 是干嘛的、depot 容量怎么来的、为什么不和公共桩对比？"

---

## 8. PR description 必须包含

```markdown
## Summary
Pedagogical refactor of notebook 03_bus_annual_simulation.ipynb so that a
researcher unfamiliar with this codebase can follow the modelling decisions:
- New A.5 explains the GTFS → block → trip → DailySchedule pipeline with a
  protagonist-block timeline.
- New B.5 explains the SOC walk, parameter sources, depot_terminus abstraction,
  warmup, and the four infeasibility reasons.
- New E.5 attributes per-block annual charging kWh to a "home LSOA" and
  builds a self-consistent synthetic depot map (one depot per block at its
  home_lsoa, capacity = vehicle's depot_charge_kw). Public OCM stations
  excluded; no utilization factor — ceiling = depot_total_kw × 8760.
- New E.6 mirrors notebook 01 §J: depot_registry confidence breakdown +
  M1 chain-mode diagnostics, as transparency on the operator-administrative
  inventory (mostly low-confidence virtual centroids). E.6 is illustrative
  only; the main E.5 analysis does not depend on it.
- All existing analytical figures (A–F) preserved; honest-labels extended.
- New docs/bus_charging_next_steps.md scopes follow-up work on public charger
  eligibility, utilization & queueing, per-event station matching, real depot
  inventory, and cross-modal contention.

## Verification
- python notebooks/_build_03_bus_annual_narrative.py  →  wrote .ipynb
- jupyter nbconvert --to notebook --execute --inplace ...  →  <90s
- No public API changes (grep simulate_block_year / simulate_fleet_year sigs)

## Public-API changes
None.

## Deviations from AGENTS.md
None.
```
