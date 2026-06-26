# Bus 仿真重构施工指令：M1-style Depot-only 抽样电动公交仿真管线

## 0. 任务定位

请将当前 bus 仿真管线重构为一条更清晰、更适合研究分析的 **M1-style 链式仿真路线**。本任务不是完整复刻原始 M1 prompt，而是在 M1 的 event-ledger / chain-style 思想基础上，改成一个 **depot-only、抽样 block、使用现有 EV bus inventory 的仿真管线**。

新的核心建模问题是：

> 从英国全国公共汽车 block 中抽取一组代表性 block，并将 EV inventory 中所有可用电动公交车逐辆、不可重复地分配到这些 block 上。如果车辆只在根据 block terminal/end LSOA 众数推断出的 operational depot 充电，那么这些 EV bus 的 SOC 轨迹、能耗、depot 充电需求、15 分钟负荷和不可行性情况是什么？

---

## 1. 与原 M1 的关系和关键改动

原 M1 管线包含 public charger、OCM、opportunity charging、L0-L4 resolution cascade，并要求所有 chain 最终都被 resolution cascade 解决。

本任务刻意修改为：

1. 只使用 M1-style 的链式建模思想。
2. 不再把 `run_bus_annual.py` legacy 年度管线作为主线。
3. 不考虑公共充电、OCM、terminal opportunity charging。
4. 只允许 depot charging。
5. 不强行 resolve infeasible cases。
6. depot-only infeasible 是有效模型结果，必须保留并报告。
7. 不创建 synthetic larger battery。
8. 不创造新 charger。
9. 不做 fleet sizing recommendation。

因此，本任务中的阶段不要命名为原 M1 的 L0/L1/L2/L3/L4，以免和原 SOC resolution cascade 混淆。请使用 **Stage 0 - Stage 6** 的实施结构。

---

## 2. 总体流程

新的主流程为：

```text
all_blocks.parquet
    -> Stage 0: 预检查与字段口径确认
    -> Stage 1: 从全国 blocks 中抽样 1200 个代表性 block，不放回抽样
    -> Stage 2: 使用 block-level terminal/end LSOA 众数推断 operational depot LSOA
    -> Stage 3: 将 EV inventory 中全部有效 bus/minibus 车辆实例化，并不可放回分配到 sampled blocks
    -> Stage 4: 为每个 simulation case 构建 depot-only event ledger
    -> Stage 5: 执行 depot-only SOC walk 和 feasibility 诊断
    -> Stage 6: 聚合 depot 15min charging load 并生成 run_summary.md
```

重要概念：

```text
block = 一个公交运营任务模板，不等于真实车辆
EV bus instance = EV inventory 中的一行（已经是逐辆，一行=一辆车，EV_ID 唯一）
simulation case = 一个 EV bus instance + 一个 sampled block
operational depot = 从该 block 自身 terminal/end LSOA 众数推断出的充电 anchor，不是经过验证的真实物理车库
```

---

## 3. 最重要的口径修正

本任务采用以下最终口径：

1. 从全国 bus block 表中抽样 1200 个 block，抽样方式为 without replacement。
2. 从 EV inventory 中提取全部可用 electric bus/minibus。
3. **EV inventory 已经是逐辆一行（`EV_ID` 唯一，bus/minibus 共 6,222 辆）。直接把每一行当作一个 vehicle instance，不要做任何 count 展开。**`count` 列是该 `(LSOA, Model)` 组的车辆数、被抄在组内每一行（组内恒定），只能用于审计/诊断；**严禁 `sum(count)`（=Σ组大小²=45,276，平方膨胀）或按 `count` 展开（会造出数万幽灵车，并把大组平方级放大）。**
4. 每个 EV bus instance 只能使用一次。
5. 每个 EV bus instance 分配到一个 sampled block 上。
6. 如果 EV bus 数量大于 sampled block 数量，则允许多个车辆映射到同一个 block。
7. 如果 EV bus 数量小于 sampled block 数量，则只有分配到车辆的 block 生成 simulation cases。
8. 最终 simulation case 数量应等于有效 EV bus instance 数量（≈6,222，取决于参数 sanity 过滤后剩余行数），而不是固定 `1200 * 5 = 6000`，也不是 `sum(count)`。

