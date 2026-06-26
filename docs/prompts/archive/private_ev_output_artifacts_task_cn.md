# 私家车 EV 仿真代码检查后的下一步修改任务

## 任务目标

本任务的目标不是重写当前私家车 EV 仿真的核心行为逻辑，而是在接受当前若干模型简化假设的基础上，补全最终输出 artifact，使仿真结果能够更好地支持后续并网分析、个体级行为分析、充电失败分析和 SOC 轨迹分析。

当前代码中有一些实现逻辑与原始 MD 说明不同，但经过讨论，这些差异暂时可以接受，不应在本任务中修改。

本任务的重点是：

> 补全 simulation observability，即将个体级 trip、home charging、failed public charging、successful public charging 以及 SOC 状态字段完整输出到最终 artifact 中。

---

# 1. 已接受的实现差异

以下差异虽然与原始 MD 说明不同，但目前被认为是可以接受的模型选择，不应在本任务中修改。

## 1.1 暂时不启用相邻 LSOA 公共充电回退

原始 MD 说明中写的是：

```text
当前 LSOA 优先，其次相邻 LSOA
```

也就是说，如果 EV 当前所在 LSOA 没有公共充电站，则应继续搜索相邻 LSOA 的公共充电站。

但当前主流程中，如果当前 LSOA 没有公共充电站，会直接设置：

```text
can_charge = False
```

这个逻辑暂时可以接受，因为它更加保守，不假设车辆一定愿意跨 LSOA 去寻找公共充电站。

因此：

```text
本任务中不要启用 neighboring LSOA public charging fallback。
```

## 1.2 暂时不重写 holiday week 行为逻辑

原始 MD 说明中希望 holiday week 体现：

```text
部分 work / commute / business / education 行为被替换为 leisure / shopping
所有出行整体推迟
保留 original purpose -> final purpose 的可追踪字段
```

当前代码实现是：

```text
使用硬编码 holiday table
使用硬编码 1 小时延迟
随机丢弃部分 work / education trip
额外注入 leisure / holiday trip
```

虽然这与原始 MD 不完全一致，但也体现了节假日减少通勤、增加休闲活动、整体出行推迟的方向。

因此：

```text
本任务中不要重写 holiday week behavior model。
```

## 1.3 暂时接受 trip jitter 后重新排序

当前代码对每个 trip 独立进行时间扰动，然后按扰动后的 departure time 重新排序。

这可能导致原始 NTS `JourSeq` 顺序在少数情况下发生变化。

经过讨论，这个问题暂时可以接受，因为：

- 时间扰动后重新排序可以保证时间轴自身一致；
- 如果两个 trip 原本时间非常接近，轻微顺序变化在当前行为模型中可以接受；
- 当前阶段不需要为了保持 `JourSeq` 而重写 jitter 逻辑。

因此：

```text
本任务中不要修改当前 trip jitter 和 re-sorting 逻辑。
```

## 1.4 暂时接受当前公共充电站打分逻辑

原始 MD 中希望公共充电站选择综合考虑：

```text
connector 数量
charging power
站点到当前 LSOA centroid 的距离
```

当前代码主要使用：

```text
station_attractiveness = log1p(TotalCapacity_kW)
```

并结合 LSOA 距离衰减。

虽然当前逻辑没有显式使用 connector count，也没有使用真实 station-to-centroid distance，但 `TotalCapacity_kW` 已经部分反映充电能力。

因此：

```text
本任务中不要修改 public charging station scoring 逻辑。
```

---

# 2. 本任务必须优先修改的问题

## 2.1 当前最终 artifact 缺少完整个体级输出

当前代码中存在以下问题：

```text
failed public charging 只在内存中出现，没有完整落盘；
home charging 没有作为明确事件完整输出；
最终 station-curve export 主要输出成功 public charging sessions / bins；
缺少完整 trip-level records；
缺少 SOC before / SOC after；
缺少 holiday 相关追踪字段；
缺少 unified charging event table。
```

这会影响后续分析，包括：

