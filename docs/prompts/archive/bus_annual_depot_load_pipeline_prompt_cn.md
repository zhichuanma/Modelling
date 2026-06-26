# Bus 全年 Depot-only 充电负荷仿真管线施工指令

## 0. 任务定位

请将当前 bus 仿真重构为一条 **全年 depot-only 充电负荷仿真管线**。

本任务的目标不是前端开发，也不是充电基础设施规划，而是生成后续地图/分析系统可以读取的后端数据：

1. 每个 depot / operational depot anchor 的位置、LSOA、来源和置信度；
2. 每辆电动公交在全年每个服务日的 event ledger；
3. 每个 depot 在全年每一天、每 15 分钟的 charging load；
4. 每个 depot 每日充电曲线摘要；
5. 每个 vehicle-day 的 SOC、能耗、充电和 infeasibility 诊断。

核心建模问题是：

> 基于英国 bus block 运行任务、GTFS service calendar 和现有 EV bus 参数分布，在 **只允许 depot charging、不考虑公共充电** 的假设下，全年每个 depot / operational depot anchor 在每个日期和每个 15 分钟时间槽会产生多少充电负荷？

---

## 1. 与旧方案的关系

### 1.1 与 legacy 年度管线的关系

当前仓库中可能已有 `run_bus_annual.py` legacy 年度仿真管线。该管线可以作为对照，但本任务不应继续以 legacy 为主线。

本任务应使用更清晰的 M1-style chain / event-ledger 思想：

```text
block template
    -> annual block instances
    -> depot registry / operational depot anchor
    -> vehicle/spec assignment
    -> vehicle-day event ledger
    -> depot-only SOC walk
    -> depot x date x 15min load aggregation
```

### 1.2 与原 M1 的区别

原 M1 包含：

```text
public chargers
OCM matching
opportunity charging
L0-L4 resolution cascade
强制所有 chain feasible
```

本任务明确不做：

```text
公共充电
OCM nearest charger search
terminal opportunity charging
L0-L4 resolution cascade
synthetic larger battery
强行 resolve infeasible cases
fleet sizing recommendation
前端开发
```

本任务只做：

```text
全年 bus block instances + depot-only charging + annual depot load curves
```

---

## 2. 最终输出目标

所有输出默认写入：

```text
outputs/bus_annual_depot_load/
```

必须输出：

```text
preflight_summary.json
preflight_summary.md
block_templates.parquet
block_templates_lsoa.parquet
block_instances_annual.parquet
depot_registry.parquet
depot_inference_diagnostics.parquet
ev_bus_specs.parquet
vehicle_day_assignments.parquet
vehicle_day_events.parquet
vehicle_day_soc_summary.parquet
depot_load_15min.parquet
depot_daily_summary.parquet
run_summary.md
```

其中最核心的两个分析产物是：

```text
depot_registry.parquet
    每个 depot / operational anchor 的位置、LSOA、来源、置信度

depot_load_15min.parquet
    每个 depot 在全年每一天、每 15 分钟的充电负荷曲线
```

---

## 3. 关键建模口径

### 3.1 Depot 的定义

本任务中的 depot 分为两类：

```text
physical_depot:
    来自 TxC Garage、postcode geocoding、curated depot list 等较高置信来源。

operational_depot_anchor:
    从 block terminal/end LSOA、闭环 block、terminal stop 坐标或 LSOA centroid 推断得到的运营充电锚点。
```

如果没有可靠真实 depot 数据，可以使用 operational depot anchor，但必须在字段中明确：

```text
depot_source
depot_confidence
is_physical_depot
is_operational_anchor
manual_review_flag
```

不要把 inferred operational anchor 命名为 `real_depot`、`true_depot` 或 `verified_garage`。

### 3.2 充电口径

只允许 depot charging。

允许充电的事件：

```text
depot_parking_pre
depot_parking_midday
depot_parking_post
depot_parking_overnight
```

不允许充电的事件：

```text
passenger_block
passenger_trip
terminal_layover
public_charger_event
opportunity_charging
OCM_station_event
```

默认 depot 充电功率：

```python
DEFAULT_DEPOT_POWER_KW = 100.0
```

车辆实际充电功率：

```python
effective_charge_kw = min(vehicle.ac_charge_kw_max, depot_power_kw)
```

`dc_charge_kw_max` 只保留用于审计，不参与 depot-only charging。

### 3.3 Infeasibility 口径

Depot-only infeasible 是有效模型结果，不是代码错误。

不要：

```text
强行插入公共充电
创造新 charger
创建更大 synthetic battery
把 SOC clamp 到 0
删除 infeasible vehicle-day
```

需要保留并报告：

