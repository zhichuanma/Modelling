# Bus Depot Curves — 代码方案（v1，未实现）

> 目标：仿照 `mobility/cars/station_curves.py` 的私家车产出形态，为 bus 仿真补一套
> **逐时刻车辆状态** + **depot 充电曲线**。本文只是方案，落地代码留待确认后再写。

---

## 1. 设计总览

### 1.1 终态产物（与 cars 对位）

| Cars 产物 | Bus 对应产物 | 说明 |
|---|---|---|
| `station_charging_curve_15min_{year}.parquet` | `bus_depot_curve_15min.parquet` | 每 depot × 15-min bin 的能量 / 平均功率 / 服务车数 / 充电会话数 |
| `station_summary_{year}.csv` | `bus_depot_summary.parquet` | 每 depot 年度总量 + 峰值时段 |
| `station_day_counts_{year}.parquet` | `bus_depot_day_counts.parquet` | 每 depot × 日：unique blocks、sessions |
| `station_metadata_{year}.json` | （并入 `bus_depot_lsoa_registry.parquet`） | 不单独出 JSON，所有静态属性挂在 depot registry |
| `private_car_trip_records.parquet` | `bus_trip_records.parquet` | 每段 trip 的执行细节，**默认输出**（bus 量级 ≲ cars） |
| `private_car_charging_events.parquet` | `bus_charging_events.parquet` | 每次停车 / 充电事件 |

### 1.2 与现有 bus 输出的关系

- **不替换、不修改** `outputs/bus_annual_per_block.parquet` / `bus_annual_load_profile.parquet`
- **不替换、不修改** 已有的 `outputs/depot_registry.parquet`（按 agency / TxC garage 的运营商粒度 depot，与新表正交）
- 新管线**后处理**地读 `outputs/all_blocks.parquet` 与 sim，产出独立目录 `outputs/bus_depot_curves_{year}/`

### 1.3 关键设计决策（已与用户确认）

| # | 决策 | 备注 |
|---|---|---|
| 1 | depot 主键 = **LSOA**（不用坐标当 id） | 同 depot 多 stop 自动归并；不需要聚类 |
| 2 | depot 仍**保留代表性 lat/lon** | 用归属该 depot 的 block 起点坐标加权均值推算 |
| 3 | block → depot 通过**第一段 trip 起点 LSOA**绑定 | operational signal，避开车辆登记数据的伦敦塌陷 |
| 4 | `MIN_BLOCKS_PER_DEPOT` 阈值 | **跑 EDA / CDF 后再定**，不预设默认 |
| 5 | low-confidence block（morning_lsoa ≠ night_lsoa）的充电归属 | **0.5 / 0.5 拆分**到 morning 和 night depot |
| 6 | trip records 是否输出 | **默认开**，与 cars 对齐（bus 量级远小于 cars） |
| 7 | multiprocessing | **暂不讨论**，等真要跑全量再敲定 |

---

## 2. 工作流四步

```
Step 1  Depot EDA            scripts/explore_bus_depots.py
        ↓
        敲定 MIN_BLOCKS_PER_DEPOT
        ↓
Step 2  生成 depot registry  mobility/bus/depot_lsoa_registry.py
        + block→depot 绑定
        ↓
Step 3  详细事件 + 曲线      mobility/bus/event_export.py
                             mobility/bus/depot_curves.py
        ↓
Step 4  CLI 入口             scripts/run_bus_depot_curves.py
        (multiproc 留位)
```

---

## 3. Step 1 — Depot EDA

### 3.1 目的

- 看 `n_blocks_per_lsoa` 的分布，从 CDF 上选 `MIN_BLOCKS_PER_DEPOT`
- 验证 stop→LSOA→depot 这套 operational 信号在伦敦不会塌陷
- 量化 morning_lsoa == night_lsoa 的占比（决定 low confidence 比例）

### 3.2 模块

`scripts/explore_bus_depots.py`（仅读 `outputs/all_blocks.parquet`，不依赖 sim）

### 3.3 算法（伪码）