- 并网负荷曲线；
- home charging 与 public charging 的比例；
- failed public charging 的空间分布；
- failed public charging 的时间分布；
- 每辆 EV 的 SOC 轨迹；
- 每辆 EV 的个体级出行行为；
- holiday week 对出行和充电的影响；
- LSOA × time interval 的聚合分析。

因此，当前最重要的修改方向是：

```text
在不改变核心行为模型的前提下，补全最终输出 artifact。
```

---

# 3. 需要新增或暴露的输出 artifact

## 3.1 failed public charging records

当 EV 需要公共充电，但当前 LSOA 没有可用公共充电站时，不应只在内存中设置 `can_charge=False` 后丢弃。

应将这次失败的公共充电尝试写入最终 artifact。

### 预期逻辑

```text
if EV needs public charging
and EV.current_lsoa != EV.home_lsoa
and no public station exists in current_lsoa:
    create failed_public_charging record
```

### 建议字段

```text
ev_id
person_id
simulation_week
date
charging_attempt_time
current_lsoa
home_lsoa
charging_type
can_charge
station_id
reason
soc_before_attempt
soc_after_attempt
holiday_week
```

其中：

```text
charging_type = "failed_public_charging"
can_charge = False
station_id = null
reason = "no_public_station_in_current_lsoa"
```

如果某些字段在当前代码中暂时不可得，应在代码中明确说明原因，并尽量保留可得字段。

## 3.2 home charging records

Home charging 不应只体现在 SOC 状态变化中，而应该作为明确的 charging event 输出。

### 预期逻辑

```text
if EV needs charging
and EV.current_lsoa == EV.home_lsoa:
    create home charging event
```

### 建议字段

```text
ev_id
person_id
simulation_week
date
charging_start_time
charging_end_time
charging_lsoa
home_lsoa
charging_type
station_id
charging_power_kw
charged_energy_kwh
soc_before_charging
soc_after_charging
holiday_week
```

其中：

```text
charging_type = "home"
station_id = null
charging_lsoa = home_lsoa
```

## 3.3 individual trip records

最终 artifact 应包含个体级 trip-level records，而不仅仅是聚合后的 station curve。

每一条 trip record 应能追踪：

- 哪辆 EV；
- 哪个 person；
- 从哪里到哪里；
- 什么目的；
- 什么时间；
- 行驶多远；
- 消耗多少电；
- trip 前后 SOC 如何变化；
- 是否属于 holiday week；
- 是否被 holiday rule 修改过。

### 建议字段

```text
ev_id
person_id
trip_id
trip_sequence_id
simulation_week
date
day_of_week
origin_lsoa
destination_lsoa
purpose_original
purpose_final
departure_time
arrival_time
distance_km
energy_consumed_kwh
soc_before_trip
soc_after_trip
holiday_week
is_holiday_modified
```

说明：

- `purpose_original` 表示原始 NTS trip purpose；
- `purpose_final` 表示经过 holiday rule 或其他行为扰动后的最终 purpose；
- 如果当前代码暂时没有 original/final purpose 的完整映射，也应尽量输出当前可用的 purpose 字段；
- `soc_before_trip` 和 `soc_after_trip` 对后续 SOC 轨迹分析非常重要。

## 3.4 unified charging event records

建议新增一个统一的 charging event artifact，将所有 charging-related events 放在同一张表中。

建议文件名：

```text
private_car_charging_events.parquet
```

该表应包含：

```text
successful public charging
home charging
failed public charging
```

### 建议字段

```text
ev_id
person_id
event_id
simulation_week
date
charging_start_time
charging_end_time
charging_lsoa
home_lsoa
charging_type
can_charge
station_id
charging_power_kw
charged_energy_kwh
soc_before_charging
soc_after_charging
reason
holiday_week
```

### charging_type 允许值

```text
home
public_current_lsoa
failed_public_charging
```

如果未来启用相邻 LSOA 回退，可以再扩展为：

```text
public_neighbor_lsoa
```

但本任务暂时不要启用该行为。

---

# 4. 需要保留的现有输出

本任务应是 additive change，不应破坏现有 station-curve pipeline。