```text
min_soc_kwh
min_soc_pct
energy_shortfall_kwh
depot_only_feasible
infeasibility_reason
```

### 3.4 年度口径

本任务是全年仿真，不是单日代表性样本。

必须基于 GTFS calendar / calendar_dates 将 block templates 展开为全年 active block instances。

推荐默认 feed-year 与现有 bus 模块保持一致。如果仓库已有 `mobility.bus.calendar.FEED_YEAR_START/END`，优先复用。

如果需要 CLI 参数，应支持：

```bash
--start-date YYYY-MM-DD
--end-date YYYY-MM-DD
```

---

# Stage 0 - Preflight 与输入事实确认

## 0.1 目标

在写核心年度仿真逻辑前，先确认输入数据形态、字段、EV bus 参数、地理数据和 calendar 可用性。

## 0.2 Block 输入事实

`outputs/all_blocks.parquet` 预期是 trip 级，而不是最终 block template 级。

Stage 0 必须确认：

```text
n_trip_rows
available_columns
has_block_id
has_agency_id
has_service_id
has_start_end_time
has_start_end_lat_lon
has_distance
```

不要假设原始 `all_blocks.parquet` 已经有 `start_lsoa/end_lsoa`。

## 0.3 EV inventory 输入事实

读取：

```text
data/EV_UK_LSOA_2025_with_energy.csv
```

或当前项目实际 EV inventory 路径。

只保留：

```python
vehicle_subtype in {"bus", "minibus"}
```

确认：

```text
EV_ID 是否唯一
每行是否代表一辆 EV bus instance
count 列是否仅用于审计
```

重要规则：

```text
每行 EV_ID = 一辆车或一个车辆参数记录。
不要按 count 展开。
不要 sum(count) 作为车辆数。
```

如果发现 `(LSOA, Model)` 组内 `count` 与组大小不一致，只作为数据质量警告，不按 count 展开。

## 0.4 EV 参数 sanity check

生成 `ev_bus_specs.parquet` 前必须检查：

```text
battery_kwh > 0
0.7 <= consumption_kwh_per_km <= 3.0
ac_charge_kw_max > 0
usable_soc_min < usable_soc_max
```

能耗转换：

```python
consumption_kwh_per_km = efficiency_wh_per_km / 1000.0
```

低于 0.7 或高于 3.0 的 rows 要剔除并写入 diagnostics。`run_summary.md` 必须报告剔除数量及异常范围。

## 0.5 输出

```text
preflight_summary.json
preflight_summary.md
```

至少包含：

```text
n_trip_rows
n_ev_rows_raw
n_ev_rows_bus_minibus
n_ev_specs_valid_after_sanity
n_ev_specs_dropped_by_sanity
min_consumption_kwh_per_km
max_consumption_kwh_per_km
count_column_interpretation
minibus_row_count
calendar_available
lsoa_attach_available
```

## 0.6 测试

新增：

```text
tests/mobility/bus/test_annual_depot_preflight.py
```

测试：

```text
test_preflight_detects_trip_level_blocks
test_preflight_does_not_require_lsoa_in_raw_all_blocks
test_preflight_filters_bus_minibus_only
test_preflight_does_not_expand_count
test_preflight_reports_sanity_drops
```

---

# Stage 1 - Trip-level all_blocks 聚合为 block templates

## 1.1 目标

将 trip 级 `all_blocks.parquet` 聚合为 block template 级表。

输出：

```text
block_templates.parquet
```

## 1.2 聚合键

优先按以下字段聚合：

```text
agency_id
service_id
block_id
block_source
```

如果仓库已有稳定函数，例如：

```python
mobility.bus.block_instances.build_block_templates
```

应优先复用，并只做必要适配。

## 1.3 block template 字段

`block_templates.parquet` 至少包含：

```text
block_template_id
agency_id
service_id
block_id
block_source
n_trips
start_h
end_h
start_time
end_time
duration_h
passenger_distance_km
start_stop
end_stop
start_lat
start_lon
end_lat
end_lon
trip_start_times
trip_end_times
trip_start_lats
trip_start_lons
trip_end_lats
trip_end_lons
trip_distances_km
```

如果部分 trip sequence 字段过大，可使用 list/array parquet 列或另写 trip-sequence 辅助表，但 event ledger 阶段必须能恢复 block 内 trip 顺序。

## 1.4 cross-midnight

GTFS 中可能存在超过 24:00 的时间，例如 `25:30:00`。

必须保留真实时序，不要简单截断到 24:00。

## 1.5 输出

```text
block_templates.parquet
block_template_build_diagnostics.parquet
```

## 1.6 测试

新增：

```text
tests/mobility/bus/test_block_template_building.py
```

测试：

