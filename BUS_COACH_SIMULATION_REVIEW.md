# BUS & COACH 仿真系统性复盘报告

> 生成日期：2026-06-02 · 范围：`mobility/bus/`、`mobility/coach/`、相关 `scripts/`、`tests/`、`data/`、`outputs/`、`docs/`
> 性质：**理解 / 梳理 / 诊断 / 提出讨论框架**，未对仿真代码做实质性修改（仅运行只读检查与轻量测试）。
> 配套验证：本轮已运行 `pytest tests/coach/`（37 passed）与 `tests/mobility/bus/test_lsoa_attribution.py + test_feasibility.py`（11 passed），并对 Scotland 地理编码版本做了数据层验证（见 §5）。

---

## 0. TL;DR（先读这一段）

1. **Bus 与 Coach 现在都已经实现"全年（feed-year）仿真"**。旧记忆里"coach 只有单程仿真"已过时——coach 在 `eb56941..5871ef4`（task 1–8 + fix 1–4）补齐了 TxC 日历、first-fit chain、feed-year SoC、LSOA 归属，已有 37 个 coach 测试通过。
2. **Bus 有两条并行管线**：M1 链式（`run_bus_pipeline.py`，含车辆指派 + SOC 级联求解）和 legacy 年度（`run_bus_annual.py`，全队 215k blocks 已跑过一次完整全年）。两者并存，M1 是更"真实"的方向但产物口径不同。
3. **苏格兰 Data Zone 版本不一致（已更正定级——见下方更正说明）**：该问题的**严重版本属于私家车**，且**已在 `mobility/cars/scotland_geography.py` + `geography_preflight.py` 修复**。底层数据事实（EV 清单 Scotland=DZ2011 `S01006506–S01013481`、ONSPD 质心/多边形=DZ2022 `S01013482+`、两区间不重叠、CSV 未重生成、bus/coach 直接读原始文件）对 bus/coach 仍存在，但**不构成头号风险**：bus 充电匹配是空间就近（非 exact lsoa_code），唯一全量年度产物来自不碰 per-LSOA 编码的 legacy 管线。残留暴露很窄，见 §5 R1（已更正）。
4. **核心抽象都偏"能跑通"而非"足够真实"的地方**：depot 是合成的（每 block 一个合成 depot 充电桩，或运营商质心虚拟 depot）；coach chain 是 first-fit 合成、非真实排班；公共充电桩（OCM/NCR）目前基本未接入 bus，coach 仅可选；LSOA "停车归属" 用的是 block/journey 终点 LSOA 的众数，是事后近似。
5. **测试覆盖不均**：年度仿真、cross-midnight、deadhead、LSOA attach、feasibility 有较好覆盖；但 **`charger_registry.py`、`depot_registry.py`、`chain_resolver.py` 几乎没有单元测试**，只在 M1 集成测试里被间接触及。

---

## 1. 仓库结构与入口梳理

### 1.1 顶层目录

| 路径 | 作用 |
|---|---|
| `mobility/core/` | 跨模式共享内核：`simulator.py`（`simulate_single_ev`）、`data_structures.py`（`DailySchedule`/`Trip`/`ParkingEvent`）、`constants.py`（时间步长、化学体系、季节系数）、`spatial.py`（LSOA 质心/多边形归属）、`txc_parser.py`（TxC XML 解析器） |
| `mobility/bus/` | Bus 仿真全部逻辑（~30 个 .py，见 §2） |
| `mobility/coach/` | Coach 仿真全部逻辑（~18 个 .py，见 §3） |
| `mobility/cars/` | 私家车（与 bus/coach 正交，本报告不展开） |
| `scripts/` | 各管线 CLI 入口（见 §1.2） |
| `tests/` | pytest 测试（见 §6） |
| `notebooks/` | 叙事 notebook（`_build_0X_*.py` 是其 python 源；`_prompts/` 是历次设计提示词存档） |
| `data/`（实际数据在仓库**上一级** `../Data/`） | 原始输入（GTFS、TxC、EV 清单、OCM 充电桩、边界/质心） |
| `outputs/` | 仿真产物（~50 个目录/文件，见 §2.10 / §3.7） |
| `docs/` | 设计与后续步骤文档（`bus_depot_curves_plan.md`、`bus_charging_next_steps.md`、`coach_annual_next_steps.md`） |

### 1.2 入口脚本（`scripts/`）及关系