也就是说：

```text
现有 successful public charging sessions 输出应继续存在；
现有 station bins / station curves 输出应继续存在；
现有测试应继续通过；
新增 artifact 不应改变核心模拟结果；
新增 artifact 应尽量从已有中间状态中导出。
```

除非必须重构，否则不要删除或改名现有输出文件。

---

# 5. 推荐的输出文件

建议最终至少输出以下文件：

```text
private_car_trip_records.parquet
private_car_charging_events.parquet
```

可选输出：

```text
private_car_failed_charging_events.parquet
private_car_home_charging_events.parquet
private_car_ev_state_records.parquet
```

如果已经有类似文件名，可以复用现有命名，但需要保证字段完整且语义清晰。

---

# 6. SOC 字段要求

所有 trip 和 charging event 应尽量包含 SOC before / after 字段。

## 6.1 Trip records 中的 SOC

```text
soc_before_trip
soc_after_trip
energy_consumed_kwh
```

应满足：

```text
soc_after_trip <= soc_before_trip
```

除非存在特殊逻辑，例如 regenerative braking，但当前私家车模型一般不需要考虑。

## 6.2 Charging records 中的 SOC

```text
soc_before_charging
soc_after_charging
charged_energy_kwh
```

应满足：

```text
soc_after_charging >= soc_before_charging
```

如果是 failed public charging：

```text
soc_after_charging == soc_before_charging
charged_energy_kwh = 0
can_charge = False
```

或者如果字段命名为 attempt：

```text
soc_after_attempt == soc_before_attempt
```

---

# 7. Holiday 字段要求

由于当前 holiday week 实现暂时接受，不要求重写其行为逻辑。

但如果 holiday 信息已经在代码中可用，应尽量在输出中加入以下字段：

```text
holiday_week
is_holiday_modified
purpose_original
purpose_final
holiday_rule_applied
```

如果当前代码没有完整的 original-to-final purpose tracking，则不要为了这个字段大规模重写 holiday logic。

可以采用最小实现：

```text
holiday_week = True / False
is_holiday_modified = True / False / null
purpose_original = available original purpose if exists
purpose_final = final simulated purpose if exists
```

---

# 8. 测试要求

请新增或修改测试，验证新增 artifact 正确输出。

## 8.1 failed public charging 测试

构造一个 EV：

```text
current_lsoa = LSOA_X
home_lsoa != LSOA_X
SOC 低于充电阈值
LSOA_X 没有公共充电站
```

预期：

```text
生成 failed_public_charging event
can_charge = False
station_id = null
reason = "no_public_station_in_current_lsoa"
charged_energy_kwh = 0 或 null
SOC 不增加
```

## 8.2 home charging 测试

构造一个 EV：

```text
current_lsoa = home_lsoa
SOC 低于充电阈值
home_lsoa 没有公共充电站
```

预期：

```text
生成 home charging event
charging_type = "home"
station_id = null
can_charge = True
SOC 增加
```

重点检查：

```text
home charging 不依赖公共 charging station 数据。
```

## 8.3 successful public charging 测试

构造一个 EV：

```text
current_lsoa != home_lsoa
SOC 低于充电阈值
current_lsoa 有公共充电站
```

预期：

```text
生成 public_current_lsoa charging event
can_charge = True
station_id 不为空
charged_energy_kwh > 0
SOC 增加
```

## 8.4 trip-level records 测试

测试 trip records 是否包含以下字段：

```text
ev_id
person_id
origin_lsoa
destination_lsoa
purpose_final
departure_time
arrival_time
distance_km
energy_consumed_kwh
soc_before_trip
soc_after_trip
```

并检查：

```text
每条 trip 都有 ev_id；
trip 后 SOC 不应大于 trip 前 SOC；
origin_lsoa 和 destination_lsoa 不应为空；
departure_time 不应晚于 arrival_time。
```

## 8.5 unified charging event table 测试

测试统一 charging event table 是否包含三类事件：

```text
home
public_current_lsoa
failed_public_charging
```

并检查：

