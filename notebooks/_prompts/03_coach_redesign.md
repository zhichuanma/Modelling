# Prompt: 在 `mobility/coach/` 之上新建仿真层 + 写 `notebooks/02_single_coach_simulation.ipynb`

## 0. 上下文

`mobility/coach/` 目前只有一个 `txc_parser.py`（575 行，已稳定）和一个空 `__init__.py`。它能把 TransXChange XML 解析成 trip table（一行一 vehicle journey, 25 列），但缺仿真所需的物理量。任务是按 [01_bus_redesign.md](01_bus_redesign.md) 已经验证有效的纪律给 coach 加完整的"trip → DailySchedule → SOC"流水线 + 单 coach 叙事 notebook。

继承约束：仓库根 [AGENTS.md](../../../AGENTS.md) 全部硬规则（单位后缀 / RNG / parquet / 不引入 `holidays/geopandas/pyproj`）。本 PR **不允许改 `txc_parser.py`**——它经过手工调过，是稳定基座。所有新逻辑落在新模块。

冻结资源：

- 原始 TxC XML：`../Data/EV_behavior/Coach_Data/TxC-2.4/{BHAT,FLIX,MEGA,NATX,PKOH,SCLK}/*.xml`（337 个）
- Inventory：`TxC-2.4/TxCInventory17APR26.csv` (operator × line × xml path)
- Custom stops：`TxC-2.4/CustomStopsList17APR26.csv` (78 行 coach 专用站，带 `Latitude / Longitude / Easting / Northing`)

需要外部输入（**用户必须先放置**）：

- **NaPTAN UK stops 数据库**：已下载到 `Modelling/data/Stops.csv` (101 MB, 434,568 行)。注意大写 `S`——文件原名就是 `Stops.csv`，**不要**改成 `stops.csv` 或 `naptan_stops.csv`。

  实测覆盖（agent 必须知道这个数字）：
  - 1,367 个 unique TxC stop refs 中：**92.3% (1,262)** 在 NaPTAN 带 lat/lon，7.7% (105) 在 NaPTAN 但 lat/lon 是 NaN，0 个完全缺失
  - 但 stop 级 92% 命中**不等于** journey 级 92%——因为一个 vehicle journey 通常 5–15 站，任意一站缺坐标整条 journey 就 `distance_source='unknown'`
  - **journey 级实测：14,041 个 vehicle journey 中，50.9% (7,146) 全部 stop 可定位、49.1% (6,895) 至少一站 NaN**
  - 仿真只能在那 7,146 条上跑；这是数据现实，不是 bug。在 Stage A.5 明示

- **真实 e-coach fleet 数据**：`Modelling/data/EV_UK_LSOA_2025_with_energy.csv` 已存在（1.58M 行），过滤 `vehicle_subtype == 'coach'` 得到 201 行 / 3 款车（其中 2 款带完整规格）：

  | Model | Energy_kWh | DC_Power_kW | AC_Power_kW | efficiency_wh_per_km |
  |---|---|---|---|---|
  | YUTONG TC12 | 281.0 | 150.0 | 22.0 | 810.0 |
  | YUTONG GTE14 | 563.0 | 150.0 | 22.0 | 1166.0 |
  | YUTONG TC9 | NaN | NaN | 22.0 | 810.0 |

  仿真**禁止使用编死的 500 kWh 默认值**——必须从这张表抽 EV，与 cars notebook 风格一致。

---

## 1. 必须解决的 6 个 coach 特有问题

### 1.1 距离完全没有，必须外挂 NaPTAN

[txc_parser.py:149-162](../../mobility/coach/txc_parser.py) 的 `parse_stop_points` 只产出 `stop_point_ref / common_name / indicator / locality_*`——TxC 不带坐标。新模块必须：

