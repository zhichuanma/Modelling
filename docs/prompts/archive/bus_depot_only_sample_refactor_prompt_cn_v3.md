# Bus 仿真重构施工指令 v3：M1-style Depot-only 当前 EV bus stock 情景仿真管线

## 0. 任务定位

请将当前 bus 仿真管线重构为一条更清晰、更适合研究分析的 **M1-style 链式仿真路线**。本任务不是完整复刻原始 M1 prompt，而是在 M1 的 event-ledger / chain-style 思想基础上，改成一个 **depot-only、抽样 block、使用现有 EV bus inventory 的情景仿真管线**。

本版 v3 的核心估计目标（estimand）为：

> **当前 EV bus inventory 规模下的 depot-only 电动公交情景负荷。** 也就是说，EV inventory 中有多少辆有效 bus/minibus，就从全国 bus block templates 中按真实 duty 分布抽取多少个代表性 block，并做“一辆 EV bus instance 对应一个 sampled block”的配对仿真。结果代表“现有 EV bus stock 如果承担从全国 bus duties 中按真实分布抽取的一组任务，在 depot-only 充电假设下会产生怎样的 SOC、能耗、不可行性和 15 分钟 depot charging load”。

本任务输出的 `depot_load_15min.parquet` 是 **当前 EV bus stock 情景负荷**，不是全英国所有 bus 完全电动化后的总负荷，也不是 1200 个诊断样本的无权重总和。

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
10. 不使用 EV 登记 `source_lsoa` 来决定车辆空间分布；车辆只作为技术参数捐赠者。

因此，本任务中的阶段不要命名为原 M1 的 L0/L1/L2/L3/L4，以免和原 SOC resolution cascade 混淆。请使用 **Stage 0 - Stage 7** 的实施结构。

---

## 2. 总体流程

正式模式主流程为：

```text
all_blocks.parquet
    -> Stage 0: trip-level all_blocks 预处理、block_templates 构建、LSOA attach、EV preflight
    -> Stage 1: 按真实 duty 分布比例抽取 n_valid_ev_bus_instances 个 block templates
    -> Stage 2: 使用 block-level terminal/end LSOA 规则推断 operational depot LSOA
    -> Stage 3: 将 EV inventory 中全部有效 bus/minibus 行作为 vehicle instances
    -> Stage 4: 将 vehicle instances 与 sampled blocks 一对一确定性配对
    -> Stage 5: 为每个 simulation case 构建 depot-only event ledger
    -> Stage 6: 执行 depot-only SOC walk 和 feasibility 诊断
    -> Stage 7: 聚合 depot 15min charging load 并生成 run_summary.md
```

重要概念：

```text
block template = 一个公交运营任务模板，不等于真实车辆
EV bus instance = EV inventory 中的一行（已经是逐辆，一行=一辆车，EV_ID 唯一）
simulation case = 一个 EV bus instance + 一个 sampled block template
operational depot = 从该 block 自身 terminal/end LSOA 推断出的充电 anchor，不是经过验证的真实物理车库
```

---

## 3. 最重要的口径修正

本任务采用以下最终口径：

1. 正式模式不再固定抽样 1200 个 block。
2. 正式模式中，sampled block 数量应等于有效 EV bus instance 数量。
3. 如果有效 EV bus instance 数量约为 6,222，则正式模式抽样约 6,222 个 block templates，并生成约 6,222 个 simulation cases。
4. `--n-blocks 1200` 只作为 pilot/debug 模式，用于快速跑通流程，不作为正式情景结果。
5. EV inventory 已经是逐辆一行（`EV_ID` 唯一）。直接把每一行当作一个 vehicle instance，不要做任何 count 展开。
6. `count` 列是该 `(LSOA, Model)` 组的车辆数、被抄在组内每一行（组内恒定），只能用于审计/诊断；严禁 `sum(count)` 或按 `count` 展开。
7. 每个 EV bus instance 只能使用一次。
8. 正式模式中，每个 sampled block 默认只接收一个 EV bus instance。
9. simulation case 数量应等于有效 EV bus instance 数量，而不是固定 `1200 * 5`，也不是 `sum(count)`。
10. 不再保留“每个 block 抽样 5 辆 EV bus”的逻辑。
11. vehicle `source_lsoa` 不参与 block 匹配，不参与 depot 负荷空间分布，只作为 audit 字段保留。
12. `sample_weight` 在本任务中默认只作为抽样诊断字段，不用于主输出 `depot_load_15min` 加权外推全国所有 bus duties。

如果未来需要估计“全英国所有 bus block 完全电动化”的全国总量，应另开显式参数，例如：

```text
--weighting-mode national_block_expansion
```

本任务默认不实现该全国扩展模式。

---

## 4. 输入数据

### 4.1 Bus block / trip 数据

使用当前已有的 bus block 构建结果，例如：

```text
outputs/all_blocks.parquet
```