```text
failed_public_charging 的 can_charge = False；
home charging 的 station_id = null；
successful public charging 的 station_id 不为空；
charging_type 只包含允许值。
```

## 8.6 不破坏现有 station-curve 测试

运行现有测试，确保原有 station-curve pipeline 没有被破坏。

例如：

```bash
pytest tests/mobility/cars -q
pytest tests/mobility/stage_1/test_station_sampling.py -q
pytest tests/mobility/stage_2a/test_holiday_rules.py -q
pytest tests/mobility/stage_2b/test_person_fleet.py -q
pytest tests/mobility/stage_2c/test_week_pattern.py -q
pytest tests/mobility/stage_2d/test_assign_year_schedules.py -q
```

最终应保证：

```text
existing tests still pass
new artifact tests pass
```

---

# 9. 实现原则

## 9.1 不要改变已接受的行为模型

本任务不要修改：

```text
neighboring LSOA charging fallback
holiday week logic
trip jitter / resorting logic
station scoring logic
EV-person binding logic
Huff destination sampling logic
```

除非新增 artifact 时确实遇到结构性 blocker。

如果必须修改上述逻辑，请先汇报：

```text
具体 blocker 是什么；
为什么无法只通过导出中间状态解决；
最小必要修改是什么；
修改后是否会改变仿真结果。
```

## 9.2 新增 artifact 应优先从已有中间状态导出

请优先查找现有中间变量、DataFrame 或 internal records，例如：

```text
trip schedules
charging sessions
station matching outputs
SOC update records
failed vehicle records
holiday modified schedules
```

然后将这些信息整理输出。

不要为了新增输出而大规模重写核心仿真。

## 9.3 缺失字段要显式处理

如果某些字段暂时无法获得，不要静默忽略。

应采用以下方式之一：

```text
输出 null，并在代码注释中说明；
添加 reason 字段；
在测试中说明该字段当前允许为空；
提出最小 refactor 建议。
```

例如：

```text
purpose_original 暂时不可得 -> 输出 null
holiday_rule_applied 暂时不可得 -> 输出 null
```

但以下字段应尽量必须有：

```text
ev_id
time
lsoa
charging_type
can_charge
soc_before
soc_after
```

---

# 10. Codex 执行要求

请 Codex 按以下顺序执行：

```text
1. 阅读当前 private-car station-curve pipeline；
2. 找到 trip records、charging sessions、station matching、SOC update 相关中间状态；
3. 判断哪些字段已经可用，哪些字段需要最小改动才能记录；
4. 新增或扩展最终 artifact 输出；
5. 新增测试；
6. 运行相关测试；
7. 汇报修改内容、输出文件路径、字段 schema、测试结果；
8. 如果某些字段无法实现，先汇报 blocker，不要擅自重写核心模型。
```

---

# 11. 最终验收标准

本任务完成后，应满足：

```text
1. 现有 private-car simulation 行为逻辑不被大幅改变；
2. 现有 station-curve outputs 仍然存在；
3. 新增 trip-level artifact；
4. 新增 unified charging event artifact；
5. failed public charging 被明确记录；
6. home charging 被明确记录；
7. successful public charging 仍然被记录；
8. trip records 包含个体级 EV 状态字段；
9. charging records 包含 SOC before / after；
10. 输出能够支持 LSOA × time interval 的并网聚合；
11. 新增测试通过；
12. 现有测试仍然通过。
```

---

# 12. 简短总结

当前不需要急着修改 holiday week、相邻 LSOA fallback、station scoring 或 trip jitter。

当前最值得做的是：

```text
补全最终输出。
```

也就是让模型不仅能生成 station curve，还能完整回答：

```text
哪辆车什么时候去了哪里？
trip 前后 SOC 是多少？
哪辆车什么时候在家充电？
哪辆车什么时候公共充电成功？
哪辆车什么时候公共充电失败？
失败原因是什么？
这些事件发生在哪个 LSOA？
这些事件是否发生在 holiday week？
```

这部分补全后，后续并网分析和模型诊断会更可靠。