不要再保留“每个 block 抽样 5 辆 EV bus”的逻辑。这是旧版本设定，已被本任务替换。

---

## 4. 输入数据

### 4.1 Bus block 数据

使用当前已有的 block 构建结果，例如：

```text
outputs/all_blocks.parquet
```

或仓库中当前 bus pipeline 实际读取的 block parquet 路径。

本任务不重写：

```text
GTFS 解析
block 构建
block inference
all_blocks.parquet 生成逻辑
```

只要现有 block 数据能提供以下字段或等价字段即可继续：

```text
block_id
agency_id
block_source
start_time
end_time
duration_h
distance_km
start_stop
end_stop
start_lat
start_lon
end_lat
end_lon
start_lsoa
end_lsoa
```

如果字段命名不同，请在代码中做兼容映射，并在 `run_summary.md` 中记录实际使用的字段。

### 4.2 EV bus inventory

使用：

```text
data/EV_UK_LSOA_2025_with_energy.csv
```

或项目当前实际使用的 EV inventory 路径。

只保留：

```python
vehicle_subtype in {"bus", "minibus"}
```

不要包含 coach。

需要保留或生成以下车辆参数：

```text
vehicle_id
vehicle_model
vehicle_subtype
source_lsoa
battery_kwh
consumption_kwh_per_km
ac_charge_kw_max
dc_charge_kw_max
usable_soc_min
usable_soc_max
count
```

能耗单位转换必须使用：

```python
consumption_kwh_per_km = efficiency_wh_per_km / 1000.0
```

不要用 `energy_kWh_per_100km / 100` 替代，除非原始数据中没有 `efficiency_wh_per_km`。

在本 depot-only 模型中：

```text
depot charging 使用 ac_charge_kw_max
dc_charge_kw_max 只保留用于审计，不参与充电
```

---

# Stage 0 - 预检查与口径确认

## 0.1 目标

在写核心仿真逻辑前，先检查输入字段、EV bus 数量（每行=一辆车，预期 ≈6,222）、`count` 列语义（应=组大小，非逐行台数），以及本次运行的最终 simulation case 数量。

## 0.2 需要完成的事情

1. 读取 block parquet。注意：`outputs/all_blocks.parquet` 是 **trip 级**且**不含 LSOA 列**——Stage 0 需先聚合成 block 级（按 `agency_id, block_id`，参考 `mobility.bus.block_instances.build_block_templates`）并跑 `mobility.bus.data_loader.attach_lsoa` 得到 `start_lsoa/end_lsoa`，并报告 LSOA 命中率。
2. 检查必要字段是否存在（含上一步产出的 LSOA 列）。
3. 读取 EV inventory。
4. 过滤 bus/minibus。
5. 断言 EV inventory 已是**逐车辆表**（`EV_ID` 唯一；本数据 bus/minibus = 6,222 行 = 6,222 辆）。`count` 是 `(LSOA, Model)` 组大小、抄在每行（组内恒定），**不是逐行台数**。
6. **不做 count 展开**——每行即一辆车。若校验发现某 `(LSOA, Model)` 组的 `count` ≠ 组内行数，则报数据质量警告（说明 count 语义与预期不符），但默认仍以行数为准。
7. 统计有效 EV bus instance 数量（= sanity 过滤后剩余行数）。
8. 写出 preflight diagnostics。

## 0.3 输出

```text
outputs/bus_depot_only_sample/preflight_summary.json
outputs/bus_depot_only_sample/preflight_summary.md
```

至少包含：

```text
n_blocks_available
block_required_columns_present
block_missing_columns
n_ev_rows_raw
n_ev_rows_bus_minibus
n_ev_instances_valid_after_sanity   # = 有效逐辆行数，预期 ≈6,222；不是 sum(count)
count_column_matches_group_size     # 校验 count 是否=(LSOA,Model)组内行数（应为 True）
```

## 0.4 测试

新增或更新：

```text
tests/mobility/bus/test_depot_only_preflight.py
```

测试：