或仓库中当前 bus pipeline 实际读取的 block parquet 路径。

注意：当前 `outputs/all_blocks.parquet` 预期是 **trip 级输入**，不要求已经包含 `start_lsoa/end_lsoa`。本任务不重写 GTFS 解析、block inference 或 `all_blocks.parquet` 生成逻辑，但 Stage 0 必须基于该 trip 级表构建 block templates。

Stage 0 必须完成：

```text
trip-level rows
    -> attach_lsoa_to_trip_endpoints
    -> build_block_templates
    -> block_templates.parquet
```

Stage 1 以后才可以假设 sampled block 中存在：

```text
block_template_id
block_id
agency_id
block_source
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
start_lsoa
end_lsoa
candidate_terminal_lsoas
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

# Stage 0 - 预检查、block_templates 构建与 LSOA attach

## 0.1 目标

在写核心仿真逻辑前，先完成四件事：

1. 将 trip-level `all_blocks.parquet` 聚合成 block-level templates。
2. 对 trip/block 起终点坐标 attach LSOA，并报告命中率。
3. 检查 EV bus 数量与 `count` 列语义。
4. 确定正式模式下的 simulation case 数量。

## 0.2 需要完成的事情

1. 读取 `all_blocks.parquet`。
2. 确认它是 trip-level 还是 block-level；如果是 trip-level，则按现有项目逻辑聚合成 block templates。
3. 聚合建议参考 `mobility.bus.block_instances.build_block_templates` 或当前仓库中最稳定的 block template 构建逻辑。
4. 对 trip endpoints 或 block template start/end coordinates 运行 LSOA attach。
5. 产出 `start_lsoa/end_lsoa` 以及可选的 `candidate_terminal_lsoas`。
6. 报告 LSOA 命中率，包括 polygon 命中、centroid fallback、missing。
7. 读取 EV inventory。
8. 过滤 bus/minibus。
9. 断言 EV inventory 已是逐车辆表：`EV_ID` 唯一；每行即一辆车。
10. 不做 count 展开。
11. 若某 `(LSOA, Model)` 组的 `count` 不等于组内行数，则报数据质量警告，但默认仍以行数为准。
12. 执行车辆参数 sanity check。
13. 统计有效 EV bus instance 数量。
14. 正式模式下设置：

```text
n_simulation_cases = n_valid_ev_bus_instances
n_sampled_blocks = n_valid_ev_bus_instances
```

15. pilot/debug 模式下允许显式覆盖：

```text
--sample-mode pilot --n-blocks 1200
```

但 pilot 输出不得被默认解释为正式情景负荷。

## 0.3 参数 sanity check

EV rows / vehicle instances 必须满足：

```text
battery_kwh > 0
0.7 <= consumption_kwh_per_km <= 3.0
ac_charge_kw_max > 0
usable_soc_min < usable_soc_max
```

特别要求：

1. preflight 必须报告被 `consumption_kwh_per_km < 0.7` 丢弃的车辆数量。
2. preflight 必须列出最小若干条异常电耗记录，包括 `vehicle_id / vehicle_model / source_lsoa / efficiency_wh_per_km / consumption_kwh_per_km / efficiency_source`（如果存在该字段）。
3. run_summary 必须说明 minibus 是否为空。如果过滤后 minibus 为 0 行，不要让读者误以为结果包含 minibus。

## 0.4 输出

```text
outputs/bus_depot_only_sample/preflight_summary.json
outputs/bus_depot_only_sample/preflight_summary.md
outputs/bus_depot_only_sample/block_templates.parquet
outputs/bus_depot_only_sample/lsoa_attach_diagnostics.parquet
outputs/bus_depot_only_sample/invalid_vehicle_rows.parquet
```

`preflight_summary` 至少包含：

```text
n_trip_rows_raw
n_block_templates_available
block_template_required_columns_present
block_template_missing_columns
lsoa_attach_polygon_hit_rate
lsoa_attach_fallback_rate
lsoa_attach_missing_rate
n_ev_rows_raw
n_ev_rows_bus_minibus
n_ev_instances_valid_after_sanity
n_ev_instances_dropped_by_low_consumption
n_ev_instances_dropped_by_invalid_battery
n_minibus_instances_valid
count_column_matches_group_size
sample_mode
n_sampled_blocks_planned
n_simulation_cases_planned
```

## 0.5 测试

新增或更新：

```text
tests/mobility/bus/test_depot_only_preflight.py
```

测试：

```text
test_preflight_builds_block_templates_from_trip_level_input
test_preflight_attaches_lsoa_to_block_templates
test_preflight_detects_missing_block_columns
test_preflight_filters_bus_minibus_only
test_preflight_treats_each_row_as_one_vehicle
test_preflight_count_equals_group_size_not_per_row
test_preflight_reports_valid_vehicle_instance_count
test_preflight_reports_low_consumption_drops
test_preflight_reports_minibus_count
```

---

# Stage 1 - Block template 抽样

## 1.1 目标

从全国 bus block templates 中抽取一组代表性 duties。正式模式下，抽样数量等于有效 EV bus instance 数量。抽样必须是 without replacement。

## 1.2 抽样模式

支持两种模式：

### 正式模式：`full_ev_inventory`

默认模式：

```text
sample_mode = full_ev_inventory
n_blocks = n_valid_ev_bus_instances
```

如果有效 EV bus instance 为 6,222，则抽取 6,222 个 block templates，并生成 6,222 个 simulation cases。

### pilot/debug 模式：`pilot`

用于快速测试：

```text
sample_mode = pilot
n_blocks = 1200
```

pilot 模式输出必须在 `run_summary.md` 中明确标记为 pilot/debug，不得默认解释为正式情景负荷。

## 1.3 抽样方法：按真实 duty 分布比例抽样

正式模式应尽量保持原始 block template 池的真实 duty 分布，而不是人为拉平区域。

推荐分层字段：

```text
region_key
distance_bin
duration_bin
block_source
```

可选加入 `agency_id`，但不要让过细的 agency strata 导致大量空层或过拟合。

抽样逻辑：

1. 为每个 block template 生成 `region_key`。
2. 根据 `passenger_distance_km` 或 `distance_km` 生成 distance bin。
3. 根据 `duration_h` 或 start/end time 生成 duration bin。
4. 构造 stratum：`region_key + distance_bin + duration_bin`。
5. 按各 stratum 在可用 block template 池中的真实占比分配样本数。
6. 每个非空 stratum 在可能时至少抽取 1 个样本。
7. 若某些 stratum 样本不足，则将剩余名额按比例重新分配给有余量的 stratum。
8. 在每个 stratum 内 without replacement 抽样。

## 1.4 region_key 定义

`region_key` 必须使用 GOR/country 级别，不使用 LAD 级别作为区域支配保护单位。

推荐区域集合：

```text
North East
North West
Yorkshire and The Humber
East Midlands
West Midlands
East of England
London
South East
South West
Scotland
Wales
Northern Ireland
Unknown
```

region_key 来源优先级：

1. 若已有 LSOA/DZ -> region lookup，则使用该 lookup。
2. England: 使用 LSOA -> LAD -> Region lookup，或 ONSPD 中的区域字段。
3. Scotland / Wales / Northern Ireland：按 LSOA/DZ code prefix 或 country lookup 归为对应 country。
4. 如果 lookup 不可用，则退回坐标点所在国家/大区的空间查找。
5. 仍无法判断则设为 `Unknown`，并写入 diagnostics。

代码中必须记录实际使用的 lookup 路径或 fallback 方法。

## 1.5 区域支配保护

设置安全网：

```python
MAX_REGION_SAMPLE_SHARE = 0.35
```

但注意：正式模式下抽样目标是保留真实 duty 分布，不主动拉平区域。35% 上限只是异常保护。如果真实 duty 分布中某一区域本身接近该比例，只有超过上限时才触发诊断和可选 cap。

如果某一区域被 cap，必须在 `block_sample_diagnostics.parquet` 和 `run_summary.md` 中记录：

```text
region_key
available_share
sample_share
is_capped
cap_reason
```

## 1.6 sample_weight 定义

`sample_weight` 必须定义，但默认只用于诊断，不用于主输出加权。

定义：

```text
sample_weight = n_available_in_stratum / n_sampled_in_stratum
```

用途：

1. 诊断 sampled blocks 是否覆盖原始 duty 分布。
2. 可选地支持未来 `national_block_expansion` 模式。
3. 默认不用于 `depot_load_15min` 主输出，因为本任务估计的是当前 EV bus stock 情景负荷，而不是全英国所有 bus block 的完全电动化总负荷。

若未来启用加权输出，必须显式参数：

```text
--weighting-mode national_block_expansion
```

且所有 weighted outputs 必须与 unweighted scenario outputs 分文件保存，避免混淆。

## 1.7 随机性

所有抽样必须可复现：

```python
RNG_SEED = 20260603
```

## 1.8 输出

```text
outputs/bus_depot_only_sample/sampled_blocks.parquet
outputs/bus_depot_only_sample/block_sample_diagnostics.parquet
```

`sampled_blocks.parquet` 字段至少包括：

```text
sampled_block_id
block_template_id
block_id
agency_id
region_key
block_source
start_time
end_time
duration_h
passenger_distance_km
start_lsoa
end_lsoa
start_lat
start_lon
end_lat
end_lon
sample_weight
stratum_id
sample_mode
```

`block_sample_diagnostics.parquet` 字段至少包括：

```text
stratum_id
region_key
distance_bin
duration_bin
n_available_blocks
n_sampled_blocks
available_share
sample_share
sample_weight
is_capped
cap_reason
```

## 1.9 测试

新增：

```text
tests/mobility/bus/test_depot_only_block_sampling.py
```

测试：

```text
test_samples_exactly_valid_ev_count_in_full_mode
test_pilot_mode_samples_requested_number
test_sampling_is_without_replacement
test_sampling_is_deterministic
test_region_key_uses_gor_country_level
test_sample_weight_defined_as_inverse_sampling_probability
test_region_dominance_guard_is_safety_net_not_default_balancer
test_sample_diagnostics_written
```

---

# Stage 2 - Operational Depot 推断

## 2.1 目标

为每个 sampled block 推断一个 depot-only charging anchor。

这里的 depot 不是经过验证的真实物理车库，而是：

```text
operational_depot_lsoa = block 自身 terminal/end LSOA 推断出的 charging anchor
```

## 2.2 禁止的做法

不要使用：

```text
全国 LSOA 众数
stop clustering
一个全国虚拟 depot
OCM charger location
public charger location
EV source_lsoa 作为 depot
让 London 高频车次吸收全国 depot
```

## 2.3 block-level depot 推断规则

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

推荐优先级：

1. 如果 `start_lsoa == end_lsoa`，且二者非空，则将其作为 `operational_depot_lsoa`，标记为闭环 block。
2. 如果 trip-level terminal / layover LSOA 有明确唯一众数，则使用该众数。
3. 如果只有 start/end 两个 LSOA 且不相等，则退化为 `end_lsoa`，但 confidence 必须为 low，并记录 `depot_inference_method = "end_lsoa_fallback"`。
4. 如果众数并列，使用 tie-break 规则。
5. 如果没有可用 LSOA，进入 fallback。

这意味着每个 block 独立推断 depot，不能对全国所有 block 求全局众数。

## 2.4 tie-break 规则

如果众数并列：

1. 优先选择 block 最终 `end_lsoa`。
2. 如果仍并列，优先选择最长 dwell / terminal time 对应的 LSOA。
3. 如果没有 dwell 信息，则选择字典序最小的 LSOA，保证确定性。
4. 记录 `depot_tie_resolved=True`。

## 2.5 fallback

如果 block 没有可用 LSOA：

1. 尝试用最终 stop 坐标匹配最近 LSOA。
2. 如果仍失败，则：

```text
operational_depot_lsoa = NaN
depot_confidence = "missing"
manual_review_flag = True
```

这些 block 默认不进入最终 simulation cases，但必须写入 dropped diagnostics。

## 2.6 depot_id 定义

为了避免不同运营商或区域的同一 LSOA 被错误混合：

```python
depot_id = f"opdepot_{agency_id}_{operational_depot_lsoa}"
```

## 2.7 depot confidence

```text
high:
    start_lsoa == end_lsoa，说明 block 在 LSOA 层面闭合；
    或 trip-level terminal/layover LSOA 有唯一众数，且该众数不是单纯由最终 end_lsoa 决定。