```
blocks = read_parquet("outputs/all_blocks.parquet")

# 每个 block 取首尾两行
firsts = blocks.sort_values(["block_id","start_h"]).groupby("block_id").head(1)
lasts  = blocks.sort_values(["block_id","start_h"]).groupby("block_id").tail(1)

# 坐标 → LSOA（复用 mobility.core.spatial.query_lsoa_polygons）
firsts["morning_lsoa"] = resolve_lsoa(firsts.start_lat, firsts.start_lon)
lasts["night_lsoa"]    = resolve_lsoa(lasts.end_lat,   lasts.end_lon)

block_endpoints = firsts[["block_id","agency_id","morning_lsoa",
                          "start_lat","start_lon"]] \
                   .merge(lasts[["block_id","night_lsoa"]], on="block_id")

# 按 morning_lsoa 聚合
per_lsoa = block_endpoints.groupby("morning_lsoa").agg(
    n_blocks_morning = ("block_id","nunique"),
    n_agencies       = ("agency_id","nunique"),
    primary_agency   = ("agency_id", lambda s: s.value_counts().index[0]),
)

# 同 LSOA 同时是 night_lsoa 的计数
per_lsoa["n_blocks_night"] = block_endpoints.groupby("night_lsoa").size()
per_lsoa["n_round_trip"]   = (block_endpoints.morning_lsoa
                              == block_endpoints.night_lsoa).groupby(
                                  block_endpoints.morning_lsoa).sum()
```

### 3.4 输出物

目录 `outputs/diagnostics/bus_depot_eda/`：

| 文件 | 内容 |
|---|---|
| `lsoa_block_count.parquet` | 每 LSOA 的 `n_blocks_morning / night / total / round_trip / agencies_top3` 全表，未阈值化 |
| `lsoa_block_count_cdf.png` | x = `n_blocks_total`，y = LSOA 数（左轴）+ 累计 block 覆盖率（右轴）；x 标注几个候选阈值（1, 3, 5, 10） |
| `n_depots_by_lad.csv` | 每 LAD 在不同阈值下的 depot LSOA 数（伦敦/曼城/伯明翰 vs Cornwall / Powys 的对比） |
| `morning_eq_night_share.csv` | 全局 + 每 agency 的 round-trip 占比 |
| `unmatched_blocks_summary.csv` | 不同阈值下落的 block 量级 + 涉及 agency 数 |
| `eda_summary.md` | 我读完三张表 + 两张图后写的结论：建议 `MIN_BLOCKS_PER_DEPOT` 取值、low-confidence 占比、需不需要追加 EDA |

### 3.5 决策点

跑完 EDA → 一起读 `eda_summary.md` → 锁定 `MIN_BLOCKS_PER_DEPOT` → 进 Step 2。

---

## 4. Step 2 — Depot Registry + Block 绑定

### 4.1 模块

`mobility/bus/depot_lsoa_registry.py`（新增）

### 4.2 顶层接口（设计）

```python
def build_depot_lsoa_registry(
    blocks_df: pd.DataFrame,
    *,
    min_blocks_per_depot: int,        # 由 EDA 敲定
    high_confidence_min_blocks: int = 10,
    high_confidence_round_trip_share: float = 0.7,
    ev_lsoa_df: pd.DataFrame | None = None,   # 可选 cross-check
    lsoa_index: dict | None = None,           # 复用 query_lsoa_polygons
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
        depot_registry : 见 §4.4
        block_assignment : 见 §4.5
    """
```

### 4.3 LSOA 解析

复用 `mobility.core.spatial.query_lsoa_polygons` + `nearest_lsoa_for_points`（fallback ≤0.25 km），与 `depot_registry.py` 现有写法一致。

### 4.4 输出 `outputs/bus_depot_lsoa_registry.parquet`

| 列 | 类型 | 含义 |
|---|---|---|
| `depot_lsoa` | str | **主键**（e.g. E01003427） |
| `depot_lat` | float | block 起点坐标的加权均值（按 morning_count 加权） |
| `depot_lon` | float | 同上 |
| `lad` | str | 反查（用 LSOA → LAD 查找表） |
| `country` | str | E / S / W / N |
| `primary_agency_id` | str | 该 depot 关联 block 的 agency mode |
| `n_agencies` | int | distinct agency 数 |
| `agencies_top3` | str | `'OP10|OP202|OP55'` |
| `n_blocks_morning` | int | morning_lsoa 命中该 LSOA 的 block 数 |
| `n_blocks_night` | int | night_lsoa 命中 |
| `n_blocks_total` | int | union |
| `n_round_trip_blocks` | int | morning==night==此 LSOA 的 block 数 |
| `round_trip_share` | float | `n_round_trip / n_blocks_total` |
| `n_vehicles_ev_lsoa` | int | 来自 `EV_UK_LSOA_2025_with_energy.csv` 的 bus+minibus **synthetic allocated fleet** 计数（model-input cross-check，不代表 actual EV stock，不参与阈值） |
| `confidence` | str | `'high'` if `n_blocks_total ≥ high_confidence_min_blocks` AND `round_trip_share ≥ high_confidence_round_trip_share`；`'medium'` if `n_blocks_total ≥ min_blocks_per_depot`；其余在 Step 2 已经被过滤掉 |
| `depot_source` | str | `'gtfs_block_endpoint_lsoa'` |