```text
test_trip_rows_aggregate_to_block_templates
test_block_template_has_first_and_last_stop
test_block_template_preserves_trip_order
test_cross_midnight_times_are_preserved
```

---

# Stage 2 - LSOA attach 与 region_key 构建

## 2.1 目标

为 block template 的起点、终点及可用 terminal/trip endpoints 匹配 LSOA，并生成 GOR/country 级 `region_key`。

输出：

```text
block_templates_lsoa.parquet
```

## 2.2 LSOA attach

优先复用现有空间工具，例如：

```python
mobility.bus.data_loader.attach_lsoa
mobility.core.spatial
```

为以下坐标匹配 LSOA：

```text
start_lat/start_lon
end_lat/end_lon
trip endpoint coordinates, if available
```

输出字段至少包括：

```text
start_lsoa
end_lsoa
start_lsoa_method
end_lsoa_method
lsoa_attach_distance_m
manual_review_flag
```

## 2.3 region_key

`region_key` 必须使用 GOR/country 级，不要使用 LAD 级作为区域支配保护单位。

推荐生成规则：

```text
England: LSOA -> LAD -> Region / GOR
Scotland: Scotland
Wales: Wales
Northern Ireland: Northern Ireland
```

如果项目已有 ONSPD 或 LSOA lookup，可复用。否则新增一个清晰的 lookup loader，并在 preflight 中报告 lookup 路径和命中率。

输出字段：

```text
region_key
region_source
region_lookup_success
```

## 2.4 输出

```text
block_templates_lsoa.parquet
lsoa_region_diagnostics.parquet
```

## 2.5 测试

新增：

```text
tests/mobility/bus/test_block_lsoa_region.py
```

测试：

```text
test_lsoa_attach_adds_start_and_end_lsoa
test_region_key_is_gor_or_country_level
test_london_is_single_region_not_boroughs
test_region_lookup_reports_missing_values
```

---

# Stage 3 - 全年 block instances 展开

## 3.1 目标

根据 GTFS `calendar.txt` 和 `calendar_dates.txt` 将 block templates 展开成全年 active block instances。

输出：

```text
block_instances_annual.parquet
```

## 3.2 日历规则

使用 GTFS calendar 作为唯一服务日来源。

必须处理：

```text
calendar.txt weekly service pattern
calendar_dates.txt exception_type=1 add service
calendar_dates.txt exception_type=2 remove service
```

代码中需要写明：

```python
# ASSUMPTION: GTFS calendar_dates.txt correctly reflects bank-holiday service.
# No separate holiday scenario is modelled in this pipeline.
```

## 3.3 block_instance_id

每个 active block instance 必须有唯一 ID：

```python
block_instance_id = f"{service_date}_{block_template_id}_{seq:02d}"
```

如果同一日期同一 block template 出现重复，递增 `seq`。

## 3.4 输出字段

`block_instances_annual.parquet` 至少包含：

```text
service_date
block_instance_id
block_template_id
agency_id
service_id
block_id
block_source
start_datetime
end_datetime
start_h
end_h
duration_h
passenger_distance_km
start_stop
end_stop
start_lat
start_lon
end_lat
end_lon
start_lsoa
end_lsoa
region_key
```

## 3.5 输出

```text
block_instances_annual.parquet
calendar_expansion_diagnostics.parquet
```

## 3.6 测试

新增：

```text
tests/mobility/bus/test_annual_block_instances.py
```

测试：

```text
test_calendar_dates_adds_service
test_calendar_dates_removes_service
test_block_instance_ids_are_unique
test_cross_midnight_instances_keep_order
test_service_date_range_respected
```

---

# Stage 4 - Depot registry / Operational depot anchor 构建

## 4.1 目标

为每个 block template 或 agency/block anchor 构建 depot registry，使全年 event ledger 能将每个 block instance 映射到一个 depot_id 和 depot 坐标。

输出：

```text
depot_registry.parquet
depot_inference_diagnostics.parquet
```

## 4.2 depot 来源优先级

按以下优先级构建 depot：

```text
Tier 1: TxC Garage 坐标或高置信外部 depot list
Tier 2: TxC Garage postcode geocoding
Tier 3: agency-level operational anchor
Tier 4: block-level closed-loop anchor
Tier 5: block terminal/end LSOA mode anchor
Tier 6: LSOA centroid fallback
```

如果当前任务无法可靠接入 TxC Garage，可先实现 Tier 4-6，但字段结构必须支持未来接入 Tier 1-3。

## 4.3 block-level operational depot anchor

对每个 block template 收集候选 LSOA：

```text
start_lsoa
end_lsoa
trip_start_lsoa
trip_end_lsoa
terminal_lsoa
layover_lsoa
```

推断规则：