medium:
    有多个 terminal/end LSOA 候选，众数存在但不闭合；
    或众数并列后通过 end_lsoa / dwell tie-break 解决。

low:
    只有 start_lsoa 和 end_lsoa，且二者不同，因此 operational_depot_lsoa 退化为 end_lsoa；
    或通过坐标 fallback 得到 LSOA。

missing:
    无法推断 LSOA。
```

## 2.8 depot 坐标

坐标只用于 deadhead 和负荷展示，不要声称是精确车库地址。

优先级：

1. 使用该 block 在 modal LSOA 内 terminal/end stop 坐标的中位数。
2. 如果没有，使用 LSOA centroid。
3. 如果仍没有，lat/lon 设为 NaN，`manual_review_flag=True`。

## 2.9 重要限制说明

必须写入 `run_summary.md`：

1. `operational_depot_lsoa` 是 charging anchor，不是真实 garage。
2. 当 depot 由 terminal/end LSOA 推断时，模型可能低估真实 depot-to-route 和 route-to-depot deadhead。
3. 非闭环 block 中，如果 `operational_depot_lsoa = end_lsoa`，则早间 deadhead 近似为 `end -> start`，晚间 return deadhead 可能接近 0。这是 anchor 假设的副作用，不代表真实车库调度。
4. 报表和图中不得使用 `real_depot`、`verified_garage`、`true_depot_location` 等命名。

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
depot_inference_method
n_sampled_blocks
n_simulation_cases
manual_review_flag
```