| 脚本 | 角色 | 读 | 写 |
|---|---|---|---|
| **`run_bus_pipeline.py`** (43 KB) | **Bus M1 链式主管线**：block instance 展开 → 贪心车辆指派 → SOC 级联求解（L0–L4） | `all_blocks.parquet`、GTFS、TxC、EV-LSOA | `depot_registry/charger_registry/vehicles/block_instances/vehicle_assignments/vehicle_day_events/resolution_summary.parquet` + `m1_reconciliation_report.md` |
| **`run_bus_annual.py`** (11 KB) | **Bus legacy 年度管线**：每 block 展开整 feed-year，逐日 SOC | `all_blocks.parquet`、vehicle params CSV、GTFS 日历 | `bus_annual_per_block.parquet`、`bus_annual_load_profile.parquet` |
| `run_bus_feasibility_audit.py` | 一次性快速可行性抽样审计 | blocks | `bus_feasibility_audit.parquet` |
| `compare_legacy_blocks.py` | 回归校验：新旧 block parquet 比对 | 两个 block parquet | `inference_comparison.csv` |
| `explore_bus_depots.py` (15 KB, **新增未跟踪**) | depot curves 计划 Step 1 的 EDA | blocks | `outputs/diagnostics/bus_depot_eda/*`（CDF、阈值候选） |
| **`run_coach_annual_pipeline.py`** (9 KB) | **Coach 年度主管线**：journeys → date_index → first-fit chains → 年度 SOC | coach journeys/stop seq/fleet、可选 OCM | `coach_annual_per_chain.parquet`、`coach_annual_load_profile.parquet` |
| `run_coach_pipeline.py` | Coach v1 单程批量（年度管线的前身） | coach journeys/fleet | 单程 feasibility parquet |
| `merge_privatecar_station_curve_shards.py` | 私家车专用（与 bus/coach 无关） | — | — |

**关系小结**：
- Bus：`run_bus_pipeline.py`（M1，更真实，逐链求解）与 `run_bus_annual.py`（legacy，逐 block 年度）**口径不同、并存**。M1 引入了真实车辆清单、depot、充电桩匹配、deadhead；legacy 只做"每 block 一辆代表车 + terminus 充电"。**注意：旧记忆称 legacy 为生产、M1 为新方向；实际 215k blocks 的完整年度产物 (`bus_annual_per_block.parquet`) 来自 legacy 管线**——这是当前唯一的"全量年度结果"。
- Coach：`run_coach_annual_pipeline.py` 是主线，`run_coach_pipeline.py` 是单程前身。
- `explore_bus_depots.py` + `docs/bus_depot_curves_plan.md` 是**正在进行中的下一步工作**（给 bus 补一套 cars 式的"逐时刻车辆状态 + depot 充电曲线"，目前只完成 Step 1 EDA，Step 2–4 仅有设计稿）。

---

## 2. Bus 仿真流程梳理（数据 → 输出）

### 2.1 文字版 pipeline

```
原始输入                         构建                              年度仿真
─────────                       ──────                            ──────────
GTFS (stop_times/trips/         build_all_blocks.py
  shapes/calendar/...)    ─────► → all_blocks.parquet
                                  (native block_id 直接用;
                                   缺失的用 infer_blocks() 贪心拼)
                                        │
TxC garage XML  ──► txc_parser  ──┐     │
                                  ▼     ▼
                          depot_registry.build_depot_registry()
                          (Tier1 TxC garage / Tier2 external /
                           Tier3 运营商质心虚拟 depot)
                                  │
OCM 充电桩 CSV ──► charger_registry.build_charger_registry()
                  (depot 合成桩 + 公共桩≥50kW)
                                  │
EV_UK_LSOA CSV ─► vehicle_inventory.bridge_ev_lsoa_to_fleet()
                  (按 LSOA 质心就近挂到 depot)  ⚠ 苏格兰编码不匹配
                                  │
        ┌─────────────────────────┴──────────────────────────┐
        ▼ M1 链式 (run_bus_pipeline.py)                         ▼ legacy 年度 (run_bus_annual.py)
 block_instances.build_block_instances()              year_schedule.block_to_year_schedules()
        → vehicle_assignment.assign_vehicles_greedy()         (每 block × 每 active date → DailySchedule;
        → event_ledger.build_event_ledger()                   注入 deadhead; 挂 parking)
        → chain_resolver.resolve_chain()  (L0→L4 + 合成兜底)          │
        → resolution_summary.parquet                    annual_simulation.simulate_fleet_year()
                                                         → simulate_single_ev() 逐日 SOC
                                                         → feasibility.scan_block_infeasibility()
                                                         → bus_annual_per_block / load_profile
                                  │
                                  ▼
                 lsoa_attribution.chain_home_lsoa() / lsoa_view()
                 (用 block 终点 end_lsoa 的众数当"home LSOA")
```

### 2.2 原始数据来源与字段
- **GTFS**（timetable）：`stop_times.txt`（站序、到发时刻，含 >24:00）、`trips.txt`（trip↔block_id↔service_id↔route）、`shapes.txt`（线形，用于距离）、`stops.txt`（坐标）、`calendar.txt`+`calendar_dates.txt`（服务日历）。`build_all_blocks.py:stream_trip_summary()` 单遍流式读取，距离优先用 shape haversine，否则用站间 haversine 累加。
- **TxC garage XML**：`bus/txc_parser.py:parse_txc_garages()` 提取车库坐标（缺坐标时按邮编 geocode），按 NOC/运营商名匹配 GTFS agency。
- **EV 车辆清单**：`data/EV_UK_LSOA_2025_with_energy.csv`（1.58M 行；**每行=一辆车，`EV_ID` 唯一**）。关键列：`EV_ID, LSOA_code, Model, count, Energy_kWh, DC_Power_kW, AC_Power_kW, efficiency_wh_per_km, vehicle_subtype`。⚠ **`count` 不是逐行车辆数，而是该 `(LSOA, Model)` 组的车辆数、抄在每行上**（组内恒定，已验证 100%）。真实 EV bus = **6,222 辆**（≈50% 在伦敦，14.5% 在 Merton），coach = 201 辆。**严禁 `sum(count)`（=Σ组大小²=45,276，平方膨胀）或"按 count 展开"**。详见 §5 R9。
- **OCM 公共充电桩**：`UK_OCM_stations_labeled.csv`（27k 行，`StationID, lat, lon, TotalCapacity_kW, Bands, lsoa_code, ...`）。