- 加载 NaPTAN（`Modelling/data/naptan_stops.csv`）+ custom stops（`TxC-2.4/CustomStopsList17APR26.csv`）合成统一的 stop→(lat, lon) 表
- 优先用 NaPTAN，custom stops 兜底（custom 是 NaPTAN 没收录的 coach 站）
- 把每个 vehicle_journey 的 stop 序列拉出来，逐段 haversine 求和得到 `distance_km_haversine`
- 加一个**显式公开**的 `road_detour_factor: float = 1.30` 参数（默认 1.30，建模假设：长途 motorway 比直线长约 30%），算出 `distance_km = distance_km_haversine * road_detour_factor`
- 在最终 parquet 加 `distance_source` 列（`'haversine_x_detour' | 'unknown'`）。`'unknown'` 表示该 vehicle journey 至少有一个 stop 在 NaPTAN+custom 都查不到——这种 journey **不允许进入仿真**，但要保留在汇总表里供数据质量审计

### 1.2 跨午夜：与 bus 同模式

TxC `clock_time` 字段允许 `30:45:00` 这种 24+ 小时表示（夜车）。`parse_clock_to_seconds` 已正确处理为 `30*3600 + 45*60`。trip_chain_coach 必须把单 vehicle journey 拆成 1 或 2 个 `DailySchedule`（同 [01_bus_redesign.md §1.1](01_bus_redesign.md)）：

- `start_h < 24 且 end_h ≤ 24`：返回 1 个 day0 schedule
- `start_h < 24 且 end_h > 24`：返回 2 个 schedule（day0 截到 24h，day1 装 0..end_h-24）
- `start_h ≥ 24`：理论上不该出现（vehicle journey 不该开始于"次日凌晨" 在同一个 TxC 里），如果出现 raise

能耗按时长比例切分以保持守恒。

### 1.3 单 vehicle journey = 一辆车一天，不要造"block"

bus 需要 `infer_blocks` 把 trip 串成车-天，coach **不需要**——TxC 的 `VehicleJourney` 本身就是"一辆车跑一趟服务"。一辆车一天通常跑 1 个 outbound + 1 个 inbound（或就 1 个长途），多 vehicle journey 之间是否归同一辆车在 coach 运营里不重要（夜里在终点饭店休息，第二天开回程，物理上是同一辆车，但充电决策互相独立）。

所以本 PR 的"单元"是 `vehicle_journey_code`，不是"coach block"。**禁止**写任何 block_inference 类的代码。

### 1.4 单充可行性是个**分布**——诚实展示，不要预设失效

实测（4661 个真实 vehicle journey + 真实 coach EV）：

```
YUTONG TC12  range~347 km → 80% of journeys feasible
YUTONG GTE14 range~483 km → 92% of journeys feasible
```