其中：

```text
depot_source = "block_terminal_lsoa_anchor"
```

## 2.11 测试

新增：

```text
tests/mobility/bus/test_operational_depot_inference.py
```

测试：

```text
test_depot_lsoa_uses_block_level_signal_not_global_mode
test_closed_block_gets_high_confidence
test_end_lsoa_fallback_gets_low_confidence
test_tie_prefers_final_end_lsoa
test_missing_lsoa_sets_manual_review_flag
test_depot_id_includes_agency_and_lsoa
test_london_does_not_absorb_non_london_blocks
test_run_summary_mentions_depot_anchor_deadhead_limitation
```

---

# Stage 3 - EV bus 实例构建

## 3.1 目标

EV inventory 已是逐辆一行；将所有有效 electric bus/minibus 行作为 vehicle instances，不展开、不复制、不按 `count` 加权。

## 3.2 车辆过滤

只保留：

```python
vehicle_subtype in {"bus", "minibus"}
```

不要包含 coach。

## 3.3 vehicle instance 构建（不要 count 展开）

本数据每行就是一辆车（`EV_ID` 唯一），不需要、也不允许按 `count` 展开。直接把每个有效行当作一个 vehicle instance：

```text
vehicle_id = 该行的 EV_ID（如 bus_1468）
```