### 2.3 block / trip / stop / depot / layover / deadhead 建模
- **block = 一辆车一天的运营任务**：native block 直接来自 GTFS `block_id`；缺失时 `block_inference.infer_blocks()` 按时空连续性贪心拼成 `INF_{agency}_{service}_{n}`（`block_inference.py`，deadhead 速度 30 km/h，`max_layover_h=4`、`max_shift_h=16`）。`build_block_templates()` 把每 block 折叠成一行（首/末站、起止时刻、距离和、trip 数）。`build_block_instances_from_templates()` 按日历展开成 `{date}_{block_id}_{seq:02d}` 的"车日实例"。
- **deadhead（空驶）**：`trip_chain_bus.py:_inject_deadhead_trips()`（`DEADHEAD_SHORT_KM=5`、`DEADHEAD_SPEED_KMH=30`）；M1 侧 `event_ledger.build_event_ledger()` 显式构造 depot→首站、block 间、末站→depot 的 deadhead 事件；空驶距离 = haversine×1.0（无路网绕行）。
- **layover（中途停留）**：`year_schedule._attach_parking()` 在 trip 间隙生成 `location_purpose="layover"` 的 ParkingEvent，`can_charge` 取决于 `allow_layover_charging` 且时长≥阈值。
- **depot/terminus**：见 2.5。

### 2.4 车辆参数分配
- M1：`vehicle_inventory.bridge_ev_lsoa_to_fleet()` 从 EV 清单取 `battery_kwh / consumption(=efficiency_wh_per_km/1000) / ac_charge_kw_max / dc_charge_kw_max`，`usable_soc_min=0.10 / max=0.95`，按 LSOA 质心就近挂 depot（≤30 km，优先同运营商）。
- legacy 年度：`vehicle_sampling.sample_bus_vehicle_specs()` 按 `stock_2025_q2` 权重有放回抽样。
- 默认兜底（`vehicle_assignment.py:79-84`）：battery 300 kWh、consumption 1.2 kWh/km、AC 100 kW、DC 150 kW。

### 2.5 何时行驶/停车，停车位置→LSOA
- 行驶：passenger_block + 各类 deadhead（`MOVEMENT_EVENTS`）按 `energy_kwh_proxy` 扣电。
- 停车：depot_parking（日首/日末）、terminal/layover。`year_schedule` 给每个 ParkingEvent 打 `location_lsoa`（depot 取 registry LSOA，layover/terminus 取 trip 终点 LSOA）。
- 停车→LSOA 的"home 归属"：`lsoa_attribution.chain_home_lsoa()` 用该链所有 block 的 `end_lsoa` **众数**（`mobility/bus/lsoa_attribution.py`，**新增未跟踪文件，尚未接入 `run_bus_pipeline.py`**）。

### 2.6 在哪能充电（station kind）
- `charger_registry.build_charger_registry()`：
  - **depot 桩**（合成）：每 depot 一个 `station_id=depot_{id}`，功率取该 depot 车辆 `ac_charge_kw_max` 中位数，默认 100 kW，AC。
  - **公共桩**：OCM 过滤 `≥50 kW` 且 band ∈ {Fast site, Rapid, Ultra-rapid}，DC。
- 匹配（`chain_resolver.query_charger_eligibility` / `_nearest_public_charger`）：proximity ≤200 m、dwell ≥10 min、功率 = min(桩功率, 车端 AC/DC)。

### 2.7 SOC / 电耗 / 充电 / 失败 / 可行性
- `chain_soc.chain_soc_walk()`：移动事件扣 `energy_kwh_proxy`；停车事件若 eligible 则 `min(功率×时长, 余量)` 充电；**不做 CV 曲线**（直接用车端最大功率）。
- `feasibility.shadow_soc_walk()`：用于审计，**应用 CV 曲线**（`soc<cv_threshold` 满功率，之后线性 derate）。CV 阈值 NMC 0.80 / LFP 0.88（`constants.py`）。
- `feasibility.scan_block_infeasibility()`：影子 SOC 不夹断求 shortfall，理由枚举：`single_trip_exceeds_battery / starts_below_min_required / depot_only_insufficient / midday_depletion`。
- M1 级联 `chain_resolver.resolve_chain()`：**L0** 仅 depot → **L1** +公共机会充电 → **L2** +车队内换大电池车 → **L3** +插入午间回 depot 充电 → **L4** 升级车+午间 → 兜底**合成车**（按缺口定电池容量，必成功）。