```text
test_preflight_detects_missing_block_columns
test_preflight_filters_bus_minibus_only
test_preflight_treats_each_row_as_one_vehicle   # 不按 count 展开
test_preflight_count_equals_group_size_not_per_row
test_preflight_reports_valid_vehicle_instance_count
```

---

# Stage 1 - Block 抽样

## 1.1 目标

从全国 bus block 中抽样 1200 个代表性 block。抽样必须是 without replacement，并且不能被 London 或其他高频区域支配。

## 1.2 抽样数量

默认：

```python
N_BLOCKS = 1200
```

如果有效 block 总数少于 1200，则使用全部有效 block，并在 `run_summary.md` 中说明。

## 1.3 抽样方法

不能使用完全无约束的全国随机抽样。

建议分层字段：

```text
region_key
agency_id
block_source
distance_bin
duration_bin
```

如果没有现成 `region_key`，则从以下信息推断：

```text
start_lsoa
end_lsoa
start_lat/start_lon
end_lat/end_lon
```

建议逻辑：

1. 从 LSOA 或坐标生成 `region_key`。
2. 根据 `distance_km` 生成 distance bin。
3. 根据 `duration_h` 或 start/end time 生成 duration bin。
4. 按 region + distance_bin + duration_bin 做分层抽样。
5. 设置区域占比上限，避免单一区域占比过高。

## 1.4 区域支配保护

新增诊断规则：

```python
MAX_REGION_SAMPLE_SHARE = 0.35
```

任何单一区域 sampled block share 不应超过 35%，除非其他区域没有足够有效 block。

如果某区域超过上限，应下采样该区域，并把剩余名额分配给其他区域。

## 1.5 随机性

所有抽样必须可复现：

```python
RNG_SEED = 20260603
```

## 1.6 输出

```text
outputs/bus_depot_only_sample/sampled_blocks.parquet
outputs/bus_depot_only_sample/block_sample_diagnostics.parquet
```

`sampled_blocks.parquet` 字段至少包括：

```text
sampled_block_id
block_id
agency_id
region_key
block_source
start_time
end_time
duration_h
distance_km
start_lsoa
end_lsoa
start_lat
start_lon
end_lat
end_lon
sample_weight
```

`block_sample_diagnostics.parquet` 字段至少包括：

```text
region_key
n_available_blocks
n_sampled_blocks
available_share
sample_share
is_capped
```

## 1.7 测试

新增：

```text
tests/mobility/bus/test_depot_only_block_sampling.py
```

测试：

```text
test_samples_exactly_requested_number
test_sampling_is_without_replacement
test_sampling_is_deterministic
test_region_dominance_guard
test_sample_diagnostics_written
```

---

# Stage 2 - Operational Depot 推断

## 2.1 目标

为每个 sampled block 推断一个 depot-only charging anchor。

这里的 depot 不是经过验证的真实物理车库，而是：

```text
operational_depot_lsoa = block 自身 terminal/end LSOA 的众数
```

## 2.2 禁止的做法

不要使用：

```text
全国 LSOA 众数
stop clustering
一个全国虚拟 depot
OCM charger location
public charger location
让 London 高频车次吸收全国 depot
```

## 2.3 block-level 众数

对每个 block，收集它自身内部的候选 LSOA。

可用候选字段包括：

```text
first_stop_lsoa
last_stop_lsoa
start_lsoa
end_lsoa
trip_end_lsoa
terminal_lsoa
layover_lsoa
```

如果只有 `start_lsoa` 和 `end_lsoa`，则使用这两个。

主规则：

```text
operational_depot_lsoa = 当前 block 内候选 terminal/end LSOA 的众数
```

这意味着每个 block 独立推断 depot，不能对全国所有 block 求全局众数。

## 2.4 避免 London 吸收全国 depot

必须加测试或诊断确保：

```text
非 London block 不能因为 London 是全国最大众数而被分配到 London depot。
```

## 2.5 tie-break 规则

如果众数并列：