`count` 是该 `(LSOA, Model)` 组的车辆数、被抄在组内每一行（组内恒定），仅供审计；不要 sum、不要据此复制行。

反例（错误做法，禁止）：把 `count=93` 的行复制成 93 份。因为该组本就有 93 行，复制后变成 93×93=8,649 辆幽灵车，并把伦敦等大组平方级放大。

每个 vehicle instance 只能使用一次。

## 3.4 vehicle source_lsoa 的角色

`source_lsoa` 只保留为 audit 字段，不参与默认车辆到 block 的匹配，不参与 depot 空间分布。

原因：当前任务中 depot 由 block 决定，车辆只是技术参数捐赠者。使用 EV 登记地理去匹配 block 会把登记数据偏差引入 depot load。

若未来实验性启用 source_lsoa 匹配，必须通过显式参数：

```text
--use-vehicle-geography
```

默认关闭，并且 `run_summary.md` 必须标注偏置风险。

## 3.5 输出

```text
outputs/bus_depot_only_sample/ev_bus_instances.parquet
outputs/bus_depot_only_sample/invalid_vehicle_rows.parquet
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

其中：

```text
vehicle_instance_weight = 1.0
```

## 3.6 测试

新增：

```text
tests/mobility/bus/test_ev_bus_instances.py
```

测试：

```text
test_each_row_is_one_vehicle_instance_no_count_expansion
test_each_vehicle_instance_id_is_unique
test_invalid_vehicle_rows_are_dropped
test_consumption_unit_conversion
test_source_lsoa_is_audit_only_by_default
```

---

# Stage 4 - EV bus instances 与 sampled blocks 一对一配对

## 4.1 目标

将每个有效 EV bus instance 与一个 sampled block template 配对，形成 simulation cases。

正式模式下：

```text
n_simulation_cases = n_valid_ev_bus_instances = n_sampled_blocks
```

每个 vehicle instance 使用一次，每个 sampled block 默认使用一次。

## 4.2 默认配对规则

默认配对不使用 vehicle `source_lsoa`。

推荐规则：

1. 对 `ev_bus_instances` 使用确定性随机种子打乱顺序。
2. 对 `sampled_blocks` 使用确定性随机种子打乱顺序。
3. 按行号一对一配对。
4. 如果因为 dropped blocks / missing depot 导致 sampled block 数少于 vehicle 数，则重新补抽 block templates；不要把多辆车塞到同一个 block，除非显式进入 pilot 模式。
5. 如果 pilot 模式中 vehicle 数大于 sampled block 数，可以 round-robin，但必须在 `run_summary.md` 标记该结果为 pilot/debug，不作为正式情景负荷。

可选技术参数分层：

```text
vehicle_subtype
battery_bin
ac_power_bin
```

但不得默认使用 `source_lsoa` 分层。

## 4.3 输出

```text
outputs/bus_depot_only_sample/simulation_cases.parquet
outputs/bus_depot_only_sample/vehicle_assignment_diagnostics.parquet
```

`simulation_cases.parquet` 字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_template_id
block_id
agency_id
region_key
depot_id
operational_depot_lsoa
assignment_method
case_status
drop_reason
sample_mode
```

`assignment_method` 默认：

```text
deterministic_random_pairing_no_vehicle_geography
```

## 4.4 测试

新增：

```text
tests/mobility/bus/test_depot_only_assignment.py
```

测试：

```text
test_simulation_case_count_equals_valid_vehicle_instance_count_in_full_mode
test_each_vehicle_instance_used_once
test_each_sampled_block_used_once_in_full_mode
test_assignment_is_deterministic
test_assignment_does_not_use_source_lsoa_by_default
test_pilot_mode_round_robin_is_marked_as_pilot_only
```

---

# Stage 5 - Depot-only Event Ledger

## 5.1 目标

为每个 simulation case 构建事件账本。

## 5.2 最小事件序列

每个 case 至少包含：

```text
depot_parking_pre
depot_to_block_deadhead
passenger_block
block_to_depot_deadhead
depot_parking_post
```

如果 block 内部 trip-level 信息可用，应尽量把 `passenger_block` 拆成多个 `passenger_trip` 事件，并显式生成 trip 间 layover。