### 2.8 关键常量速查（文件:行）
| 量 | 值 | 位置 |
|---|---|---|
| 决策步长 / 步数 | 15 min / 96 步 | `core/constants.py:3,5` |
| SOC 安全裕度 | 0.05 | `core/constants.py:9` |
| CV 阈值 NMC/LFP | 0.80 / 0.88 | `core/constants.py:36` |
| 季节电耗系数 | 冬1.35/春1.0/夏1.10/秋1.0 | `core/constants.py:40-45` |
| deadhead 速度 | 30 km/h | `block_inference.py:19`、`event_ledger.py:129`、`chain_resolver.py:312` |
| depot 桩默认功率 | 100 kW | `charger_registry.py`（约199） |
| 公共桩最小功率 | 50 kW | `charger_registry.py:129` |
| 机会充电门槛 | dwell≥10min、≤200m | `chain_resolver.py:~129-176` |
| usable SOC | 0.10 / 0.95 | `vehicle_inventory.py`、`vehicle_assignment.py:83-84` |
| bus 默认电池/电耗 | 300 kWh / 1.2 kWh·km⁻¹ | `vehicle_assignment.py:79-80` |
| feed-year | 2026-04-17 → 2027-04-16 | `bus/calendar.py:15-16` |

### 2.9 vehicle subtype 过滤
- `vehicle_sampling.load_bus_vehicle_params()`：仅保留 `subtype ∈ {bus, minibus, unknown}`（默认），并要求 `stock>0 & battery>0 & consumption>0 & depot_charge_kw>0`（`vehicle_sampling.py:~104`）。
- `vehicle_inventory.bridge_ev_lsoa_to_fleet()`：过滤到 bus/minibus 行。
- ⚠ 默认允许 `unknown` 进入 bus 抽样——若 GenModel→subtype 查表覆盖不全，可能把误标车型混入（见 §5）。

### 2.10 主要输出
| 文件 | 含义 |
|---|---|
| `all_blocks.parquet` (1.67M) | GTFS 派生的 bus trip/block 表（含 native/inferred、cross-midnight） |
| `bus_annual_per_block.parquet` (215k) | **legacy 年度每 block 结果**（能耗/可行性/deadhead） |
| `bus_annual_load_profile.parquet` (35,040=365×96) | 全队逐 15min 负荷曲线 |
| `vehicles.parquet` (6,222) | M1 车辆登记 |
| `vehicle_assignments.parquet` (668k) | M1 block→车辆指派 |
| `block_instances.parquet` (14.6M) | M1 展开的车日实例 |
| `vehicle_day_events.parquet` (2.68M) | M1 事件账本 |
| `resolution_summary.parquet` | M1 每链求解结果（L0–L4、合成占比、机会充电、deadhead km） |
| `depot_registry.parquet` / `charger_registry.parquet` | depot / 充电桩登记 |
| `outputs/diagnostics/bus_depot_eda/*` | depot curves 计划 Step 1 EDA |

---

## 3. Coach 仿真流程梳理（与 bus 异同）

### 3.1 文字版 pipeline
```
TxC XML (TxCInventory17APR26.csv 列出) 
  ─► coach/data_loader.build_all_coach_tables()
       (coach/txc_parser 解析 vehicle journeys; distance.vehicle_journey_distance_km
        = 站间 haversine × 1.30 绕行因子)
  ─► all_coach_journeys.parquet (14,041) + all_coach_stop_sequences.parquet (107,723)
        │
EV_UK_LSOA CSV ─► coach_fleet.load_coach_fleet()  [vehicle_subtype=="coach"]
        │
stop_geometry.attach_lsoa_to_journeys()  [最近质心, ≤5km]  → start_lsoa/end_lsoa
        │
calendar.build_journey_date_index()  [解析 TxC OperatingProfile → 每 journey active dates]
        │
chain_builder.build_coach_chains()  [按 (operator, date) first-fit;
        transit_buffer_h=0.5, max_relocation_km=50; 非 SoC 感知, 非真实排班]
        │
annual_simulation.simulate_coach_fleet_year()
   └─ simulate_coach_chain_year(_with_retry)
        ├─ year_schedule.chain_to_year_schedules()  [feed-year 展开 + parking]
        ├─ coach_fleet.sample_coach_ev()  [按 count 权重抽 EV]
        ├─ simulate_single_ev()  [warm-up + 全年 SOC]
        └─ 可选：layover 充电重试（pass1 不可行且该链 LSOA 命中 OCM eligible 时）
        │
  ─► coach_annual_per_chain.parquet + coach_annual_load_profile.parquet
        │
lsoa_attribution.chain_home_lsoa() / lsoa_view()  [end_lsoa 众数 + gap_ratio]
```

### 3.2 数据来源与 subtype 识别
- 数据：`../Data/EV_behavior/Coach_Data/TxC-2.4/`（TransXChange 2.4 XML + `TxCInventory17APR26.csv`）。
- subtype：`coach_fleet.py:54` `raw.loc[subtype.eq("coach")]`，从 `EV_UK_LSOA_2025_with_energy.csv` 仅取 `vehicle_subtype=="coach"`，要求 `Energy_kWh>0 & efficiency_wh_per_km>0`，按 `count` 权重抽样。