`depot_lat/lon` 推算公式：

```
depot_lat = Σ_{block ∈ morning_lsoa=depot} block.start_lat / n_blocks_morning
depot_lon = Σ_{block ∈ morning_lsoa=depot} block.start_lon / n_blocks_morning
```

不是 LSOA 几何质心，而是**实际 block 起点的代表点**，更接近真实物理 depot。`depot_lsoa` 仍是规范主键。

### 4.5 输出 `outputs/bus_block_depot_assignment.parquet`

| 列 | 类型 | 含义 |
|---|---|---|
| `block_id` | str | |
| `agency_id` | str | |
| `morning_lsoa` | str | block 第一段 trip 起点所在 LSOA |
| `night_lsoa` | str | block 最后一段 trip 终点所在 LSOA |
| `primary_depot_lsoa` | str | 充电主归属 depot（= morning_lsoa 若在 registry，否则空） |
| `secondary_depot_lsoa` | str | 次归属（= night_lsoa，仅 low-confidence block 用） |
| `primary_share` | float | primary depot 分到的充电能量比例 |
| `secondary_share` | float | secondary depot 比例 |
| `block_depot_confidence` | str | `'high' / 'low' / 'unmatched'` |

绑定规则（与决策 #5 一致）：

```
case A  morning_lsoa == night_lsoa AND in registry:
    primary   = morning_lsoa
    secondary = ""
    primary_share = 1.0
    secondary_share = 0.0
    confidence = 'high'

case B  morning_lsoa != night_lsoa AND both in registry:
    primary   = morning_lsoa
    secondary = night_lsoa
    primary_share = 0.5
    secondary_share = 0.5
    confidence = 'low'

case C  仅 morning_lsoa 在 registry:
    primary = morning_lsoa, primary_share = 1.0
    secondary = "", secondary_share = 0.0
    confidence = 'low'                 (因为 night_lsoa 不在 registry，标低)

case D  仅 night_lsoa 在 registry:
    primary = night_lsoa, primary_share = 1.0
    secondary = ""
    confidence = 'low'

case E  两个 LSOA 都不在 registry:
    primary = ""
    confidence = 'unmatched'
    → 该 block 的充电事件**不进 depot 曲线**，仅出现在 charging_events 表
```

---

## 5. Step 3 — 详细事件 + Depot 15-min 曲线

### 5.1 模块

| 文件 | 职责 |
|---|---|
| `mobility/bus/event_export.py` | 把 `simulate_block_year` 返回的 `schedules` 展平成两张长表 |
| `mobility/bus/depot_curves.py` | events → 15-min bin → depot 聚合（镜像 `cars.station_curves.aggregate_station_curves_15min`） |

### 5.2 改动 `mobility/bus/annual_simulation.py`

仅在 `simulate_block_year` 增加返回字段：

```python
def simulate_block_year(..., keep_event_ledger: bool = False) -> dict:
    ...
    if keep_event_ledger:
        result["schedules"] = schedules     # 已经在内存里，只是不要丢
```

`simulate_fleet_year` 不修改 —— 详细产出走独立的 `run_bus_depot_curves.py` 调 `simulate_block_year(keep_event_ledger=True)` 一次性消费。

### 5.3 `event_export.py` 接口