如果当前 block 只有 aggregate 信息，一个 `passenger_block` 事件也可以接受，但必须在 `run_summary.md` 中记录：

```text
passenger service represented as aggregate block event; midday depot charging windows may be under-detected
```

## 5.3 日间 depot 充电窗口

如果 trip-level 信息可用，并且 block 中段存在 layover 满足：

```text
layover_lsoa == operational_depot_lsoa
duration_min >= 30
```

则插入：

```text
depot_parking_midday
```

该事件允许 depot charging。

如果只有 aggregate block，没有 trip-level layover，则不插入日间 depot charging window，并在 `run_summary.md` 中说明。

## 5.4 deadhead 假设

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

注意：当 `operational_depot_lsoa` 退化为 `end_lsoa` 时，晚间 `block_end -> depot` deadhead 可能接近 0，而早间 `depot -> block_start` 可能近似为 `end -> start`。这必须作为 anchor 假设限制写入 summary。

## 5.5 时间处理与跨午夜过夜充电

如果 block 的 start/end time 可用，则：

1. `depot_parking_pre` 从 service day 00:00 到 depot deadhead 出发前。
2. `depot_to_block_deadhead` 在 passenger block 开始前结束。
3. `passenger_block` 或 `passenger_trip` 使用 block/trip start/end time。
4. `block_to_depot_deadhead` 从 block end 后开始。
5. `depot_parking_post` 从回到 depot 后延伸到下一次出车前的 proxy time，而不是硬截断到当天 24:00。

正式规则：

```text
depot_parking_post_start = return_to_depot_time
depot_parking_post_end   = next_day 06:00 local time
```

如果 block end 已经超过 next day 06:00，则：

```text
depot_parking_post_end = return_to_depot_time + 6 hours
```

并记录 `overnight_window_method = "fallback_6h_after_return"`。

15 分钟负荷聚合必须允许跨午夜，并把能量分配到正确的 `slot_start / slot_end`。

不要把 overnight charging 简单截断到 24:00。

## 5.6 禁止事件

在本 pipeline 中不得出现：

```text
public_charger_event
opportunity_charging
terminal_public_charging
OCM_station_event
```

## 5.7 输出

```text
outputs/bus_depot_only_sample/vehicle_day_events.parquet
```

字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_template_id
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
overnight_window_method
```

Stage 5 可以先写出未填 SOC 的 event ledger；Stage 6 再补充 `charge_*` 与 `soc_*` 字段。

## 5.8 测试

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
test_depot_parking_post_extends_past_midnight_when_needed
test_overnight_charging_not_truncated_at_24h
test_midday_depot_parking_inserted_when_trip_layover_at_depot_lsoa
```

---

# Stage 6 - Depot-only SOC Walk 与 Feasibility

## 6.1 目标

计算每个 simulation case 的 SOC 轨迹、depot charging、energy shortfall 和 depot-only feasibility。

## 6.2 初始 SOC

默认：

```python
initial_soc_kwh = battery_kwh * usable_soc_max
```

注意：在单代表日模型中，`depot_parking_pre` 通常因为初始 SOC 已经满到 usable upper bound 而加不了电。该事实不是 bug，但必须在 `run_summary.md` 的 modelling notes 中说明。

## 6.3 行驶扣电

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

## 6.4 depot charging

只允许以下事件充电：

```text
depot_parking_pre
depot_parking_midday
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

## 6.5 depot_power_kw

本版本默认使用固定 depot power：

```text
depot_power_kw = 100.0
```

实际车辆充电功率仍受车辆 AC 限制：

```python
effective_charge_kw = min(vehicle.ac_charge_kw_max, 100.0)
```

记录：

```text
depot_power_kw = 100.0
depot_power_source = "fixed_default_100kw"
```

不要默认使用 assigned vehicles 的 AC 中位数作为 depot power，因为 vehicle-to-block pairing 是情景分配，不应反过来内生决定 depot 基础设施功率。

如果未来需要测试 depot power sensitivity，应通过显式参数：

```text
--depot-power-kw 50|100|150
```

并在 run_summary 中记录。

## 6.6 不要 clamp SOC

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

## 6.7 infeasibility reason

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
midday_depletion_no_depot_window
insufficient_depot_charging_time
missing_depot_lsoa
invalid_vehicle_parameters
unknown
```

reason 说明：

1. `midday_depletion_no_depot_window` 只有在 trip-level event ledger 能判断日间缺少 depot window 时才使用。
2. `insufficient_depot_charging_time` 只有在存在 depot charging windows 但可充电时长不足时使用。
3. aggregate block 模式下无法可靠识别日间 depot 回库，应优先使用 `daily_energy_exceeds_usable_battery` 或 `unknown`，并在 summary 中说明限制。

## 6.8 输出

```text
outputs/bus_depot_only_sample/vehicle_day_events.parquet
outputs/bus_depot_only_sample/case_soc_summary.parquet
```

`case_soc_summary.parquet` 字段至少包括：