### 3.3 trip/block/stop/depot/charging 实现状态——**已实现（年度）**
- **trip**：`trip_chain_coach.journey_to_daily_schedules()` 把一条 journey 转成 Trip + 前后 ParkingEvent，支持 cross-midnight 拆日（`start_h∈[0,24)`，`end_h` 可达 48）。
- **block/chain**：`chain_builder.build_coach_chains()` first-fit 合成链（按运营商+日期），`coach_chain_template_id = {operator}_{journey_set_hash[:10]}`（跨日复用），`coach_chain_id` 按日。
- **stop**：`stop_geometry.load_unified_stops()`（NaPTAN + 自定义），`attach_lsoa_to_journeys()` 最近质心 ≤5km。
- **depot/terminus**：`year_schedule._attach_chain_parking()` 在首尾插 `depot_terminus` dwell，默认 `terminus_charge_kw=50`、`pre_journey_dwell_h=6`。
- **charging**：终点桩（terminus）始终可充；公共桩 `charging_supply.load_coach_eligible_stations()`（OCM Rapid/Ultra-Rapid，≥50kW）→ layover 充电按 LSOA eligibility 可选（默认关）。

### 3.4 与 bus 的异同
| 维度 | Bus | Coach |
|---|---|---|
| 数据源 | GTFS | TransXChange XML |
| block | 真实 block_id（或推断） | **合成 first-fit chain（非真实排班）** |
| 距离 | shape / 站间 haversine | 站间 haversine × **1.30 绕行因子** |
| LSOA 归属 | 多边形优先 + 质心兜底 | **仅最近质心**（≤5km） |
| depot | TxC garage/运营商质心 三层 | **泛化 terminus（50kW）**，无真实 depot |
| 日历 | GTFS calendar | TxC OperatingProfile（+ 银行假日） |
| SOC 级联 | L0–L4 + 合成兜底 | 单遍 + 可选 layover 重试 |
| 复用 bus | — | 仅 `calendar.py` import bus 的 `FEED_YEAR_START/END` 作兜底 |

### 3.5 仍是占位/未实现（v1 非目标，见 `docs/coach_annual_next_steps.md`）
- 真实运营商车辆排班（现为 first-fit 合成）；跨链 SoC 不结转；公共充电桩 eligibility 仅可选未默认；无利用率/排队（ceiling = `terminus_kw×8760`）；无途中快充；LSOA 仅质心匹配（无多边形）。

### 3.6 coach 相比 bus 缺的关键环节
- 没有 bus 那样的"真实 depot registry / charger registry"——coach 的 depot 是抽象 terminus；
- 没有 SOC 级联求解（换车/午间回程/合成兜底），不可行链只能靠 layover 重试或直接标记 infeasible；
- LSOA 归属精度低于 bus（无多边形）。

### 3.7 主要输出
`all_coach_journeys.parquet` (14,041)、`all_coach_stop_sequences.parquet` (107,723)、`coach_annual_per_chain.parquet`、`coach_annual_load_profile.parquet`；目前 outputs 里多为 `coach_annual_smoke_*`（**仅 smoke 规模，未见全量年度产物**）。

---

## 4. 当前进展表

状态：✅已完成 / 🟡部分完成 / ⬜未完成 / ❓不确定 / 🔍需要验证

| 模块/功能 | Bus | Coach | 相关文件 | 已测试 | 主要问题/风险 |
|---|---|---|---|---|---|
| 原始数据解析 | ✅ | ✅ | `build_all_blocks.py` / `coach/data_loader.py`,`txc_parser.py` | 部分 | coach 距离含 1.30 绕行因子假设 |
| block/chain 构建 | ✅(native+推断) | ✅(first-fit 合成) | `block_inference.py`,`block_instances.py` / `coach/chain_builder.py` | ✅ | coach chain 非真实排班 |
| 日历展开 | ✅ | ✅ | `bus/calendar.py` / `coach/calendar.py` | ✅ | — |
| depot registry | 🟡(合成+虚拟) | ⬜(抽象 terminus) | `depot_registry.py` / — | ❌**无单测** | depot 非真实库存 |
| charger registry | 🟡(depot 合成+OCM) | 🟡(OCM 可选) | `charger_registry.py` / `coach/charging_supply.py` | ❌**bus 无单测**；coach 有 | 公共桩接入 bus 未默认 |
| 车辆清单/抽样 | ✅ | ✅ | `vehicle_inventory.py`,`vehicle_sampling.py` / `coach_fleet.py` | ✅ | **苏格兰 DZ 版本不匹配(§5)**；bus 默认放行 `unknown` subtype |
| 车辆指派(M1) | ✅(贪心) | ⬜ | `vehicle_assignment.py` | 🟡(仅集成) | 无单测 pool/合成逻辑 |
| 事件账本 | ✅ | (隐含于 schedule) | `event_ledger.py` | 🟡 | — |
| SOC 求解 | ✅(L0–L4+合成) | 🟡(单遍+重试) | `chain_resolver.py`,`chain_soc.py` / `coach/annual_simulation.py` | ❌**resolver 几乎无单测** | 合成兜底"必成功"会掩盖真实不可行 |
| 年度仿真 | ✅(legacy 全量) + ✅(M1 链式) | ✅(仅 smoke) | `bus/annual_simulation.py` / `coach/annual_simulation.py` | ✅ | 两条 bus 管线口径不同 |
| feasibility 审计 | ✅ | ✅(单程) | `bus/feasibility.py` / `coach/feasibility.py` | ✅ | chain_soc 不做 CV，feasibility 做——两处口径差异 |
| LSOA 归属 | 🟡(新模块未接入) | ✅ | `bus/lsoa_attribution.py`(**未跟踪/未接管线**) / `coach/lsoa_attribution.py` | ✅(新 bus 测试 11passed) | 用 end_lsoa 众数，事后近似 |
| depot 充电曲线 | ⬜(仅 Step1 EDA) | ⬜ | `docs/bus_depot_curves_plan.md`,`explore_bus_depots.py` | — | Step2–4 仅设计稿 |
| 全量年度产物 | ✅(bus_annual 215k) | ⬜(仅 smoke) | `outputs/*` | — | coach 未跑全量 |