1. 优先选择 block 最终 `end_lsoa`。
2. 如果仍并列，优先选择最长 dwell / terminal time 对应的 LSOA。
3. 如果没有 dwell 信息，则选择字典序最小的 LSOA，保证确定性。
4. 记录 `depot_tie_resolved=True`。

## 2.6 fallback

如果 block 没有可用 LSOA：

1. 尝试用最终 stop 坐标匹配最近 LSOA。
2. 如果仍失败，则：

```text
operational_depot_lsoa = NaN
depot_confidence = "missing"
manual_review_flag = True
```

这些 block 默认不进入最终 simulation cases，但必须写入 dropped diagnostics。

## 2.7 depot_id 定义

为了避免不同运营商或区域的同一 LSOA 被错误混合：

```python
depot_id = f"opdepot_{agency_id}_{operational_depot_lsoa}"
```

## 2.8 depot confidence

```text
high:
    block 内有清晰唯一众数，且候选 LSOA 数量 >= 2

medium:
    只有一个候选 LSOA
    或众数存在并列但已通过 tie-break 解决

low:
    通过坐标 fallback 得到 LSOA

missing:
    无法推断 LSOA
```

## 2.9 depot 坐标

坐标只用于 deadhead 和负荷展示，不要声称是精确车库地址。

优先级：

1. 使用该 block 在 modal LSOA 内 terminal/end stop 坐标的中位数。
2. 如果没有，使用 LSOA centroid。
3. 如果仍没有，lat/lon 设为 NaN，`manual_review_flag=True`。

## 2.10 输出

```text
outputs/bus_depot_only_sample/operational_depot_registry.parquet
outputs/bus_depot_only_sample/depot_inference_diagnostics.parquet
```

`operational_depot_registry.parquet` 字段至少包括：

```text
depot_id
agency_id
operational_depot_lsoa
lat
lon
depot_source
depot_confidence
n_sampled_blocks
n_simulation_cases
manual_review_flag
```

其中：

```text
depot_source = "block_terminal_lsoa_mode"
```

## 2.11 测试

新增：

```text
tests/mobility/bus/test_operational_depot_inference.py
```

测试：

```text
test_depot_lsoa_uses_block_level_mode_not_global_mode
test_tie_prefers_final_end_lsoa
test_missing_lsoa_sets_manual_review_flag
test_depot_id_includes_agency_and_lsoa
test_london_does_not_absorb_non_london_blocks
```

---

# Stage 3 - EV bus 实例构建与分配

## 3.1 目标

EV inventory 已是逐辆一行；将所有有效 electric bus/minibus **行**作为 vehicle instances（不展开），并以 without replacement 的方式分配到 sampled blocks。

## 3.2 车辆过滤

只保留：

```python
vehicle_subtype in {"bus", "minibus"}
```

不要包含 coach。

## 3.3 vehicle instance 构建（不要 count 展开）

**本数据每行就是一辆车（`EV_ID` 唯一），不需要、也不允许按 `count` 展开。** 直接把每个有效行当作一个 vehicle instance：

```text
vehicle_id = 该行的 EV_ID（如 bus_1468）
```

`count` 是该 `(LSOA, Model)` 组的车辆数、被抄在组内每一行（组内恒定），仅供审计；不要 sum、不要据此复制行。

反例（错误做法，禁止）：把 `count=93` 的行复制成 93 份 —— 因为该组本就有 93 行，复制后变成 93×93=8,649 辆幽灵车，并把伦敦等大组平方级放大。

每个 vehicle instance 只能使用一次。

## 3.4 参数 sanity check

仿真前必须检查：

```text
battery_kwh > 0
0.7 <= consumption_kwh_per_km <= 3.0
ac_charge_kw_max > 0
usable_soc_min < usable_soc_max
```

不满足条件的 EV rows 或 vehicle instances 应剔除并写入 diagnostics。

## 3.5 车辆到 block 的分配

将每个 EV bus instance 分配到一个 sampled block。

推荐规则：

1. 优先将车辆分配到同 region/country 的 sampled block。
2. 如果同 region/country block 不足，则允许分配到全国 sampled block。
3. block 可以接收多个车辆实例。
4. 车辆不可重复使用。
5. 最终 simulation case 数量等于有效 EV bus instance 数量。