```text
high confidence:
    start_lsoa == end_lsoa，block 在 LSOA 层面闭合。

medium confidence:
    trip-level terminal/layover/end LSOA 有唯一众数，且不是单纯依赖最终 end_lsoa。

low confidence:
    只有 start_lsoa 和 end_lsoa，且二者不同，因此 operational_depot_lsoa 退化为 end_lsoa；
    或通过坐标 fallback 得到 LSOA。

missing:
    无法推断 LSOA。
```

tie-break：

```text
1. 优先最终 end_lsoa
2. 优先最长 dwell / terminal time 对应 LSOA
3. 字典序最小 LSOA，保证确定性
```

## 4.4 depot_id

如果是物理 depot：

```python
depot_id = f"depot_{source}_{operator_or_agency}_{stable_id}"
```

如果是 operational anchor：

```python
depot_id = f"opdepot_{agency_id}_{operational_depot_lsoa}"
```

## 4.5 depot 坐标

坐标优先级：

```text
1. physical depot lat/lon
2. geocoded garage postcode lat/lon
3. modal terminal/end stop coordinates median
4. LSOA centroid
5. NaN + manual_review_flag=True
```

## 4.6 限制说明

必须写入 `run_summary.md`：

```text
Operational depot anchors are inferred charging anchors, not verified physical garage locations.
When depot is inferred from end_lsoa, depot-to-route and route-to-depot deadhead may be biased.
For non-closed blocks where depot=end_lsoa, morning deadhead approximates end->start while evening return deadhead may be near zero.
This can underestimate or distort true garage deadhead energy.
```

## 4.7 输出字段

`depot_registry.parquet` 至少包含：

```text
depot_id
agency_id
depot_lat
depot_lon
depot_lsoa
depot_source
depot_confidence
is_physical_depot
is_operational_anchor
source_block_template_count
source_block_instance_count
manual_review_flag
limitation_note
```

`block_instances_annual.parquet` 或一个 join table 必须能将每个 block instance 映射到 depot_id。

## 4.8 测试

新增：

```text
tests/mobility/bus/test_annual_depot_registry.py
```

测试：

```text
test_closed_loop_block_gets_high_confidence_anchor
test_end_lsoa_fallback_gets_low_confidence
test_depot_id_includes_agency_and_lsoa_for_operational_anchor
test_depot_registry_has_lat_lon_lsoa_confidence
test_missing_depot_sets_manual_review_flag
```

---

# Stage 5 - EV bus spec pool 构建

## 5.1 目标

从 EV inventory 构建电动公交参数池。

输出：

```text
ev_bus_specs.parquet
```

## 5.2 参数字段

`ev_bus_specs.parquet` 至少包含：

```text
vehicle_spec_id
source_ev_id
vehicle_model
vehicle_subtype
source_lsoa
battery_kwh
consumption_kwh_per_km
ac_charge_kw_max
dc_charge_kw_max
usable_soc_min
usable_soc_max
source_row_id
spec_weight
```

默认：

```python
usable_soc_min = 0.10
usable_soc_max = 0.95
```

## 5.3 两种年度仿真模式

本 pipeline 必须支持至少一种模式。建议先实现 `scenario_mode="ev_stock_scale"`，并保留扩展字段支持未来模式。

### 模式 A：ev_stock_scale（推荐先实现）

含义：

```text
使用现有 EV bus inventory 的车辆数量规模。
每天从 active block instances 中抽取/分配与 EV bus spec 数量相当的 duties。
结果代表当前 EV stock 规模下的 representative depot-only annual load。
```

特点：

```text
不会模拟全英国所有 bus block 完全电动化；
不会把 EV source_lsoa 用作默认地理分配；
vehicle specs 是参数捐赠者。
```

### 模式 B：full_electrification_scenario（可延期）

含义：

```text
所有 active bus block instances 都被视为电动化。
EV inventory 只作为参数分布，可重复抽样 EV specs。
结果代表全英国 bus block 完全电动化情景。
```

本任务如果时间有限，可以只实现模式 A，但代码结构不要阻止未来添加模式 B。

## 5.4 测试

新增：

```text
tests/mobility/bus/test_ev_bus_specs.py
```

测试：

```text
test_ev_specs_filter_bus_minibus_only
test_ev_specs_do_not_expand_count
test_consumption_unit_conversion
test_invalid_specs_are_dropped
test_spec_fields_present
```

---

# Stage 6 - 年度 vehicle-day assignment

## 6.1 目标

为每个 service_date 生成 vehicle-day assignments。

输出：

```text
vehicle_day_assignments.parquet
```

## 6.2 默认模式：ev_stock_scale

对每个 service_date：