---

## 5. 关键建模假设与风险诊断

### 🟡 R1（已更正定级：原写"🔴最高/破坏核心目标"为**夸大**，特此更正）— 苏格兰 Data Zone 版本不一致
> **更正说明**：初版报告把"苏格兰充电失败"这个**私家车** bug 的严重性错误平移到了 bus/coach。经核对：该问题的严重版本属于私家车（exact lsoa_code 匹配充电桩），根因见 `notebooks/_prompts/scotland_private_car_geography_fix_task_cn.md`，**已在 `mobility/cars/scotland_geography.py` + `mobility/cars/geography_preflight.py` 修复**（含 `SCOTLAND_DZ2011_RANGE=(6506,13481)`、`SCOTLAND_DZ2022_RANGE=(13482,20873)` 的版本判定与 best-fit 重映射）。bus/coach 并不会以同样方式失败。

- **数据事实（仍成立）**：EV 清单苏格兰码 `S01006506–S01013481` = **2011 DZ**；ONSPD `lsoa21` 苏格兰码 `S01013482–S01020873` = **2022 DZ**；多边形用 `SG_DataZone_Bdry_2022`。两区间不重叠；该 CSV 未重生成；bus/coach 直接读原始文件、未接 preflight、未引用 cars 的修复。
- **为何对 bus/coach 不是头号风险**：
  1. **bus 充电匹配是空间就近、不是 exact lsoa_code**：`chain_resolver._nearest_public_charger` 用 BallTree + haversine、≤200m（`chain_resolver.py:71-126`），depot 桩建在 depot 坐标上——"能否充电"不经过 lsoa_code。
  2. **唯一全量年度产物 (`bus_annual_per_block.parquet`) 免疫**：`run_bus_annual.py` 用 `load_bus_vehicle_params`（stock 权重抽**参数**），不做 EV→LSOA→depot 桥接。
- **残留的窄口径暴露（均非当前生产路径，需进一步核对）**：
  - **(a) Bus M1** `bridge_ev_lsoa_to_fleet`（`vehicle_inventory.py:145`）：EV `source_lsoa`(2011)↔质心(2022) 在苏格兰零命中→苏格兰车辆 NaN 经纬度→挂不上 depot。**影响取决于 M1 是否全量运行并使用此桥接，需对照真实 M1 run 确认。**
  - **(b) Coach** 可选 layover 充电：`annual_simulation.py:477` 用 exact 集合交 `chain_lsoas & eligible_layover_lsoas`，苏格兰会静默漏桩——但**默认关闭**，仅不可行链重试分支。
  - (c) 跨模式 LSOA 级报表对齐。
- **建议（降级为中等）**：若后续启用 bus M1 全量或 coach layover 充电，复用 cars 已有的 `scotland_geography` 重映射 + 加一个跨模式版本一致性护栏测试即可；当前 legacy 全量 bus 产物无需处理。

### 🟠 R2 — block 是否真实代表"一辆车一天"
- native block 较可靠；inferred block 是贪心拼接（`block_inference.py`），可能把不同实体车拼到一起或拆开同一辆车。`compare_legacy_blocks.py` 有回归校验但不验证"物理车辆"真实性。需看 inferred 占比（建议在 `05_bus_annual_results.ipynb` 里按 `block_source` 分布核对）。

### 🟠 R3 — 缺回程时的处理
- M1 `event_ledger` 显式构造 depot↔首末站 deadhead（haversine×1.0，30km/h，受 gap 时间上限约束）；`chain_resolver` 可插午间回程、合成兜底。**没有"真实回程缺失"的判定**——若数据本身缺回程，模型用 deadhead/合成补，可能低估真实运营里程/能耗。

### 🟠 R4 — depot 的 operational definition
- **三套不同定义并存**：(a) M1 `depot_registry` = TxC garage / 运营商质心虚拟 depot；(b) charger 的 depot 桩 = 每 depot 一个合成桩；(c) `docs/bus_depot_curves_plan.md` 计划改为 **depot 主键 = block 首站 LSOA**。三者口径需统一，否则"depot"含义在不同产物里不一致。

### 🟡 R5 — 停车→LSOA 可靠性
- 用 block/journey **终点 end_lsoa 的众数**当 home（`*/lsoa_attribution.py`）。对"回程闭合"的 block 合理，对开放链或夜间异地停放会偏。bus 用多边形+质心兜底（较好），coach 仅质心。

### 🟡 R6 — charger matching 地理版本
- **bus 侧不受影响**：bus 充电匹配是空间就近（≤200m），不经过 `lsoa_code`（见 R1 更正）。
- **仅 coach 可选 layover 路径相关**：`charging_supply` 用 OCM 自带 `lsoa_code` 聚合，coach 重试用 exact 交集；若 OCM 用 2022 而 coach end_lsoa 也是 2022（质心），二者一致——但 EV 清单（coach home）是 2011，跨表对齐仍需核。**需验证 `UK_OCM_stations_labeled.csv` 的 lsoa_code 版本**。