```text
simulation_case_id
vehicle_id
sampled_block_id
block_template_id
block_id
depot_id
operational_depot_lsoa
battery_kwh
consumption_kwh_per_km
ac_charge_kw_max
depot_power_kw
depot_power_source
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

## 6.9 测试

新增：

```text
tests/mobility/bus/test_depot_only_soc.py
```

测试：

```text
test_no_public_charging_used
test_depot_charging_uses_ac_power
test_fixed_default_depot_power_used_by_default
test_soc_not_clamped_below_zero
test_infeasible_case_reports_shortfall
test_feasible_case_has_no_shortfall
test_midday_depot_window_can_reduce_shortfall
test_case_soc_summary_has_required_columns
```

---

# Stage 7 - Depot 15min Load Aggregation 与 Run Summary

## 7.1 目标

将 depot charging energy 分配到 15 分钟时间槽，并生成 run summary。

## 7.2 15 分钟负荷聚合

只聚合 depot charging，不聚合公共桩。

输出：

```text
outputs/bus_depot_only_sample/depot_load_15min.parquet
```

字段至少包括：

```text
depot_id
operational_depot_lsoa
region_key
time_slot
slot_start
slot_end
charge_kwh
average_kw
n_active_cases
sample_mode
weighting_mode
```

要求：

1. `depot_load_15min.charge_kwh` 总和应与 event ledger 中 depot charging 总量一致，允许极小浮点误差。
2. 不出现 public station id。
3. 不依赖 OCM。
4. 必须支持跨午夜 charging slots。
5. 默认 `weighting_mode = "unweighted_ev_stock_scenario"`。
6. 默认不使用 `sample_weight` 加权 `depot_load_15min`。

## 7.3 sample_weight 与输出解释

默认主输出解释为：

```text
unweighted_ev_stock_scenario:
    当前 EV bus stock 数量下的一组代表性 depot-only duty 情景负荷。
```

不得把默认 `depot_load_15min` 解释为：

```text
全英国所有 bus block 完全电动化后的总负荷
```

如果将来实现 weighted national expansion，必须输出独立文件，例如：

```text
depot_load_15min_weighted_national_expansion.parquet
```

并在 `run_summary.md` 中明确区分。

## 7.4 run_summary.md

输出：

```text
outputs/bus_depot_only_sample/run_summary.md
```

必须包含：

```text
n_trip_rows_raw
n_block_templates_available
n_blocks_sampled
n_ev_instances_available
n_simulation_cases_created
n_cases_successful
n_cases_dropped
sample_mode
weighting_mode
n_depots_inferred
depot_confidence_distribution
region_sample_distribution
vehicle_model_distribution
vehicle_subtype_distribution
minibus_count_note
depot_only_feasible_share
top_10_depots_by_charge_kwh
top_10_blocks_by_energy_shortfall
主要建模假设
主要数据质量问题
主要限制
```

主要限制必须包括：

1. depot 是 operational charging anchor，不是真实 garage。
2. depot=end_lsoa fallback 可能低估 return deadhead，并使早晚 deadhead 不对称。
3. 单代表日模型没有多日 SOC warm-up / carry-over。
4. 初始 SOC=usable upper bound 使 pre-service charging 多数情况下为空操作。
5. 若 aggregate block 模式被使用，日间 depot charging windows 可能被低估。
6. 默认输出不是全国所有 bus 完全电动化总量。

## 7.5 测试

新增：

```text
tests/mobility/bus/test_depot_only_outputs.py
```

测试：

```text
test_depot_load_has_only_depot_lsoas
test_depot_load_energy_matches_event_ledger
test_depot_load_supports_cross_midnight_slots
test_run_summary_is_written
test_run_summary_contains_estimand
test_run_summary_contains_key_assumptions
test_run_summary_distinguishes_unweighted_ev_stock_scenario_from_national_expansion
```

---

## 8. 新增脚本

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
  --sample-mode full_ev_inventory \
  --seed 20260603 \
  --charging-mode depot_only \
  --depot-power-kw 100
```

pilot/debug 示例：

```bash
python scripts/run_bus_depot_only_sample.py \
  --blocks outputs/all_blocks.parquet \
  --ev-inventory data/EV_UK_LSOA_2025_with_energy.csv \
  --out-dir outputs/bus_depot_only_sample_pilot \
  --sample-mode pilot \
  --n-blocks 1200 \
  --seed 20260603 \
  --charging-mode depot_only \
  --depot-power-kw 100
```

注意：不要再使用 `--vehicle-draws-per-block 5` 作为默认核心参数。如果保留该参数用于实验，也必须默认关闭，并且不能影响本任务的主路径。

---

## 9. 推荐新增模块

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

## 10. 总输出清单

所有输出写入：

```text
outputs/bus_depot_only_sample/
```

必须输出：