1. 取当天 active block instances；
2. 根据 region_key、distance_bin、duration_bin 按真实 duty 分布比例抽样 duties；
3. 抽样数量等于 `n_valid_ev_bus_specs`，如果当天 active block instances 更少，则使用全部；
4. 将 EV specs 以确定性 shuffle 后一对一分配给 sampled block instances；
5. 默认不使用 vehicle source_lsoa 匹配 block，避免登记地理偏差进入 depot load；
6. 每个 EV spec 每天最多使用一次；
7. 同一天每个 sampled block instance 默认只接收一个 EV spec。

这不是完整 fleet scheduling，不要求同一辆车跨天连续跟踪同一 depot。它是现有 EV stock 规模下的 representative annual duty assignment。

## 6.3 抽样权重

`sample_weight` 只用于诊断和可选外推，不默认用于 `depot_load_15min` 主结果。

默认输出的 `depot_load_15min` 是：

```text
ev_stock_scale unweighted scenario load
```

即：当前 EV stock 数量规模下的代表性年度 depot-only 负荷。

如果未来添加全国全量外推模式，应显式使用：

```bash
--weighting-mode national_block_expansion
```

并定义：

```python
sample_weight = n_available_in_stratum / n_sampled_in_stratum
```

当前任务默认不启用该外推模式。

## 6.4 输出字段

`vehicle_day_assignments.parquet` 至少包含：

```text
service_date
vehicle_day_id
vehicle_spec_id
block_instance_id
block_template_id
agency_id
service_id
block_id
depot_id
region_key
assignment_method
scenario_mode
sample_weight
```

## 6.5 确定性

使用固定种子：

```python
RNG_SEED = 20260603
```

每个日期的 shuffle 必须稳定，例如：

```python
daily_seed = hash((RNG_SEED, service_date))
```

不要使用 Python 默认不稳定 hash；使用稳定 hash 函数，例如 md5/sha1 派生整数。

## 6.6 测试

新增：

```text
tests/mobility/bus/test_vehicle_day_assignment.py
```

测试：

```text
test_daily_assignment_count_matches_ev_specs_when_enough_blocks
test_each_vehicle_spec_used_once_per_day
test_each_block_instance_used_once_in_default_mode
test_assignment_does_not_use_vehicle_source_lsoa_by_default
test_assignment_is_deterministic_by_date
```

---

# Stage 7 - 全年 depot-only event ledger

## 7.1 目标

为每个 vehicle-day assignment 构建 event ledger。

输出：

```text
vehicle_day_events.parquet
```

## 7.2 最小事件序列

每个 vehicle-day 至少包含：

```text
depot_parking_pre
depot_to_block_deadhead
passenger_block 或 passenger_trip 序列
block_to_depot_deadhead
depot_parking_post / depot_parking_overnight
```

如果 block 内 trip-level 信息可用，推荐生成 `passenger_trip` 序列，而不是单一 `passenger_block`。

## 7.3 日间 depot charging window

如果 block 内 trip-level 信息可用，并且中间 layover 满足：

```text
layover_lsoa == depot_lsoa
duration_min >= 30
```

则插入：

```text
depot_parking_midday
```

该事件允许 depot charging。

如果只有 aggregate block 信息，无法识别中间回 depot，则不插入日间 depot charging window，并在 `run_summary.md` 中说明。

## 7.4 跨午夜过夜充电

`depot_parking_post` 不得硬截断到当天 24:00。

规则：

```text
return_to_depot_time -> next_service_start_time if known
```

如果不知道下一次出车时间，使用默认过夜窗口：

```text
return_to_depot_time -> next day 06:00
```

该默认值应可通过 CLI 配置：

```bash
--default-overnight-end-hour 6
```

15 分钟负荷聚合必须允许跨午夜，并把能量记到真实 slot datetime。

## 7.5 deadhead

使用 haversine：

```text
distance_method = "haversine_x_1.0"
deadhead_speed_kmh = 30.0
```

必须包含：

```text
depot -> block start
block end -> depot
```

如果 depot 是 operational anchor 而非真实 garage，deadhead 是近似值，必须在 `run_summary.md` 说明。

## 7.6 禁止事件

不得出现：

```text
public_charger_event
opportunity_charging
terminal_public_charging
OCM_station_event
```

## 7.7 输出字段

`vehicle_day_events.parquet` 至少包含：

```text
service_date
vehicle_day_id
vehicle_spec_id
block_instance_id
block_template_id
agency_id
block_id
depot_id
depot_lsoa
event_seq
event_type
start_datetime
end_datetime
duration_min
start_lat
start_lon
end_lat
end_lon
start_lsoa
end_lsoa
distance_km
distance_method
energy_kwh
can_charge
charge_power_kw
charge_kwh_added
soc_start_kwh
soc_end_kwh
```