这不是 fleet scheduling，也不是 time-space assignment。这里只是为了把现有 EV bus stock 映射到一组代表性 block 上做 depot-only 仿真。

## 3.6 输出

```text
outputs/bus_depot_only_sample/ev_bus_instances.parquet
outputs/bus_depot_only_sample/simulation_cases.parquet
outputs/bus_depot_only_sample/vehicle_assignment_diagnostics.parquet
```

`ev_bus_instances.parquet` 字段至少包括：

```text
vehicle_id
source_row_id
vehicle_model
vehicle_subtype
source_lsoa
battery_kwh
consumption_kwh_per_km
ac_charge_kw_max
dc_charge_kw_max
usable_soc_min
usable_soc_max
vehicle_instance_weight
```

`simulation_cases.parquet` 字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_id
agency_id
region_key
depot_id
operational_depot_lsoa
assignment_method
case_status
drop_reason
```

## 3.7 测试

新增：

```text
tests/mobility/bus/test_ev_bus_instance_assignment.py
```

测试：

```text
test_each_row_is_one_vehicle_instance_no_count_expansion
test_each_vehicle_instance_used_once
test_simulation_case_count_equals_valid_vehicle_instance_count
test_vehicle_assignment_is_deterministic
test_invalid_vehicle_rows_are_dropped
test_consumption_unit_conversion
```

---

# Stage 4 - Depot-only Event Ledger

## 4.1 目标

为每个 simulation case 构建事件账本。

## 4.2 最小事件序列

每个 case 至少包含：

```text
depot_parking_pre
depot_to_block_deadhead
passenger_block
block_to_depot_deadhead
depot_parking_post
```

如果 block 内部 trip-level 信息可用，可以把 `passenger_block` 拆成多个 `passenger_trip` 事件。

如果当前 block 只有 aggregate 信息，一个 `passenger_block` 事件也可以接受。

## 4.3 deadhead 假设

使用 haversine 距离：

```text
distance_method = "haversine_x_1.0"
deadhead_speed_kmh = 30.0
```

必须包含：

```text
depot -> block start 的 deadhead
block end -> depot 的 deadhead
```

## 4.4 时间处理

如果 block 的 start/end time 可用，则：

1. `depot_parking_pre` 从当天 00:00 到 depot deadhead 出发前。
2. `depot_to_block_deadhead` 在 passenger block 开始前结束。
3. `passenger_block` 使用 block start/end time。
4. `block_to_depot_deadhead` 从 block end 后开始。
5. `depot_parking_post` 从回到 depot 后到当天 24:00。

如果 block 跨午夜，必须保持时间顺序正确，不要简单截断到 24:00。

## 4.5 禁止事件

在本 pipeline 中不得出现：

```text
public_charger_event
opportunity_charging
terminal_public_charging
OCM_station_event
```

## 4.6 输出

```text
outputs/bus_depot_only_sample/vehicle_day_events.parquet
```

字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_id
depot_id
operational_depot_lsoa
service_date_or_template_date
event_seq
event_type
start_time
end_time
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

Stage 4 可以先写出未填 SOC 的 event ledger；Stage 5 再补充 `charge_*` 与 `soc_*` 字段。

## 4.7 测试

新增：

```text
tests/mobility/bus/test_depot_only_event_ledger.py
```

测试：

```text
test_every_case_has_event_ledger
test_event_sequence_starts_and_ends_at_depot
test_event_seq_is_strictly_increasing
test_deadhead_events_are_present
test_no_public_charging_events_created
```

---

# Stage 5 - Depot-only SOC Walk 与 Feasibility

## 5.1 目标

计算每个 simulation case 的 SOC 轨迹、depot charging、energy shortfall 和 depot-only feasibility。

## 5.2 初始 SOC

默认：

```python
initial_soc_kwh = battery_kwh * usable_soc_max
```

## 5.3 行驶扣电

movement events 包括：

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

## 5.4 depot charging

只允许以下事件充电：

```text
depot_parking_pre
depot_parking_post
其他明确位于 depot 的 parking event
```

实际充电功率：

```python
effective_charge_kw = min(vehicle.ac_charge_kw_max, depot_power_kw)
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