### 🟡 R7 — vehicle subtype 区分
- bus 抽样默认放行 `unknown`（`vehicle_sampling.py`），coach 严格 `=="coach"`。若 GenModel→subtype 查表覆盖不全，coach/minibus/van 可能以 `unknown` 混入 bus。**需核对 subtype 查表覆盖率与 `dropped_by_subtype` 统计**。

### 🟡 R8 — "为跑通"的假设清单
- depot 桩功率取车辆 AC 中位数、默认 100kW（合成）；公共桩对 bus 基本未接入；coach chain first-fit 合成；deadhead 用直线距离无路网；`chain_soc` 不做 CV 而 feasibility 做；合成兜底车"必成功"会把真实不可行掩盖成"可行"。

### 🟠 R9（2026-06-03 新增）— EV 清单 `count` 列语义被误解的连带风险
- **事实**（已验证）：`EV_UK_LSOA_2025_with_energy.csv` **每行=一辆车**（`EV_ID` 唯一）；`count` 是该 `(LSOA, Model)` 组的车辆数、被抄在组内每行（100% 组内恒定）。真实 bus 车队 = **6,222**，coach = **201**。`sum(count)=Σ组大小²=45,276` 是平方膨胀，**无意义**。
- **连带 bug 1（已确认）**：`mobility/coach/coach_fleet.py:sample_coach_ev(weight_by_count=True)`（默认）按 `count` 作抽样权重。由于 count=组大小、每行已是一辆车，这等于给每组施加 **组大小² 权重**，**系统性过采样大组**（应改为对行**等概率**抽样，或先去重到组再按真实台数加权）。
- **连带 bug 2（潜在）**：bus depot-only 重构方案 `notebooks/_prompts/bus_depot_only_sample_refactor_prompt_cn.md` §3.3 "按 count 展开为逐辆 vehicle instances" 会造 **45,276 个幽灵车**（大组平方放大，如 93 辆组→8,649）。正解：直接把每行当一辆车，**不要展开**。
- **影响**：之前一度把车队当成 45k、伦敦占比 77%/Merton 43%，均为 count² 假象；按车辆数(行数)真实占比为伦敦 ~50%、Merton 14.5%（见 §2.2）。

---

## 6. 测试与验证现状

### 6.1 已有测试（本轮实跑）
- `pytest tests/coach/` → **37 passed (11s)**：覆盖 feasibility、sim_adapter、trip_chain、selection、coach_fleet、lsoa_attribution、charging_supply、chain_builder、calendar、year_schedule、annual_simulation(+retry)、run_coach_(annual_)pipeline。
- `tests/mobility/bus/test_lsoa_attribution.py + test_feasibility.py` → **11 passed**。
- bus 测试总量：`tests/mobility/bus/` 收集到 **101 个**（未全跑，部分含真实数据较慢；`test_pipeline_integration_week.py` 标 slow）。

### 6.2 覆盖较好
年度 SOC 连续性、cross-midnight、deadhead 注入、车辆抽样、load matrix、LSOA attach（多边形+质心）、单 block/journey feasibility、calendar 边界。

### 6.3 缺失/薄弱（建议补单测）
| 模块 | 现状 | 缺什么 |
|---|---|---|
| `bus/charger_registry.py` | ❌ 几乎无单测 | depot 桩功率推断、station_kind、公共桩过滤、AC/DC 选择、距离阈值 |
| `bus/depot_registry.py` | ❌ 无单测 | 三层匹配、LSOA 归属法、confidence、TxC garage 解析 |
| `bus/chain_resolver.py` | ❌ 仅集成 | L0–L4 每级、合成兜底、午间回程、容量越界 |
| `bus/vehicle_assignment.py` | 🟡 仅集成 | pool 选择、合成 overflow 触发 |
| **跨模式地理版本** | ❌ 无 | **一个断言 EV 清单 LSOA 版本 == 质心/多边形版本 的护栏测试（针对 R1）** |

### 6.4 无法运行/未运行
- 未运行全量年度仿真（legacy 全量约 7.5h、M1 链式更重），符合本轮"只读/轻量"约束。
- ONSPD 路径在 `core/spatial.py` 写成 `PROJECT_ROOT/Data/Units/ONSPD_MAY_2025_UK.csv`，实际文件在仓库上一级 `../Data/Units/`——本轮用绝对相对路径验证通过，但**需确认 `PROJECT_ROOT` 解析是否在所有入口都正确**（否则质心加载会静默失败、退回空表）。

---

## 7. 面向后续讨论的方案框架（真实性优先）