Stage 7 可以先生成未填 SOC/charge 的 ledger，Stage 8 再补齐。

## 7.8 测试

新增：

```text
tests/mobility/bus/test_annual_depot_events.py
```

测试：

```text
test_every_vehicle_day_has_events
test_event_sequence_starts_and_ends_at_depot
test_overnight_charging_can_cross_midnight
test_midday_depot_window_inserted_when_layover_at_depot
test_no_public_charging_events_created
```

---

# Stage 8 - 全年 depot-only SOC walk

## 8.1 目标

对每个 vehicle-day event ledger 执行 SOC walk，计算充电、最低 SOC、shortfall 和 feasibility。

输出：

```text
vehicle_day_events.parquet
vehicle_day_soc_summary.parquet
```

## 8.2 初始 SOC

默认：

```python
initial_soc_kwh = battery_kwh * usable_soc_max
```

当前版本不做跨日 SOC 结转；每个 vehicle-day 独立从 `usable_soc_max` 开始。

该限制必须写入 `run_summary.md`：

```text
This pipeline does not model multi-day SOC carry-over. Each vehicle-day starts at usable_soc_max.
```

## 8.3 movement events 扣电

movement events：

```text
depot_to_block_deadhead
passenger_block
passenger_trip
block_to_depot_deadhead
```

扣电：

```python
energy_kwh = distance_km * consumption_kwh_per_km
soc_end = soc_start - energy_kwh
```

## 8.4 depot charging

允许充电：

```text
depot_parking_pre
depot_parking_midday
depot_parking_post
depot_parking_overnight
```

不允许充电：

```text
terminal_layover
public_charger_event
passenger events
```

充电功率：

```python
effective_charge_kw = min(vehicle.ac_charge_kw_max, depot_power_kw)
```

默认：

```python
depot_power_kw = 100.0
```

充电量：

```python
available_capacity_kwh = battery_kwh * usable_soc_max - soc_start
charge_kwh_added = min(
    effective_charge_kw * duration_h,
    available_capacity_kwh,
)
soc_end = soc_start + charge_kwh_added
```

## 8.5 不 clamp SOC

不要把 SOC 强行限制在 0 以上。

负 SOC 是诊断信号。

输出：

```text
min_soc_kwh
min_soc_pct
energy_shortfall_kwh
depot_only_feasible
breaches_zero_soc
breaches_usable_min_soc
```

定义：

```python
energy_shortfall_kwh = max(0, -min_soc_kwh)
depot_only_feasible = min_soc_kwh >= battery_kwh * usable_soc_min
```

## 8.6 infeasibility reason

建议 reason：

```text
single_trip_exceeds_usable_battery
daily_energy_exceeds_usable_battery
midday_depletion_no_depot_window
insufficient_depot_charging_time
missing_depot_lsoa
invalid_vehicle_parameters
unknown
```

注意：

```text
midday_depletion_no_depot_window 只有在 trip-level event ledger 可判断中间无 depot window 时使用。
insufficient_depot_charging_time 只有存在 depot parking window 但时长不足时使用。
```

## 8.7 输出字段

`vehicle_day_soc_summary.parquet` 至少包含：

```text
service_date
vehicle_day_id
vehicle_spec_id
block_instance_id
block_template_id
depot_id
depot_lsoa
battery_kwh
consumption_kwh_per_km
ac_charge_kw_max
depot_power_kw
total_passenger_km
total_deadhead_km
total_energy_kwh
total_charge_kwh
min_soc_kwh
min_soc_pct
energy_shortfall_kwh
depot_only_feasible
infeasibility_reason
```

## 8.8 测试

新增：

```text
tests/mobility/bus/test_annual_depot_soc.py
```

测试：

```text
test_depot_charging_uses_ac_power
test_no_public_charging_used
test_soc_not_clamped_below_zero
test_overnight_charging_adds_energy_after_midnight
test_infeasible_vehicle_day_reports_shortfall
test_vehicle_day_soc_summary_has_required_columns
```

---

# Stage 9 - Depot x date x 15min load aggregation

## 9.1 目标

将所有 depot charging events 聚合为：

```text
depot_id x service_date x 15-minute time slot
```

输出：

```text
depot_load_15min.parquet
depot_daily_summary.parquet
```

## 9.2 聚合规则

只聚合 event_type 属于以下类型的充电：

```text
depot_parking_pre
depot_parking_midday
depot_parking_post
depot_parking_overnight
```

不聚合：

```text
public charger
OCM station
terminal public charging
```

如果充电事件跨越多个 15 分钟槽，应按时间重叠比例分摊 energy。

例如：