```text
preflight_summary.json
preflight_summary.md
block_templates.parquet
lsoa_attach_diagnostics.parquet
sampled_blocks.parquet
block_sample_diagnostics.parquet
operational_depot_registry.parquet
depot_inference_diagnostics.parquet
ev_bus_instances.parquet
invalid_vehicle_rows.parquet
simulation_cases.parquet
vehicle_assignment_diagnostics.parquet
vehicle_day_events.parquet
case_soc_summary.parquet
depot_load_15min.parquet
run_summary.md
```

---

## 11. 明确不要做的事情

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
全英国所有 bus 完全电动化加权扩展
coach 仿真修改
private car 仿真修改
OSRM / 路网绕行
温度修正
随机延误传播
Monte Carlo timetable delay
原 M1 L0-L4 resolution cascade
按 count 展开 EV rows
使用 EV source_lsoa 默认匹配 block
```

本任务只做：

```text
当前 EV bus stock 规模 + representative sampled bus duties + depot-only charging
```

---

## 12. Acceptance Criteria

任务完成标准：

1. 新脚本 `scripts/run_bus_depot_only_sample.py` 可以端到端运行。
2. Stage 0 可以从 trip-level `all_blocks.parquet` 构建 block templates。
3. Stage 0 可以完成 start/end LSOA attach 并报告命中率。
4. 正式模式默认 `sample_mode=full_ev_inventory`。
5. 正式模式 sampled block 数量等于有效 EV bus instance 数量。
6. pilot 模式可指定 `--n-blocks 1200`，但输出必须标记为 pilot/debug。
7. block 抽样是 without replacement。
8. 抽样具有确定性。
9. region_key 使用 GOR/country 级别。
10. `sample_weight` 被定义为 stratum inverse sampling probability，但默认不用于主输出加权。
11. EV inventory 中每个有效 bus/minibus 行直接作为一个 vehicle instance，不做 count 展开；`count` 仅审计。
12. 每个 EV bus instance 最多使用一次。
13. 正式模式下每个 sampled block 默认最多使用一次。
14. simulation case 数量等于有效 EV bus instance 数量，不是 `sum(count)`、也不是 `1200*5`。
15. vehicle `source_lsoa` 默认不参与 block 匹配。
16. depot 使用 block-level terminal/end LSOA 信号推断。
17. depot 众数绝不能是全国全局众数。
18. 闭环 block 得到 high confidence；end_lsoa fallback 得到 low confidence。
19. 非 London block 不会因为 London 是全局众数而被分配到 London depot。
20. 不使用公共充电。
21. 不依赖 OCM 数据。
22. 每个 simulation case 都有 event ledger。
23. event ledger 支持跨午夜 depot_parking_post，不截断到 24:00。
24. 如果 trip-level layover 显示车辆位于 depot_lsoa 且 dwell >= 30min，则生成 depot_parking_midday。
25. 每个 event ledger 都有 `soc_start_kwh` 和 `soc_end_kwh`。
26. SOC 不 clamp 到 0。
27. depot-only infeasible cases 被保留并明确标记。
28. depot_power_kw 默认固定为 100kW，effective power 仍受 vehicle AC 限制。
29. 输出 `depot_load_15min.parquet`。
30. `depot_load_15min` 支持跨午夜 slots。
31. 输出 `run_summary.md`。
32. run_summary 明确 estimand：当前 EV bus stock 情景负荷，而非全国完全电动化总量。
33. run_summary 明确 depot 是 operational charging anchor，不是真实物理车库。
34. run_summary 明确 depot=end_lsoa fallback 对 deadhead 的限制。
35. run_summary 报告 minibus 是否为空。
36. preflight 报告低电耗异常丢弃数量。
37. 所有新增测试通过。
38. 不破坏现有 bus/coach/car 其他测试。
39. notebook 不写 parquet/csv。
40. 代码和输出中都必须避免把 operational depot 称为 verified garage / real depot。

---

## 13. 给代码 agent 的最后提醒

这个任务不是要估计英国真实 bus depot 的精确位置，也不是要模拟全英国所有 bus 全部电动化后的总负荷。

本任务中的 depot 是：

```text
operational_depot_lsoa
```

它来自：

```text
block 自身 terminal/end LSOA 信号
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
block_terminal_lsoa_anchor
depot_confidence
manual_review_flag
```

核心建模假设必须写进代码注释和 `run_summary.md`：

> 本 pipeline 使用 EV inventory 中所有有效 bus/minibus 车辆实例作为当前 EV bus stock。正式模式下，从全国 bus block templates 中按真实 duty 分布抽取同等数量的 representative sampled blocks，并将每个 EV bus instance 作为技术参数捐赠者与一个 sampled block 配对。车辆只在根据该 block terminal/end LSOA 信号推断出的 operational depot charging anchor 充电。本模型用于分析当前 EV bus stock 规模下 depot-only 情景的 SOC、能耗、充电负荷和不可行性，不声称推断出真实物理 depot，也不输出全国所有 bus 完全电动化总量。