### 方案 A — 先修地理编码地基（强烈建议先做）
- **思想**：统一全链路 LSOA/DZ 版本（推荐统一到 2021 LSOA + 2022 DZ），给 EV 清单加 2011→2022 DZ 映射；加一个"版本一致性"护栏测试。
- **需要数据**：苏格兰 2011→2022 DZ best-fit lookup（NRS 官方）；England 2011→2021 LSOA lookup。
- **改动模块**：`vehicle_inventory.py`、`coach_fleet.py`、`core/spatial.py`（或新增 `geography_version.py`）。
- **优点**：直接修复核心目标在苏格兰失效的问题；成本低、收益高。
- **风险**：lookup 是 best-fit，会引入轻微聚合误差。
- **对输出影响**：苏格兰车辆/充电归属从"基本错误"变"可用"。
- **适合先做原型**：✅ 最适合，几小时即可验证苏格兰命中率。

### 方案 B — 真实 depot / 充电基础设施替换合成层
- **思想**：用真实运营商 depot（DfT/CPT/OS）替换虚拟质心 depot，并把 OCM 公共桩正式接入 bus（现已在 coach 部分实现）。落地 `docs/bus_depot_curves_plan.md` 的 Step 2–4。
- **需要数据**：运营商 depot 清单、OCM eligibility 规则（如 ≥150kW DC）。
- **改动模块**：`depot_registry.py`、`charger_registry.py`、新增 `depot_lsoa_registry.py` / `event_export.py` / `depot_curves.py`。
- **优点**：充电地点真实化，产出 cars 式逐时刻 depot 曲线。
- **风险**：depot 数据可得性/质量；与现有 M1 口径需对齐。
- **对输出影响**：从"合成 depot 自洽"到"真实 depot 容量约束"。
- **适合先做原型**：🟡 可先做 Step 2 registry 原型。

### 方案 C — coach 真实排班 + 跨链 SoC + SOC 级联
- **思想**：用真实运营商 roster 替换 first-fit 合成链，引入跨链 SoC 结转，并把 bus 的 L0–L4 级联移植到 coach。
- **需要数据**：运营商 coach roster；公共快充网络。
- **改动模块**：`coach/chain_builder.py`、`coach/annual_simulation.py`、新增 coach resolver。
- **优点**：coach 与 bus 真实性对齐。
- **风险**：roster 数据难获取；工作量大。
- **对输出影响**：coach 从 smoke 升级为可发表全量。
- **适合先做原型**：⬜ 数据依赖重，宜在 A/B 之后。

---

## 8. 建议优先讨论/修改的 5 个问题

1. **【地理版本，已更正】**苏格兰 DZ2011/2022 的**严重版本是私家车问题、且已修复**（`cars/scotland_geography.py`）。bus/coach 仅有窄口径残留（M1 EV→depot 桥接、coach 可选 layover），均非当前生产路径。**优先级降为中等**：仅在启用 bus M1 全量或 coach layover 充电时，复用 cars 的重映射并加跨模式一致性护栏测试。（详见 §5 R1 更正）
2. **【两条 bus 管线如何收敛】**`run_bus_annual.py`(legacy 全量) 与 `run_bus_pipeline.py`(M1 链式) 口径不同，最终发表用哪条？是否把 M1 的 depot/充电桩/deadhead 真实性回灌 legacy，或反之？
3. **【depot 定义统一】**registry depot / charger depot 桩 / depot_curves 计划的"首站 LSOA depot" 三套定义需要敲定唯一口径（R4）。
4. **【合成兜底的诚实性】**`chain_resolver` 合成车"必成功"与 coach layover 重试，会把真实不可行掩盖；讨论是否保留"真实不可行"标记并在产出中区分。
5. **【关键模块补单测】**`charger_registry` / `depot_registry` / `chain_resolver` 目前几乎无单测（R 与 §6.3），在改动地理/depot 逻辑前先补护栏测试，避免回归。

---

### 附：关键文件索引
- 共享内核：`mobility/core/{constants,spatial,simulator,data_structures,txc_parser}.py`
- Bus：`mobility/bus/{build_all_blocks,block_inference,block_instances,depot_registry,charger_registry,vehicle_inventory,vehicle_sampling,vehicle_assignment,event_ledger,chain_resolver,chain_soc,feasibility,year_schedule,annual_simulation,lsoa_attribution,calendar,trip_chain_bus}.py`
- Coach：`mobility/coach/{data_loader,txc_parser,coach_fleet,chain_builder,trip_chain_coach,year_schedule,annual_simulation,charging_supply,feasibility,stop_geometry,lsoa_attribution,calendar,distance,sim_adapter,selection}.py`
- 脚本：`scripts/{run_bus_pipeline,run_bus_annual,run_coach_annual_pipeline,run_coach_pipeline,explore_bus_depots}.py`
- 文档：`docs/{bus_depot_curves_plan,bus_charging_next_steps,coach_annual_next_steps}.md`、`TASKS.md`、`COACH_ANNUAL_RESPONSE.md`、`CODE_REVIEW_RESPONSE.md`
- 产物：`outputs/{all_blocks,bus_annual_per_block,bus_annual_load_profile,vehicles,vehicle_assignments,resolution_summary,depot_registry,charger_registry,all_coach_journeys,coach_annual_*}.parquet`

> **不确定项需进一步核对**：(a) OCM `lsoa_code` 的 DZ/LSOA 版本；(b) `PROJECT_ROOT` 在各入口对 ONSPD 路径的解析是否一致；(c) inferred block 占比与物理车辆真实性；(d) subtype 查表覆盖率（unknown 占比）；(e) coach 是否已有全量（非 smoke）年度产物。