```text
18:10-18:40 的 30 分钟 charging event
应分配到 18:00-18:15、18:15-18:30、18:30-18:45 三个 slot，按重叠分钟数分摊。
```

## 9.3 跨午夜处理

如果 charging event 跨午夜，必须分配到真实 calendar datetime slots。

例如：

```text
service_date = 2026-04-17
charging event = 2026-04-17 23:30 -> 2026-04-18 06:00
```

则 slot_start_datetime 应包含：

```text
2026-04-17 23:30
...
2026-04-18 05:45
```

同时保留：

```text
service_date
slot_date
```

其中：

```text
service_date = block 运营日
slot_date = 实际电网负荷发生日期
```

## 9.4 depot_load_15min 字段

`depot_load_15min.parquet` 至少包含：

```text
depot_id
depot_lsoa
depot_lat
depot_lon
depot_confidence
service_date
slot_date
slot_index
slot_start_datetime
slot_end_datetime
charge_kwh
average_kw
n_charging_vehicles
scenario_mode
```

## 9.5 depot_daily_summary 字段

`depot_daily_summary.parquet` 至少包含：

```text
depot_id
depot_lsoa
depot_lat
depot_lon
depot_confidence
service_date
slot_date
daily_charge_kwh
daily_peak_kw
n_vehicle_days
n_charging_vehicles
n_infeasible_vehicle_days
share_infeasible_vehicle_days
```

## 9.6 能量一致性

必须验证：

```text
sum(depot_load_15min.charge_kwh)
≈ sum(vehicle_day_events.charge_kwh_added for depot charging events)
```

允许极小浮点误差。

## 9.7 测试

新增：

```text
tests/mobility/bus/test_depot_load_aggregation.py
```

测试：

```text
test_15min_load_only_uses_depot_charging_events
test_charging_event_split_across_slots_by_overlap
test_cross_midnight_slots_have_correct_slot_date
test_depot_load_energy_matches_event_ledger
test_depot_daily_summary_has_required_columns
```

---

# Stage 10 - Run summary 与诊断

## 10.1 目标

生成可读的年度仿真总结。

输出：

```text
run_summary.md
```

## 10.2 必须包含内容

`run_summary.md` 至少包括：

```text
n_trip_rows_input
n_block_templates
n_block_instances_annual
n_ev_specs_valid
scenario_mode
feed_year_start
feed_year_end
n_vehicle_day_assignments
n_depots
n_physical_depots
n_operational_anchors
depot_confidence_distribution
total_charge_kwh
total_energy_kwh
total_deadhead_km
depot_only_feasible_share
top_10_depots_by_annual_charge_kwh
top_10_depots_by_peak_kw
top_10_blocks_by_energy_shortfall
region_distribution_of_assigned_block_instances
lsoa_attach_success_rate
minibus_count
sanity_filter_drop_count
主要建模假设
主要限制
```

## 10.3 必须写明的限制

必须写入以下限制：

```text
1. 本模型只允许 depot charging，不考虑公共充电和 opportunity charging。
2. inferred operational depot anchors 不是经过验证的真实物理车库。
3. depot=end_lsoa 的非闭环 block 可能导致 deadhead 方向和距离偏差。
4. 当前版本不做多日 SOC 结转，每个 vehicle-day 默认从 usable_soc_max 开始。
5. ev_stock_scale 模式代表现有 EV stock 规模下的 representative annual duty assignment，不代表全英国所有 bus block 完全电动化。
6. 若 trip-level layover 不可用，中午回 depot 充电窗口可能无法识别。
```

## 10.4 测试

新增：

```text
tests/mobility/bus/test_annual_depot_run_summary.py
```

测试：

```text
test_run_summary_is_written
test_run_summary_contains_key_counts
test_run_summary_contains_depot_limitations
test_run_summary_states_no_public_charging
test_run_summary_states_no_multiday_soc_carryover
```

---

# 11. 新增脚本

新增脚本：

```text
scripts/run_bus_annual_depot_load.py
```

建议 CLI：

```bash
python scripts/run_bus_annual_depot_load.py \
  --blocks outputs/all_blocks.parquet \
  --ev-inventory data/EV_UK_LSOA_2025_with_energy.csv \
  --out-dir outputs/bus_annual_depot_load \
  --scenario-mode ev_stock_scale \
  --charging-mode depot_only \
  --depot-power-kw 100 \
  --default-overnight-end-hour 6 \
  --seed 20260603
```

可选参数：

```bash
--start-date YYYY-MM-DD
--end-date YYYY-MM-DD
--max-days N                         # smoke/debug use only
--max-vehicle-days N                 # smoke/debug use only
--use-trip-level-events true/false
--enable-physical-depot-sources true/false
```