```python
def build_trip_records(
    block_id: str,
    agency_id: str,
    depot_lsoa: str,
    schedules: list[DailySchedule],
) -> pd.DataFrame: ...

def build_charging_events(
    block_id: str,
    agency_id: str,
    primary_depot_lsoa: str,
    secondary_depot_lsoa: str,
    primary_share: float,
    secondary_share: float,
    schedules: list[DailySchedule],
) -> pd.DataFrame:
    """
    对每个 ParkingEvent 输出 1 行（high confidence）或 2 行（low confidence
    时按 primary/secondary 各拆一行，能量字段已乘相应 share）。
    """
```

**关键点（低 confidence 拆分）**：在 Step 3 落 `bus_charging_events.parquet` 时**直接把能量按 share 拆好**写两行 —— Step 4 的曲线聚合无需再判分支，行内已经有 `depot_lsoa` 和 `energy_charged_kwh = total × share`。

### 5.4 `bus_trip_records.parquet` 列定义

| 列 | 来源 | 备注 |
|---|---|---|
| `block_id` | – | |
| `agency_id` | – | |
| `depot_lsoa` | block_assignment.primary_depot_lsoa | 便于直接 groupby |
| `schedule_date` | `DailySchedule.date` | |
| `service_date` | trip 上挂的属性 | |
| `trip_id` | `Trip.trip_id` | |
| `trip_sequence_id` | 当日 trip 的 0-based 顺序 | |
| `route_id` | `Trip.route_id` | |
| `origin_lsoa / destination_lsoa` | `Trip.origin_lsoa / destination_lsoa` | |
| `origin_purpose / destination_purpose` | – | 'bus_stop' 居多 |
| `departure_h / arrival_h` | – | |
| `distance_km` | – | |
| `energy_consumed_kwh` | – | |
| `soc_before_trip / soc_after_trip` | `Trip.soc_before/after_trip` | sim 已经填好 |
| `is_deadhead / deadhead_class` | – | |

### 5.5 `bus_charging_events.parquet` 列定义

| 列 | 备注 |
|---|---|
| `block_id` | |
| `agency_id` | |
| `depot_lsoa` | low-conf 时一个 event 拆两行，分别填 primary / secondary |
| `share` | 0.5 / 0.5 或 1.0 / 0.0 |
| `schedule_date` | |
| `event_id` | `f"{block_id}__{schedule_date}__{idx}__{depot_lsoa}"` |
| `location_purpose` | `'depot_terminus'` / `'layover'` |
| `location_lsoa` | `ParkingEvent.location_lsoa`（原始记录，未经拆分） |
| `parking_start_h / parking_end_h / duration_hours` | |
| `can_charge` | bool |
| `charge_power_kw` | |
| `energy_charged_kwh` | **已乘 share**，即写入磁盘时就是拆好的值 |
| `soc_on_arrival / soc_on_departure` | |

### 5.6 `depot_curves.py` 接口

```python
def explode_events_to_15min_bins(
    charging_events: pd.DataFrame,
) -> pd.DataFrame:
    """
    每个 event 按 15-min 切片，能量按时间占比线性分摊。
    输出列: depot_lsoa, block_id, event_id, time_bin_start, time_bin_end,
            energy_kwh
    """

def aggregate_depot_curves_15min(
    bin_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    groupby (depot_lsoa, time_bin_start, time_bin_end) 求和。
    输出列见 §5.7。
    """
```

实现可大段复制 `cars.station_curves.aggregate_station_curves_15min` 的逻辑，仅替换 `station_id → depot_lsoa`、`vehicle_id → block_id`。

### 5.7 `bus_depot_curve_15min.parquet` 列定义

| 列 | 备注 |
|---|---|
| `depot_lsoa` | |
| `time_bin_start / time_bin_end` | 15-min bin 边界（datetime64） |
| `date` | `YYYY-MM-DD`（便于 partition pruning） |
| `energy_kwh` | bin 内全部 block 在此 depot 充电能量总和（low-conf 已拆分） |
| `avg_power_kw` | `energy_kwh / 0.25` |
| `active_block_count` | nunique block_id |
| `charging_session_count` | nunique event_id |

### 5.8 `bus_depot_summary.parquet` 列定义

| 列 | 备注 |
|---|---|
| `depot_lsoa` | |
| `annual_energy_kwh` | |
| `annual_session_count` | |
| `unique_blocks_served` | |
| `peak_avg_power_kw` | |
| `peak_time_bin_start` | |

### 5.9 `bus_depot_day_counts.parquet` 列定义