绝大多数 coach 跑得完。但仿真器 `_soc_walk` 在 SOC < 0 时静默 clamp 到 0 继续跑（[mobility/core/simulator.py:251-253](../../mobility/core/simulator.py#L251)），所以**不可行的 10–20% 路线不会报错，会给出虚假的 `soc_end`**——必须显式拦截。

新增 `feasibility.py`，提供：

```python
def journey_feasibility(
    distance_km: float,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    safety_margin: float = 0.05,
) -> dict:
    """Return {'feasible_single_charge': bool, 'energy_required_kwh': float,
                'usable_energy_kwh': float, 'shortfall_kwh': float,
                'min_soc_required': float}."""
```

每个 vehicle journey 在仿真前都先调一次。`simulate_coach_journey` 返回字典里加 `feasibility` 字段。**不要**在 notebook 里预设 protagonist 失败的剧本——随机抽样、跑出什么样就报什么样。如果 protagonist 恰好是 `feasible=False`，再切到 contrast 找一个 feasible 的对照；反之亦然。Stage E 名字从"infeasibility wall"改成"the feasibility frontier"，主表展示**全表 feasibility 比例分布**（按 EV model × 按 operator 切片），而不是单一失败案例煽情。

### 1.5 终点 dwell 是 coach 的"depot"，但语义和 bus 不同

bus 的 depot_terminus 是首末 stop 的占位抽象。coach 通常**真的**在终点过夜（司机休息 + 法定 11h rest）。`trip_chain_coach` 生成的 ParkingEvent 用 `location_purpose='terminus_dwell'`（不是 `depot_terminus`，避免和 bus 混），并接受外部传入 `terminus_charge_kw`（默认 50 kW DC，反映"终点站可能有快充也可能没有"的不确定性）。

**禁止**默认 `terminus_charge_kw = 100 kW`——coach 终点不像 bus depot 那样确定有大功率充电桩。

### 1.6 Operating profile = 字符串，不展开日期

[txc_parser.py:92-127](../../mobility/coach/txc_parser.py#L92) 的 `_parse_operating_profile` 已经把 `RegularDayType` 提取成字符串（`"DaysOfWeek"` 这种），把 `SpecialDaysOperation` 的日期范围列出来。本 PR **不展开日期**——和 bus 一样停在"a representative service day"层级。identity card 不允许出现 `date` 字段，`operating_profile` 字段保留原始字符串供读者参考。

---

## 2. 模块设计 — `mobility/coach/`

7 个新文件 + 不改 `txc_parser.py`：

```
mobility/coach/
├── __init__.py               ← rewrite (re-export public surface)
├── txc_parser.py             ← UNCHANGED (575 lines, frozen)
├── stop_geometry.py          ← NEW: NaPTAN + custom stops → unified coord lookup
├── distance.py               ← NEW: vehicle_journey → distance_km via haversine + detour
├── coach_fleet.py            ← NEW: load real Yutong fleet from EV_UK_LSOA_2025_with_energy.csv
├── feasibility.py            ← NEW: per-journey single-charge feasibility check
├── trip_chain_coach.py       ← NEW: vehicle_journey → list[DailySchedule]
├── sim_adapter.py            ← NEW: simulate_coach_journey, simulate_fleet_journeys
├── selection.py              ← NEW: protagonist sampling (no operator filter)
├── data_loader.py            ← NEW: orchestrate inventory → trip table → parquet
└── build_all_journeys.py     ← NEW: offline one-shot builder, writes
                                     outputs/all_coach_journeys.parquet
```

### 2.1 `stop_geometry.py`

```python
NAPTAN_PATH = Path(__file__).resolve().parents[2] / "data" / "Stops.csv"
# CustomStopsList17APR26.csv has 78 coach-only stops, all of which (per audit)
# overlap with NaPTAN already — kept as a defensive fallback only.
CUSTOM_STOPS_PATH = (
    Path(__file__).resolve().parents[3] / "Data" / "EV_behavior" / "Coach_Data" /
    "TxC-2.4" / "CustomStopsList17APR26.csv"
)

def load_unified_stops(
    naptan_path: Path = NAPTAN_PATH,
    custom_stops_path: Path = CUSTOM_STOPS_PATH,
) -> pd.DataFrame:
    """Return columns: stop_point_ref, lat, lon, source ('naptan' | 'custom').

    Drops NaPTAN rows where Latitude or Longitude is NaN before the union
    (those stops are present in the registry but lack coords — ~12% of NaPTAN).
    NaPTAN takes precedence; custom stops fill gaps.

    Returns empty DataFrame with the right columns if NaPTAN file missing —
    callers must handle this gracefully.
    """

def naptan_available(naptan_path: Path = NAPTAN_PATH) -> bool: ...
```

### 2.2 `distance.py`

```python
def vehicle_journey_distance_km(
    stop_sequence: pd.DataFrame,         # cols: stop_point_ref, stop_sequence
    stops_geom: pd.DataFrame,             # from load_unified_stops
    *,
    road_detour_factor: float = 1.30,
) -> tuple[float, str]:
    """Return (distance_km, distance_source).

    distance_source ∈ {'haversine_x_detour', 'unknown'}.
    'unknown' if any stop in the sequence has no coords.
    """

def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray | float:
    """Vectorised; embedded here, NOT imported from mobility.bus.block_inference."""
```

⚠️ **不要** import `mobility.bus.block_inference.haversine_km` 跨子包——重复实现 30 行不痛不痒，跨子包耦合更糟。

### 2.3 `feasibility.py`

```python
def journey_feasibility(
    distance_km: float,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    safety_margin: float = 0.05,
) -> dict:
    """Return:
        {'feasible_single_charge': bool,
         'energy_required_kwh': float,
         'usable_energy_kwh': float,        # battery_kwh * (1 - safety_margin)
         'shortfall_kwh': float,            # max(0, energy_required - usable)
         'min_soc_required': float}         # energy_required / battery_kwh + safety_margin
    """
```

### 2.4 `trip_chain_coach.py`

```python
def journey_to_daily_schedules(
    journey_row: pd.Series,                # one row from all_coach_journeys.parquet
    stop_sequence: pd.DataFrame,           # ordered stops for this VJ
    *,
    consumption_kwh_per_km: float,
    terminus_charge_kw: float,
    pre_journey_dwell_h: float = 6.0,      # default: 6h pre-trip dwell (driver rest, charge window)
    post_journey_dwell_h: float | None = None,  # if None, fill to end of day
) -> list[DailySchedule]:
    """Convert one vehicle journey into 1 or 2 DailySchedule objects.

    Trip structure:
      - one Trip object per vehicle journey (the long-haul run itself).
        Origin/destination purposes both 'coach_terminus'.
      - Two ParkingEvent objects: pre-journey terminus dwell (charges)
        and post-journey terminus dwell (charges).
      - Cross-midnight: split as in §1.2.
    """
```

### 2.5 `coach_fleet.py` (新增) + `sim_adapter.py`

新增 `coach_fleet.py`，类比 [mobility/cars/data_loader.py](../../mobility/cars/data_loader.py)：

```python
COACH_FLEET_PATH = Path(__file__).resolve().parents[2] / "data" / "EV_UK_LSOA_2025_with_energy.csv"

def load_coach_fleet(path: Path = COACH_FLEET_PATH) -> pd.DataFrame:
    """Read EV_UK_LSOA_2025_with_energy.csv, filter vehicle_subtype == 'coach',
    drop rows with NaN Energy_kWh or efficiency_wh_per_km.

    Returns columns: EV_ID, Model, Energy_kWh, DC_Power_kW, AC_Power_kW,
    efficiency_wh_per_km, consumption_kwh_per_km (= efficiency_wh_per_km / 1000),
    LSOA_code, count.
    """

def sample_coach_ev(
    fleet_df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    weight_by_count: bool = True,
) -> pd.Series:
    """Sample a single coach EV row weighted by per-LSOA count (default)
    or uniformly across rows."""
```

`sim_adapter.py` 的接口跟着改：

```python
DEFAULT_TERMINUS_CHARGE_KW = 50.0   # coach end-of-line charger; conservative

def simulate_coach_journey(
    journey_row: pd.Series,
    stop_sequence: pd.DataFrame,
    ev_spec: pd.Series,                       # one row from load_coach_fleet
    *,
    terminus_charge_kw: float = DEFAULT_TERMINUS_CHARGE_KW,
    soc_init: float = 1.0,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Battery and consumption come from ev_spec — Energy_kWh and consumption_kwh_per_km.
    Returns {schedules, soc, load_kw, soc_end, soc_min, energy_charged_kwh,
              terminus_kwh, total_km, total_consumed_kwh, feasibility,
              ev_model, battery_kwh, consumption_kwh_per_km}."""

def simulate_fleet_journeys(
    journeys_df: pd.DataFrame,
    stop_sequences: dict[str, pd.DataFrame],  # vehicle_journey_code -> stop df
    fleet_df: pd.DataFrame,
    rng: np.random.Generator,
    **kwargs,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Each journey gets a coach EV sampled from fleet_df with per-journey rng-derived seed.
    Same wrap-back-to-96-step convention as bus.simulate_fleet_blocks."""
```

**禁止**写死 `BATTERY_KWH` 默认值。所有仿真必须从 `load_coach_fleet()` 抽 EV。

### 2.6 `selection.py`

```python
def sample_protagonist_journey(
    journeys_df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    runtime_h_range: tuple[float, float] = (1.0, 8.0),
    require_no_cross_midnight: bool = True,
    require_known_distance: bool = True,
) -> str:                                # vehicle_journey_code
    """Pure random sample over journeys satisfying the basic filters.
    NO operator filter — all 6 operators (BHAT/FLIX/MEGA/NATX/PKOH/SCLK)
    are eligible."""

def sample_contrast_journey(
    journeys_df: pd.DataFrame,
    rng: np.random.Generator,
    protagonist_id: str,
    *,
    require_different_operator: bool = False,
    require_distance_gap: float = 0.5,    # |contrast.km - protagonist.km| / max(prot.km,1) >= 0.5
    **kwargs,
) -> str:
    """Pick a journey with a meaningfully different distance profile from the
    protagonist. Does NOT prescribe feasibility — let the data say what it says."""

def render_journey_identity_card(
    journeys_df: pd.DataFrame,
    journey_id: str,
) -> pd.DataFrame:
    """Fields: vehicle_journey_code, operator_code, line_name,
       start_stop_name, end_stop_name, runtime_h, distance_km,
       distance_source, regular_day_types, n_stops,
       service_day_label='a representative service day'."""
```

protagonist + contrast 都是**真随机抽样**（仅过滤 cross-midnight 和 known-distance）。叙事不预设"成功 vs 失败"——抽到什么报什么，feasibility 由 `journey_feasibility` 单独说。如果一对样本都 feasible 或都 infeasible，那也是诚实的数据现象，notebook 用 Stage E 的全表分布图补足全局视角。

### 2.7 `data_loader.py` + `build_all_journeys.py`

`build_all_journeys.py` 是离线一次性脚本（类比 bus 的 `build_all_blocks.py`，但 coach 数据小，跑得快——预计 < 30 秒）：

1. 读 `TxC-2.4/TxCInventory17APR26.csv`
2. 对每个 XML 调 `txc_parser.build_trip_table_from_xml`
3. concat 成全表
4. 读 NaPTAN + custom stops
5. 对每个 vehicle journey 计算 `distance_km` 和 `distance_source`，写 parquet 到 `outputs/all_coach_journeys.parquet`
6. 同时写 `outputs/all_coach_stop_sequences.parquet`（按 `vehicle_journey_code` 分组的 stop 序列），仿真时按需 lazy load

`data_loader.py` 提供 `load_all_coach_journeys()` 和 `summarize_journey_quality()`，签名风格对齐 `mobility.bus.data_loader`。

`summarize_journey_quality()` 返回单行：

| 字段 | 说明 |
|---|---|
| `n_journeys` | 总 vehicle journey 数 |
| `n_xmls` | 唯一 XML 数 |
| `n_operators` | 唯一 operator 数 |
| `pct_known_distance` | `distance_source != 'unknown'` 占比 |
| `pct_cross_midnight` | `end_h ≥ 24` 占比 |
| `distance_km_p50 / p95` | 已知距离的中位数 / P95 |
| `pct_feasible_at_default_specs` | 默认电池/能耗下 feasible 比例 |

### 2.8 `__init__.py`

```python
from .data_loader import load_all_coach_journeys, summarize_journey_quality
from .coach_fleet import load_coach_fleet, sample_coach_ev
from .sim_adapter import simulate_coach_journey, simulate_fleet_journeys
from .selection import (
    sample_protagonist_journey, sample_contrast_journey, render_journey_identity_card,
)
from .feasibility import journey_feasibility
```

---

## 3. 测试 — `tests/mobility/coach/`

每个 ≤ 100 行，pytest 不许联网、不许写盘到 `outputs/`：

| 文件 | 断言 |
|---|---|
| `test_stop_geometry.py` | NaPTAN 缺失时 `load_unified_stops` 返回空 frame 而不抛；custom stops 优先级 / 列存在性 |
| `test_distance.py` | 手工 3-stop 序列（lat/lon 已知）haversine 计算正确（与解析公式吻合到 1e-3 km）；`distance_source='unknown'` 当任一 stop 缺坐标时 |
| `test_feasibility.py` | 距离 100 km / 500 kWh / 1.7 kWh/km → feasible=True；距离 400 km → feasible=False，shortfall_kwh > 0 |
| `test_trip_chain_cross_midnight.py` | 20:00→26:00 的 vehicle journey 拆出 day0+day1，能耗守恒 |
| `test_sim_adapter_simulate_coach_journey.py` | 200 km / 真实 Yutong GTE14 (563 kWh, 1.166 kWh/km) / 50 kW terminus，soc_init=1.0：断言 `soc_min > 0`、`energy_charged_kwh ≈ terminus_kwh`、`feasibility['feasible_single_charge'] == True` |
| `test_sim_adapter_infeasible.py` | 600 km journey + 真实 Yutong TC12 (281 kWh, 0.81 kWh/km)：`soc_min == 0` 且 `feasibility['feasible_single_charge'] == False`、`feasibility['shortfall_kwh'] > 0` |
| `test_selection.py` | 固定 seed 抽 protagonist：known distance、no cross-midnight、runtime ∈ [1, 8]h；**不**断言 feasibility（feasibility 由数据决定）；contrast 距离与 protagonist 至少差 50% |
| `test_coach_fleet.py` | `load_coach_fleet()` 返回 ≥ 2 行（GTE14 和 TC12），所有行 `Energy_kWh > 0` 且 `consumption_kwh_per_km > 0`；`sample_coach_ev` 在固定 seed 下确定性 |
| `test_data_loader.py` | 跑 `build_all_journeys` 在 fixture 上能产出 parquet；`summarize_journey_quality` 返回单行单列校验通过 |

测试 fixture：把 2–3 个真实 XML 复制到 `tests/mobility/coach/fixtures/`（或者构造 minimal 合成 TxC XML 字符串）。**禁止**测试依赖 `../Data/EV_behavior/Coach_Data/`——CI 不会有这个路径。

---

## 4. Notebook — `notebooks/02_single_coach_simulation.ipynb`

配套 builder `_build_02_coach_narrative.py`，与 [_build_01_bus_narrative.py](../_build_01_bus_narrative.py) 同结构。

| # | 标题 | 关键操作 |
|---|---|---|
| 0 | Units & time grid | 同 bus notebook，stub schedule (500 kWh / 50 kW / 18h dwell) 跑 `simulate_single_day` |
| A | What `all_coach_journeys.parquet` is | 调 `load_all_coach_journeys + summarize_journey_quality`；画 distance / runtime / operator 三联图 |
| **A.5** | **Honest labels for the data** | 6 行：(1) NaPTAN 是否到位、(2) 距离 = haversine × 1.30 而非真实路网（OSRM 留下个 PR）、(3) `distance_source='unknown'` 占比、(4) 跨午夜占比、(5) calendar 没展开、(6) 单充可行性是分布、不是绝对值 |
| **A.7** | **Real coach EV fleet** | 新增节：调 `load_coach_fleet()`，列出 Yutong TC12 / GTE14 / TC9 的规格；画一张 range_km 横条对比图（range_km = Energy_kWh / consumption_kwh_per_km）；说明仿真**不写死电池容量**，从这张表抽 |
| B | Picking protagonist + contrast | `MAIN_COACH_SEED = 20260501`、`ALT_COACH_SEED = MAIN_COACH_SEED + 1`；调 `sample_protagonist_journey` + `sample_contrast_journey`（**纯随机**，仅过滤 cross-midnight + known-distance），同时调 `sample_coach_ev` 抽一辆 EV（按 fleet count 加权） |
| C | Journey → schedule | 调 `journey_to_daily_schedules`，列 trips + parking_events 表；水平甘特图（trip 橙、terminus_dwell 蓝） |
| D | Baseline SOC + feasibility check | 先调 `journey_feasibility` 输出 `feasible / energy_required_kwh / shortfall_kwh`；再跑 `simulate_coach_journey`，画 SOC + load_kw 双轴图；如果 `feasibility=False`，**整张图加红色边框** + 在 SOC 触底处画水平虚线，但**不要**写"the model failed"——写"this journey exceeds the sampled EV's single-charge range by X kWh" |
| **E** | **The feasibility frontier (全表视角)** | 关键节：对**全表**已知距离的 journey 跑 `journey_feasibility`，按 EV model × operator 切片，画热力图 / 累积分布；展示实测 80% (TC12) / 92% (GTE14) feasible 的真实分布；点出 protagonist 和 contrast 在分布中的位置 |
| F | Sensitivity grid | 3×3：`battery_kwh ∈ {281, 400, 563}`（含两款真实 Yutong）× `consumption ∈ {0.81, 1.17, 1.5}` kWh/km（含两款真实效率），记 `soc_min` 和 `feasibility`，画热力图 |
| G | Operator-level context (optional) | 全表按 operator 聚合 distance / pct_feasible，给所有 6 个 operator 一张并排图。如果 NaPTAN 缺失导致 `pct_known_distance < 50%`，跳过本节并打印诚实说明 |
| H | Final identity card | ≥ 13 字段：`vehicle_journey_code, operator, line, start_stop_name, end_stop_name, distance_km, runtime_h, ev_model, battery_kwh, consumption_kwh_per_km, terminus_charge_kw, soc_end, soc_min, feasible_single_charge` + wall-clock |

全程 < 30s（数据量比 bus 小一个量级）。所有图 `figsize=(12, 4.5), dpi=110`，关上/右 spine。不导入 `mobility.cars` / `mobility.bus`。

---

## 5. 不要做的事

- 不要改 `mobility/coach/txc_parser.py`（575 行手工调过的 XML 解析，本 PR 视为冻结基座）
- 不要新引入依赖（`xml.etree.ElementTree` 已经够用）
- 不要在 notebook 里 `pip install` / 写 `df.to_csv` / `df.to_parquet`
- 不要 import `mobility.bus.*` 或 `mobility.cars.*`——coach 是独立子包
- 不要把 `np.random.seed` / `random.seed` / `random.choice(无 rng)` 写进任何文件
- 不要把 NaPTAN 缺失当 fatal——必须降级到"distance unknown / 跳过 SOC"路径，notebook 在 A.5 节诚实说明
- 不要给 coach 写 block inference——TxC 的 vehicle_journey 就是仿真单元，不需要拼车-天
- 不要 `terminus_charge_kw` 默认 100 kW 或更高——coach 终点充电基础设施远不如 bus depot 确定，50 kW 已经偏乐观
- 不要在仿真中悄悄"修正"长途 coach 跑不完的问题（比如自动降能耗、自动加电池）——这是模型边界，必须暴露
- 不要把 `regular_day_types='DaysOfWeek'` 这种字符串当真实日历——本轮 punt，与 bus 一致
- 不要用 `road_detour_factor != 1.30` 作为默认值——这是已知粗糙假设，但要稳定可复现
- **不要在 sim_adapter 写死 `BATTERY_KWH = 500.0` 这种默认值**——必须从 `load_coach_fleet()` 抽 EV
- **不要在 selection 加 operator_top_k 过滤**——总共 6 个 operator，全部纳入抽样
- 不要预设 protagonist 必须 feasible / contrast 必须 infeasible——纯随机，feasibility 由数据自己说

---

## 6. PR description 必须包含

沿用 [AGENTS.md §6](../../../AGENTS.md)：

```markdown
## Summary
Coach simulation layer on top of frozen txc_parser:
- Builds outputs/all_coach_journeys.parquet with haversine-based distance + detour factor.
- Adds journey-level feasibility check that surfaces single-charge shortfalls instead of
  letting the simulator silently clamp SOC to 0.
- Notebook 02 contrasts a feasible protagonist (50-250 km) against an infeasible
  contrast (>=400 km) to make the model's range limit explicit.

## Verification
- pytest tests/mobility/coach/ -v   →   N/N passed (paste output)
- python -m mobility.coach.build_all_journeys   →   wrote outputs/all_coach_journeys.parquet
- jupyter nbconvert --execute notebooks/02_single_coach_simulation.ipynb   →   completed in <X>s

## Dependency changes
None.

## Deviations from AGENTS.md
None.

## Data dependencies
- Requires Modelling/data/naptan_stops.csv (user-provided NaPTAN UK stops dump).
- Without it, the pipeline runs but distance is 'unknown' for all journeys and
  notebook Stage D-G are skipped with an explicit note. Stages 0/A/A.5/B/C still run.
```

---

## 7. 实测参考（agent 在交付前先跑这些 sanity check）

```bash
# Coach raw data sanity:
ls ../Data/EV_behavior/Coach_Data/TxC-2.4/{BHAT,FLIX,NATX,PKOH,SCLK}/*.xml | wc -l    # 337
wc -l ../Data/EV_behavior/Coach_Data/TxC-2.4/CustomStopsList17APR26.csv               # 79 (含 header)
wc -l data/Stops.csv                                                                   # 434,569

python -c "from mobility.coach.txc_parser import build_trip_table_from_xml; \
           import time; t=time.time(); \
           df = build_trip_table_from_xml('../Data/EV_behavior/Coach_Data/TxC-2.4/NATX/NATX-National_Express-180-Glasgow-Birmingham.xml'); \
           print(f'rows={len(df)}, t={time.time()-t:.2f}s')"     # ≈ rows=4, t<0.05s

# After build_all_journeys.py runs:
python -c "import pandas as pd; \
           df = pd.read_parquet('outputs/all_coach_journeys.parquet'); \
           print(df.shape, df['operator_code'].value_counts().to_dict()); \
           print(df['distance_source'].value_counts().to_dict())"
# expected: ~14,041 rows total, NATX > FLIX > SCLK > others
# distance_source: ~7,146 'haversine_x_detour' / ~6,895 'unknown'

# Coach EV fleet sanity:
python -c "import pandas as pd; \
           df = pd.read_csv('data/EV_UK_LSOA_2025_with_energy.csv'); \
           coach = df[df['vehicle_subtype']=='coach'].drop_duplicates('Model'); \
           print(coach[['Model','Energy_kWh','efficiency_wh_per_km']].to_string(index=False))"
# expected: YUTONG TC12 (281, 810), GTE14 (563, 1166), TC9 (NaN, 810)
```

---

## 8. 已知要在下个 PR 处理（不在本 PR scope）

- 真实路网距离（OSRM 或 Valhalla）替代 haversine × 1.30
- TxC `OperatingProfile` 完整展开成日期序列
- 中途停车的"opportunity charging"——目前 coach 长途几乎不停，但未来如果建模"司机法定 4.5h 必停 45min"可以利用
- 真实 charger 位置数据库（ZapMap / OCM 已有车规级 DC，但 coach 终点站匹配是单独问题）
- Battery degradation over a year — 需要先把 single-day → annual schedule 这一层接上

---

*Last reviewed: 2026-04-30. Edits to this prompt require separate PR, not bundled with code.*