不要在 notebook 中写 parquet/csv。

---

# 12. 推荐新增模块

建议新增模块，避免污染 legacy 和原 M1 代码：

```text
mobility/bus/annual_depot_preflight.py
mobility/bus/annual_block_templates.py
mobility/bus/annual_lsoa_region.py
mobility/bus/annual_block_instances.py
mobility/bus/annual_depot_registry.py
mobility/bus/annual_ev_specs.py
mobility/bus/annual_vehicle_day_assignment.py
mobility/bus/annual_depot_events.py
mobility/bus/annual_depot_soc.py
mobility/bus/annual_depot_load.py
mobility/bus/annual_depot_outputs.py
```

如果复用已有模块，必须保证：

```text
不会触发 public charging
不会触发 OCM matching
不会触发原 M1 L0-L4 cascade
不会调用 coach/cars 模块
不会破坏 legacy bus 管线
```

---

# 13. 总输出清单

所有输出写入：

```text
outputs/bus_annual_depot_load/
```

必须输出：

```text
preflight_summary.json
preflight_summary.md
block_templates.parquet
block_template_build_diagnostics.parquet
block_templates_lsoa.parquet
lsoa_region_diagnostics.parquet
block_instances_annual.parquet
calendar_expansion_diagnostics.parquet
depot_registry.parquet
depot_inference_diagnostics.parquet
ev_bus_specs.parquet
vehicle_day_assignments.parquet
vehicle_day_events.parquet
vehicle_day_soc_summary.parquet
depot_load_15min.parquet
depot_daily_summary.parquet
run_summary.md
```

---

# 14. 明确不要做的事情

本任务不要实现：

```text
前端页面
地图交互
API 服务
公共充电
OCM charger matching
nearest public charger search
opportunity charging
新 charger site
fleet sizing recommendation
min-cost flow assignment
coach 仿真修改
private car 仿真修改
OSRM / 路网绕行
温度修正
随机延误传播
Monte Carlo timetable delay
原 M1 L0-L4 resolution cascade
```

---

# 15. Acceptance Criteria

任务完成标准：

1. 新脚本 `scripts/run_bus_annual_depot_load.py` 可以端到端运行。
2. `all_blocks.parquet` trip 级输入被正确聚合为 `block_templates.parquet`。
3. block templates 成功 attach start/end LSOA，并生成 GOR/country 级 `region_key`。
4. block templates 被 GTFS calendar 展开为全年 `block_instances_annual.parquet`。
5. 每个 annual block instance 能映射到一个 depot_id，或被明确标记 missing/manual_review。
6. `depot_registry.parquet` 包含 depot 位置、LSOA、source、confidence。
7. EV inventory 不按 `count` 展开。
8. EV specs 参数 sanity check 生效，并报告剔除数量。
9. 默认 `scenario_mode=ev_stock_scale` 下，每个 service_date 分配一组 representative active duties 给 EV specs。
10. 默认不使用 vehicle source_lsoa 匹配 block。
11. 每个 vehicle-day assignment 都有 event ledger。
12. event ledger 不包含 public charging / OCM / opportunity charging 事件。
13. overnight depot charging 可以跨午夜。
14. 若 trip-level layover 显示车辆在 depot_lsoa 停靠足够久，则可插入 midday depot charging window。
15. SOC walk 使用 AC power 进行 depot charging。
16. SOC 不 clamp 到 0。
17. infeasible vehicle-days 被保留并报告 shortfall/reason。
18. `depot_load_15min.parquet` 包含 depot_id、位置、service_date、slot_start_datetime、charge_kwh、average_kw。
19. 跨午夜 charging events 被正确分配到实际 slot_date。
20. `depot_load_15min` 总能量与 event ledger depot charging 总能量一致。
21. `depot_daily_summary.parquet` 成功生成。
22. `run_summary.md` 成功生成并包含关键假设和限制。
23. 不破坏现有 bus/coach/car 其他测试。
24. notebook 不写 parquet/csv。
25. 所有新增测试通过。

---

# 16. 给 coding agent 的最后提醒

本任务的核心是生成 **全年 depot-only charging load 数据**，不是做前端，也不是规划 charger 数量。

最重要的输出是：

```text
depot_registry.parquet
    每个 depot / operational anchor 的位置和置信度

depot_load_15min.parquet
    每个 depot 每一天每 15 分钟的充电曲线
```

所有代码和文档必须诚实表达：

```text
1. depot 可能是 inferred operational anchor，不一定是真实物理车库；
2. 只考虑 depot charging；
3. 不考虑公共充电；
4. 不强行修复 infeasible cases；
5. 当前默认 ev_stock_scale 模式不是全英国公交完全电动化情景。
```