## 5.5 depot_power_kw

每个 depot 的 depot power 可采用：

优先：

```text
该 depot 下 assigned EV bus instances 的 ac_charge_kw_max 中位数
```

fallback：

```text
100 kW
```

记录：

```text
depot_power_kw
depot_power_source
```

其中：

```text
depot_power_source = "assigned_vehicle_ac_median" 或 "default_100kw"
```

## 5.6 不要 clamp SOC

不要把 SOC 强行限制在 0 以上。

负 SOC 是诊断信号，用于计算 shortfall。

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

## 5.7 infeasibility reason

不可行不是代码错误，而是模型结果。

不要：

```text
创建更大的 synthetic battery
插入公共充电
创造新 charger
强行把 SOC clamp 到 0
删除 infeasible case
```

建议 reason 类型：

```text
single_trip_exceeds_usable_battery
daily_energy_exceeds_usable_battery
insufficient_depot_charging_time
midday_depletion_no_depot_window
missing_depot_lsoa
invalid_vehicle_parameters
unknown
```

## 5.8 输出

```text
outputs/bus_depot_only_sample/vehicle_day_events.parquet
outputs/bus_depot_only_sample/case_soc_summary.parquet
```

`case_soc_summary.parquet` 字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_id
depot_id
operational_depot_lsoa
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

## 5.9 测试

新增：

```text
tests/mobility/bus/test_depot_only_soc.py
```

测试：

```text
test_no_public_charging_used
test_depot_charging_uses_ac_power
test_soc_not_clamped_below_zero
test_infeasible_case_reports_shortfall
test_feasible_case_has_no_shortfall
test_case_soc_summary_has_required_columns
```

---

# Stage 6 - Depot 15min Load Aggregation 与 Run Summary

## 6.1 目标

将 depot charging energy 分配到 15 分钟时间槽，并生成 run summary。

## 6.2 15 分钟负荷聚合

只聚合 depot charging，不聚合公共桩。

输出：

```text
outputs/bus_depot_only_sample/depot_load_15min.parquet
```

字段至少包括：

```text
depot_id
operational_depot_lsoa
time_slot
slot_start
slot_end
charge_kwh
average_kw
n_active_cases
```

要求：

1. `depot_load_15min.charge_kwh` 总和应与 event ledger 中 depot charging 总量一致，允许极小浮点误差。
2. 不出现 public station id。
3. 不依赖 OCM。

## 6.3 run_summary.md

输出：

```text
outputs/bus_depot_only_sample/run_summary.md
```

必须包含：

```text
n_blocks_available
n_blocks_sampled
n_ev_instances_available
n_simulation_cases_created
n_cases_successful
n_cases_dropped
n_depots_inferred
depot_confidence_distribution
region_sample_distribution
vehicle_model_distribution
depot_only_feasible_share
top_10_depots_by_charge_kwh
top_10_blocks_by_energy_shortfall
主要建模假设
主要数据质量问题
```

## 6.4 测试

新增：

```text
tests/mobility/bus/test_depot_only_outputs.py
```

测试：

```text
test_depot_load_has_only_depot_lsoas
test_depot_load_energy_matches_event_ledger
test_run_summary_is_written
test_run_summary_contains_key_assumptions
```

---

## 5. 新增脚本

新增脚本：

```text
scripts/run_bus_depot_only_sample.py
```

建议 CLI：

```bash
python scripts/run_bus_depot_only_sample.py \
  --blocks outputs/all_blocks.parquet \
  --ev-inventory data/EV_UK_LSOA_2025_with_energy.csv \
  --out-dir outputs/bus_depot_only_sample \
  --n-blocks 1200 \
  --seed 20260603 \
  --charging-mode depot_only
```

注意：不要再使用 `--vehicle-draws-per-block 5` 作为默认核心参数。如果保留该参数用于实验，也必须默认关闭，并且不能影响本任务的主路径。

---

## 6. 推荐新增模块

可以新增以下模块，避免污染原有 M1 代码：