| 列 | 备注 |
|---|---|
| `depot_lsoa` | |
| `date` | |
| `unique_blocks` | |
| `total_sessions` | |
| `total_energy_kwh` | |

---

## 6. Step 4 — CLI 入口

### 6.1 脚本

`scripts/run_bus_depot_curves.py`（新增；不替代 `run_bus_annual.py`）

### 6.2 命令行参数

```
--blocks                  outputs/all_blocks.parquet
--depot-registry          outputs/bus_depot_lsoa_registry.parquet
--depot-assignment        outputs/bus_block_depot_assignment.parquet
--vehicle-params          (默认与 run_bus_annual 一致)
--warm-up-days            14
--start-date / --end-date (默认 = feed year)
--output-dir              outputs/bus_depot_curves_{year}/
--chunk-size              5000             # 内存上限控制
--workers                 1 (默认单进程，留接口)   ← multiproc 见 §7
--skip-trip-records       (反向开关：默认输出，加上后跳过)
--resume                  跳过已有 chunk parquet
--no-checkpoint           不写 chunk
--progress-interval       1
--seed                    42
```

### 6.3 主流程

```
1. load blocks + service_date_index + vehicle_params (复用 run_bus_annual 的 loader)
2. load depot_registry + block_depot_assignment
3. 切 chunk (block_id 列表均分)
4. for each chunk:
    a. for each block in chunk:
         result = simulate_block_year(..., keep_event_ledger=True)
         trip_rows     += build_trip_records(...)
         event_rows    += build_charging_events(...)  # 内含 low-conf 拆分
    b. bin_df = explode_events_to_15min_bins(event_rows)
    c. curve_chunk = aggregate_depot_curves_15min(bin_df)
    d. 写 chunks/chunk_{i:06d}_{trip_records,charging_events,depot_curve}.parquet
5. _combine_chunks → 最终 4 张 parquet
6. sanity check:
     Σ depot_curve_15min.energy_kwh
     == Σ charging_events.energy_kwh  (±1e-6)
     == Σ bus_annual_per_block.energy_charged_kwh（high-conf block 集合）
```

### 6.4 错误处理

- 单 block sim 抛异常 → 记 `failed_blocks.csv` (`block_id, error`)，其它继续
- chunk 写入失败 → 不更新 chunk index，下次 `--resume` 重做

---

## 7. Step 5 — Multiprocessing（占位，待真要跑全量再敲）

预留 `--workers` 参数。架构（不实现）：

- `ProcessPoolExecutor`，chunk 派发
- 大表（blocks_df / vehicle_params / depot_assignment）走磁盘传递（worker 自己 `read_parquet(filters=...)`），不走 pickle IPC
- RNG：`SeedSequence(master_seed).spawn(n_chunks)[chunk_id]`
- 每 worker 独立写 chunk parquet，主进程合并

具体阈值（workers / chunk_size）等真要跑前再调。

---

## 8. 实施顺序（落地时分 PR）

| PR | 内容 | 依赖 |
|---|---|---|
| PR-1 | `scripts/explore_bus_depots.py` + EDA 产物 + `eda_summary.md` | – |
| **（讨论）** | 一起读 EDA → 敲定 `MIN_BLOCKS_PER_DEPOT` | PR-1 |
| PR-2 | `mobility/bus/depot_lsoa_registry.py` + 单测 | PR-1 决议 |
| PR-3 | `mobility/bus/event_export.py` + `simulate_block_year(keep_event_ledger)` 改动 | PR-2 |
| PR-4 | `mobility/bus/depot_curves.py` | PR-3 |
| PR-5 | `scripts/run_bus_depot_curves.py`（单进程版） + smoke 测试 | PR-4 |
| **（讨论）** | 跑全量前商议 multiproc / 资源预算 | PR-5 |
| PR-6 | 加 multiproc + `--workers` | PR-5 |

---

## 9. 待办（写代码前的最后确认）

- [ ] EDA 输出物（§3.4）够不够？要不要再加图？
- [ ] depot registry 列（§4.4）有没有想增删的？
- [ ] block assignment 5 个 case（§4.5 case A–E）有没有遗漏？
- [ ] trip records / charging events 列定义（§5.4–5.5）够不够覆盖你的下游分析？
- [ ] CLI 参数（§6.2）默认值合适吗？

确认即开写 PR-1（EDA 脚本）。