```text
mobility/bus/depot_only_preflight.py
mobility/bus/depot_only_sampling.py
mobility/bus/operational_depot.py
mobility/bus/ev_bus_instances.py
mobility/bus/depot_only_assignment.py
mobility/bus/depot_only_events.py
mobility/bus/depot_only_soc.py
mobility/bus/depot_only_outputs.py
```

如果复用已有模块，需要保证旧的 public charging / OCM / L0-L4 cascade 不会在这个新 pipeline 中被误触发。

---

## 7. 总输出清单

所有输出写入：

```text
outputs/bus_depot_only_sample/
```

必须输出：

```text
preflight_summary.json
preflight_summary.md
sampled_blocks.parquet
block_sample_diagnostics.parquet
operational_depot_registry.parquet
depot_inference_diagnostics.parquet
ev_bus_instances.parquet
simulation_cases.parquet
vehicle_assignment_diagnostics.parquet
vehicle_day_events.parquet
case_soc_summary.parquet
depot_load_15min.parquet
run_summary.md
```

---

## 8. 明确不要做的事情

本任务不要实现：

```text
公共充电
OCM charger matching
nearest public charger search
opportunity charging
新 charger site
fleet sizing recommendation
min-cost flow assignment
全英国所有 block 全量仿真
coach 仿真修改
private car 仿真修改
OSRM / 路网绕行
温度修正
随机延误传播
Monte Carlo timetable delay
原 M1 L0-L4 resolution cascade
```

本任务只做：

```text
sampled bus blocks + all available EV bus instances + depot-only charging
```

---

## 9. Acceptance Criteria

任务完成标准：

1. 新脚本 `scripts/run_bus_depot_only_sample.py` 可以端到端运行。
2. 默认抽样 1200 个 block，除非有效 block 少于 1200。
3. block 抽样是 without replacement。
4. 抽样具有确定性。
5. London 或其他高频区域不能支配 sampled blocks，除非数据本身缺乏替代样本。
6. EV inventory 中每个有效 bus/minibus **行**直接作为一个 vehicle instance（不做 count 展开；`count` 仅审计）。
7. 每个 EV bus instance 最多使用一次。
8. simulation case 数量等于有效 EV bus instance 数量（≈6,222），不是 `sum(count)`、也不是 `1200*5`。
9. depot 使用 block-level terminal/end LSOA 众数推断。
10. depot 众数绝不能是全国全局众数。
11. 非 London block 不会因为 London 是全局众数而被分配到 London depot。
12. 不使用公共充电。
13. 不依赖 OCM 数据。
14. 每个 simulation case 都有 event ledger。
15. 每个 event ledger 都有 `soc_start_kwh` 和 `soc_end_kwh`。
16. SOC 不 clamp 到 0。
17. depot-only infeasible cases 被保留并明确标记。
18. 输出 `depot_load_15min.parquet`。
19. 输出 `run_summary.md`。
20. 所有新增测试通过。
21. 不破坏现有 bus/coach/car 其他测试。
22. notebook 不写 parquet/csv。
23. 代码和输出中都必须明确说明：这里的 depot 是 operational charging anchor，不是经过验证的真实物理车库。

---

## 10. 给代码 agent 的最后提醒

这个任务不是要估计英国真实 bus depot 的精确位置。

本任务中的 depot 是：

```text
operational_depot_lsoa
```

它来自：

```text
block 自身 terminal/end LSOA 的众数
```

因此请避免使用以下容易误导的命名：

```text
real_depot
verified_garage
physical_depot
true_depot_location
```

建议使用：

```text
operational_depot_lsoa
block_terminal_lsoa_mode
depot_confidence
manual_review_flag
```

核心建模假设必须写进代码注释和 `run_summary.md`：

> 本 pipeline 从全国 bus block 中抽样 1200 个代表性 block，并将 EV inventory 中所有有效 bus/minibus 车辆实例逐辆、不可重复地分配到这些 sampled blocks 上。车辆只在根据该 block terminal/end LSOA 众数推断出的 operational depot 充电。本模型用于分析 depot-only EV bus 情景下的 SOC、能耗、充电负荷和不可行性，不声称推断出真实物理 depot。
