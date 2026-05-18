# 私家车 EV 仿真逻辑说明（Codex 验证版）

请检查当前私家车 EV 仿真代码是否实现了以下仿真逻辑。你可以通过阅读代码、追踪数据流、检查关键函数，或设计单元测试/集成测试来验证代码准确性。

本仿真的目标是：以每一辆私家电动车 EV 为独立个体，基于 NTS 出行链、LSOA 空间单元、POI 面积、充电站数据，以及 holiday week 情景，生成每辆 EV 的一周出行行为和充电行为，最后聚合所有 EV 的结果，为后续电网并网分析提供输入。

请不要只检查代码是否能运行，而是检查它与以下目标仿真逻辑之间的关系。若代码实际实现逻辑与本文不同，不要直接判定为错误，也不要直接修改代码；请先汇报差异，说明差异出现在哪个文件、函数、变量或数据流中，并分析该差异可能带来的影响。因为实际代码可能采用了另一种合理设计，所以需要先与我讨论，再决定是否修改代码。

---

## 0. 仿真核心要求

仿真应满足以下核心原则：

1. 每辆私家 EV 是一个独立个体；
2. 每辆 EV 在仿真初始化后只能绑定一个 NTS person / 出行人；
3. 每辆 EV 每周的 NTS 出行链抽样只能从该 EV 已绑定出行人的 travel-chain pool 中进行，不能跨 person pool 混合抽样；
4. NTS 出行链可以加入随机扰动，但不能破坏 trip 顺序和时间逻辑；
5. holiday week 会改变部分出行目的，并整体推迟出行时间；
6. 非 home 目的地通过 Huff score 按概率抽样；
7. home 目的地必须强制回到该 EV 自己的 home LSOA；
8. 每次 trip 后必须更新 EV 的当前位置、SOC 和时间状态；
9. 充电逻辑区分 home charging 与 public charging；
10. public charging 优先当前 LSOA，其次相邻 LSOA；
11. 充电站选择应体现 connector 多、功率大、距离当前 LSOA centroid 近的偏好；
12. 当前 LSOA 与相邻 LSOA 都无公共充电站时，应记录无法充电事件；
13. 最终输出应包含个体级 trip records 和 charging records；
14. charging output 应能聚合到 `LSOA × time interval × charging power / charging energy`，用于后续并网分析；
15. 所有随机过程应支持固定 random seed 后可复现；
16. 如果代码实现与本文逻辑不同，Codex 应先报告差异和影响，不应直接假设代码错误或直接改代码。

---

## 1. 输入数据

仿真至少应使用以下数据集。

### 1.1 EV 数据集

EV 数据集中每一行代表一辆私家电动车。每辆车至少应包含：

- EV 唯一 ID；
- 车辆参数，例如电池容量、能耗参数、初始 SOC 等；
- 车辆所属的 home LSOA；
- 该 EV 绑定的 NTS person ID / 出行人 ID，例如 `assigned_nts_person_id`，如果该映射不是输入字段，则应在仿真初始化阶段生成并固定；
- 其他与出行或充电相关的车辆属性。

代码应将每辆 EV 作为独立个体进行仿真，而不是只做整体平均或区域级聚合。

### 1.2 NTS trip / tour / travel-chain 数据

NTS 数据用于生成私家车的出行链条。

预期逻辑是：

- NTS 数据中包含一周的出行链条；
- 每个出行链条应能关联到具体出行人或家庭；
- 出行链条中应包含一周内的多个 trip；
- 每个 trip 至少应包含：
  - 出发时间；
  - 到达时间或持续时间；
  - 出行目的；
  - 出行顺序；
  - 如有，则包含距离、出行方式、停留时间等。

代码应使用 NTS 中的周出行链作为 EV 一周出行目的建模的基础，而不是将 NTS 数据拆散成无序单次 trip 后再随机拼接。

此外，NTS 数据应能组织成以 person 为单位的 travel-chain pool：

```text
nts_person_chain_pool[person_id] = [weekly_chain_1, weekly_chain_2, ...]
```

如果真实 NTS 中某个 person 只有一条周链，也可以将该周链通过可控随机扰动生成多个同一 person 的候选链。但这些候选链仍属于该 person 的 pool，不能与其他 person 的链混合。

### 1.3 LSOA 空间数据

LSOA 数据用于表示空间区域。

代码应能获取：

- 每个 LSOA 的唯一 ID；
- 每个 LSOA 的 centroid；
- LSOA 之间的距离；
- 每个 EV 的 home LSOA；
- 当前 EV 所在 LSOA；
- 邻近 LSOA 信息，如果充电逻辑或目的地选择逻辑需要用到。

### 1.4 POI 数据

POI 数据用于目的地吸引力建模。

代码应使用每个 LSOA 内不同类型 POI 的面积或规模信息。

对于不同出行目的，例如 work、shopping、education、leisure 等，应使用与该目的相关的 POI 面积作为目的地吸引力的一部分。

示例映射：

| Trip purpose | 相关 POI 类型示例 |
|---|---|
| shopping | retail / shopping / supermarket |
| education | school / college / university |
| work | office / employment / industrial / business |
| leisure | leisure / recreation / park / entertainment / culture |
| healthcare | hospital / clinic / GP |
| other | 可使用通用 POI 面积或 fallback 规则 |

如果代码中使用了目的与 POI 类型的映射表，应检查该映射是否明确、稳定、可测试。

### 1.5 Charging station 数据

充电站数据用于公共充电行为建模。

每个充电站至少应包含：

- 充电站 ID；
- 所属 LSOA；
- 位置坐标；
- connector 数量；
- 充电功率；
- 其他可选属性，例如可用性、运营状态、收费信息等。

代码应能根据 EV 当前所在 LSOA 以及邻近 LSOA 查询候选充电站。

### 1.6 Holiday week 数据或配置

代码需要支持 holiday week 情景。

holiday week 可以通过以下任一方式定义，但必须在代码中清晰、可测试：

- 显式输入字段，例如 `is_holiday_week = True / False`；
- holiday calendar，例如根据周编号或日期判断；
- scenario 配置，例如 `scenario = "normal_week"` 或 `scenario = "holiday_week"`。

holiday week 不应被写死在某个函数内部，最好由配置或输入参数控制。

---

## 2. 仿真总体流程

仿真应以 EV 为基本单位，而不是以 trip、LSOA 或 aggregate population 为基本单位。

总体流程应为：

```text
for each simulation week:
    determine whether this week is a holiday week

    for each EV in EV dataset:
        1. 根据该 EV 固定绑定的 assigned_nts_person_id，从该 person 的 travel-chain pool 中抽取或生成一周 NTS 出行链
        2. 对出行链加入普通随机扰动
        3. 如果是 holiday week，应用 holiday week 出行链转换
        4. 根据出行链得到该 EV 的一周出行目的序列
        5. 对每个 trip，根据出行目的选择目的地 LSOA
        6. 更新 EV 的位置、行驶距离、SOC 和当前时间
        7. 根据充电触发逻辑判断是否充电
        8. 如果触发充电，则执行 home charging 或 public charging 选择逻辑
        9. 记录该 EV 的 trip 行为和 charging 行为

最后聚合所有 EV 的出行结果和充电结果
```

注意：EV 与 NTS person 的绑定应在仿真初始化阶段确定，并在整个仿真期间保持固定。每周可以重新抽取或扰动出行链，但只能在该 EV 已绑定 person 的 pool 内完成，不能每周给同一辆 EV 重新分配不同的 person。

---

## 3. EV-person 绑定与出行目的建模

### 3.1 基本原则：一辆 EV 只能绑定一个 NTS person

对于每一辆 EV，仿真中必须存在一个固定的出行人绑定关系：

```text
EV.assigned_nts_person_id = one NTS person ID
```

这意味着：

- 一辆 EV 不能同时混合多个 NTS person 的出行链；
- 一辆 EV 不能在 week 1 使用 person A 的链、week 2 又改用 person B 的链；
- 每周可以重新抽取或扰动 weekly chain，但抽样范围必须限制在该 EV 已绑定 person 的 travel-chain pool 内；
- 如果 EV-person 映射由随机过程生成，则该映射应在仿真初始化阶段一次性生成，并在所有 simulation weeks 中保持不变；
- 如果 EV 数据集已经提供 `assigned_nts_person_id`，代码应直接使用该字段，而不是每周重新随机分配 person。

错误示例：

```text
week 1: EV_001 <- NTS_person_A 的出行链
week 2: EV_001 <- NTS_person_B 的出行链
```

正确示例：

```text
initialization:
    EV_001.assigned_nts_person_id = NTS_person_A

week 1:
    EV_001 <- sample from NTS_person_A.chain_pool

week 2:
    EV_001 <- sample from NTS_person_A.chain_pool
```

如果某个 person 只有一条原始周出行链，week 1 与 week 2 可以基于同一条链加入不同随机扰动，但仍然属于同一个 person 的 pool。

### 3.2 Person-specific travel-chain pool

NTS 出行链应以 person 为单位组织。

推荐结构：

```text
nts_person_chain_pool = {
    NTS_person_A: [A_week_chain_1, A_week_chain_2, A_perturbed_chain_1, ...],
    NTS_person_B: [B_week_chain_1, B_week_chain_2, B_perturbed_chain_1, ...],
}
```

对 EV 抽取周链时，应使用：

```text
person_id = EV.assigned_nts_person_id
candidate_chains = nts_person_chain_pool[person_id]
weekly_chain = sample(candidate_chains, rng)
```

不应使用：

```text
weekly_chain = sample(all_nts_persons_all_chains, rng)
```

除非代码有明确设计说明并且你确认这种设计是有意为之；即便如此，也应先将该差异报告给我，而不是直接判定为错误或直接修改。

### 3.3 EV-person 映射的生成与复现

如果 EV 数据没有预先给出 `assigned_nts_person_id`，代码可以在仿真初始化阶段为每辆 EV 分配一个 person。

预期逻辑：

```text
for each EV:
    EV.assigned_nts_person_id = sample_one_person_from_allowed_person_pool(EV, rng)
```

然后在所有 simulation weeks 中复用该绑定关系。

需要检查：

- 每辆 EV 是否有且只有一个 `assigned_nts_person_id`；
- 该 ID 是否在 week-to-week 之间保持固定；
- 每周抽取 weekly chain 时是否只使用该 person 的 pool；
- 固定 random seed 后，EV-person 映射与后续 chain 抽样是否可复现；
- 如果代码允许多个 EV 绑定到同一个 NTS person，是否是明确设计；
- 如果代码要求 EV 与 NTS person 一一对应，是否有测试保证不会重复分配。

### 3.4 EV home LSOA 与 NTS person home 的关系

EV 的 home LSOA 应以 EV 数据集为准。

NTS person 主要提供：

- 一周 trip 顺序；
- trip purpose；
- departure / arrival time 或 duration；
- 停留结构；
- 可选的出行距离或出行行为特征。

除非代码有明确设计说明，否则不应因为绑定了某个 NTS person，就把 EV 的 home LSOA 替换为 NTS person 的 home location。

预期逻辑：

```text
EV.home_lsoa = EV dataset 中的 home_lsoa
EV.assigned_nts_person_id = selected NTS person
EV.travel_chain = sample from selected person's chain_pool
```

home trip 的 destination 仍应强制为：

```text
destination_lsoa = EV.home_lsoa
```

### 3.5 出行链应保留的结构

代码应确保 NTS 出行链中的 trip 顺序被保留。

例如，一辆 EV 的一周出行链应类似于：

```text
home -> work -> shopping -> home
home -> education -> home
home -> leisure -> home
```

而不是把所有 trip 目的打乱后重新排列。

### 3.6 普通随机扰动要求

如果代码对 NTS 出行链加入普通随机扰动，应满足：

- 出发时间可以在合理范围内扰动；
- 到达时间或停留时间可以在合理范围内扰动；
- trip 目的可以保持不变，或者在明确规则下进行小概率变化；
- trip 顺序不能被破坏；
- 一周内的 trip 仍应落在合法时间范围内；
- 不应出现负的 trip duration；
- 不应出现到达时间早于出发时间的情况；
- 不应出现同一辆 EV 在同一时间出现在多个地点的情况；
- 扰动不应把该 EV 的周链变成另一个 NTS person 的行为链。

### 3.7 每周抽样的正确含义

本文中的“每周抽样”不是指每周重新给同一辆 EV 分配不同 NTS person。

正确含义是：

```text
fixed binding:
    EV_001.assigned_nts_person_id = NTS_person_A

week 1:
    EV_001 <- sample or perturb one chain from NTS_person_A.chain_pool

week 2:
    EV_001 <- sample or perturb one chain from NTS_person_A.chain_pool
```

因此，需要检查代码是否：

- 在初始化阶段固定 EV-person mapping；
- 对每辆 EV 分别抽样 weekly chain；
- 每周抽样时只访问该 EV 绑定人的 pool；
- 不会在不同周把同一 EV 绑定到不同 person；
- 在设置 random seed 后，person binding 和 weekly chain sampling 都可重复。

---

## 4. Holiday week 出行链转换逻辑

holiday week 是一个显式情景。当某一周被标记为 holiday week 时，代码应在 NTS 周出行链抽取和普通随机扰动之后，对该 EV 的一周出行链应用 holiday week 转换。

holiday week 的影响包括两类：

1. 部分工作等日常必要行为会被替换为 leisure 或 shopping；
2. 所有出行时间都会整体推迟。

### 4.1 Holiday week 触发条件

代码应有明确的 holiday week 判断逻辑。

示例：

```text
if simulation_week.is_holiday_week:
    travel_chain = apply_holiday_week_transform(travel_chain, config, rng)
```

不应在没有配置或没有输入标记的情况下隐式修改出行链。

### 4.2 Holiday week 下的目的替换

holiday week 中，部分 work / commute / business / education 等日常行为可以被替换为 leisure 或 shopping。

推荐逻辑如下：

```text
replaceable_purposes = ["work", "commute", "business", "education"]

for trip in travel_chain:
    if trip.purpose in replaceable_purposes:
        if rng.random() < holiday_replace_probability[trip.purpose]:
            trip.purpose = sample_from(["leisure", "shopping"], replacement_weights)
```

其中：

- `holiday_replace_probability` 应是可配置参数；
- `replacement_weights` 应是可配置参数，例如 leisure 与 shopping 的比例；
- home trip 不能被替换；
- charging 不是 trip purpose 时，不应被此逻辑替换；
- medical / healthcare / escort / emergency 等必要目的是否可替换，应由配置明确规定；
- 目的替换后仍应保留原 trip 的时间顺序和 trip ID。

示例配置：

```text
holiday_replace_probability:
    work: 0.5
    commute: 0.5
    business: 0.4
    education: 0.6

holiday_replacement_weights:
    leisure: 0.6
    shopping: 0.4
```

如果当前代码没有区分 commute 与 work，也可以将 commute 合并到 work 类别中处理，但必须有明确映射。

### 4.3 Home purpose 不得替换

任何表示回家的 trip 都必须保持为 home。

预期逻辑：

```text
if trip.purpose == "home":
    keep purpose as "home"
    destination_lsoa = EV.home_lsoa
```

不能出现 holiday week 将 home trip 改成 leisure 或 shopping 的情况。

### 4.4 Holiday week 下所有出行整体推迟

holiday week 中，所有出行都应推迟。

推荐使用“日链整体延迟”而不是对每个 trip 独立延迟，因为这样更容易保持一天内 trip 的先后顺序和停留时间结构。

推荐逻辑：

```text
for each day_chain in weekly_travel_chain:
    delay_minutes = sample_holiday_delay(config, rng)

    for trip in day_chain:
        trip.departure_time += delay_minutes
        trip.arrival_time += delay_minutes
```

这样可以保证：

- 同一天内所有 trip 都向后移动；
- 每个 trip 的 duration 不变；
- trip 顺序不变；
- 停留时间结构基本不变。

如果代码选择“每个 trip 独立延迟”，则必须额外保证：

- 后一个 trip 的 departure_time 不早于前一个 trip 的 arrival_time；
- 不出现 trip 重叠；
- 不出现 arrival_time 早于 departure_time；
- 一周时间范围处理清晰。

### 4.5 Holiday delay 的配置

holiday delay 应为可配置参数。

示例：

```text
holiday_delay_distribution:
    type: normal
    mean_minutes: 60
    std_minutes: 20
    min_minutes: 15
    max_minutes: 180
```

或者：

```text
holiday_delay_distribution:
    type: uniform
    min_minutes: 30
    max_minutes: 120
```

关键要求：

- holiday week 中所有 trip 的 departure_time 和 arrival_time 都应晚于或等于原时间；
- 延迟分钟数不能为负；
- 在固定 random seed 下，延迟结果应可复现。

### 4.6 跨日和跨周边界处理

如果 holiday delay 导致 trip 推迟到第二天或超出一周边界，代码必须有明确处理逻辑。

可接受的处理方式包括：

1. 允许跨日，但保持绝对时间戳连续；
2. 将超出一周末尾的 trip 截断或丢弃，并记录原因；
3. 将超出一周末尾的 trip 限制到最大允许时间；
4. 扩展仿真时间窗口，允许最后一天的出行延迟到下一天早晨。

不能接受的情况：

- 时间溢出后静默变成负值；
- trip 顺序错乱；
- 同一辆 EV 在同一时间出现在多个地点；
- charging event 与被延迟后的 trip 时间发生不合理重叠。

### 4.7 Holiday week 与 Huff destination choice 的关系

holiday week 修改 trip purpose 后，目的地 LSOA 选择应基于修改后的 purpose。

例如：

```text
normal week:
    work trip -> 使用 work POI 计算 Huff score

holiday week:
    work trip 被替换为 leisure
    -> 使用 leisure POI 计算 Huff score
```

如果 work trip 被替换为 shopping，则应使用 shopping 相关 POI，而不是继续使用 work POI。

### 4.8 Holiday week 与 charging 的关系

holiday week 不需要单独改变 charging station 选择规则。

但是由于 holiday week 会改变：

- trip purpose；
- destination LSOA；
- departure / arrival time；
- SOC 消耗时间序列；
- 停留时间窗口；

所以它会间接改变 charging event 的：

- 发生时间；
- 发生地点；
- 是否触发充电；
- home charging 或 public charging 类型；
- charging load profile。

代码应确保 charging 判断使用 holiday week 转换后的 trip 时间和目的地，而不是使用转换前的原始 NTS 时间和目的。

---

## 5. 出行行为建模：基于 Huff Score 选择目的地 LSOA

### 5.1 基本逻辑

当 EV 的某个 trip 已经确定出行目的后，代码应根据该目的选择下一站目的地 LSOA。

目的地选择应基于 Huff 模型或类似的空间交互打分方法。

对于当前 EV 所在位置，候选目的地 LSOA 的得分应取决于：

- 当前 LSOA 到候选 LSOA 的距离；
- 候选 LSOA 中与该 trip 目的相关的 POI 面积或吸引力；
- 出行目的类型；
- 可选的距离衰减参数；
- 可选的目的地吸引力指数。

### 5.2 Huff score 的预期形式

代码不一定必须使用完全相同的公式，但逻辑应等价于：

```text
score(candidate_lsoa, purpose)
    = attractiveness(candidate_lsoa, purpose) / distance(current_lsoa, candidate_lsoa)^beta
```

其中：

```text
attractiveness(candidate_lsoa, purpose)
    = candidate_lsoa 中与该 purpose 相关的 POI 面积或 POI 权重
```

`beta` 是距离衰减参数。

也可以使用如下更通用形式：

```text
score_i = A_i^alpha * f(distance_i)
```

其中：

- `A_i` 是候选 LSOA 对当前出行目的的吸引力；
- `distance_i` 是当前 LSOA 到候选 LSOA 的距离；
- `f(distance_i)` 是随距离增加而下降的函数；
- `alpha` 和 `beta` 是可配置参数。

### 5.3 概率抽样

目的地 LSOA 不应简单选择最高分，而应根据 score 占比进行概率抽样。

也就是说，对于所有候选 LSOA：

```text
probability_i = score_i / sum(score_all_candidates)
```

然后根据 `probability_i` 随机抽样得到下一站 LSOA。

需要检查代码是否：

- 计算了每个候选 LSOA 的 score；
- 将 score 正确归一化为概率；
- 概率和为 1；
- 按概率抽样，而不是总是选择最大 score；
- 在固定 random seed 下抽样结果可复现。

### 5.4 候选 LSOA 范围

候选 LSOA 应至少包括 EV 当前所在 LSOA 附近的一组 LSOA。

代码需要明确候选集的定义，例如：

- 全部 LSOA；
- 一定距离阈值内的 LSOA；
- 相邻 LSOA；
- top-k 最近 LSOA；
- 与当前出行目的相关且 POI 面积大于 0 的 LSOA。

如果候选集中某些 LSOA 对当前 purpose 没有相关 POI，则这些 LSOA 的吸引力应为 0 或极低值。

### 5.5 特殊目的地逻辑

代码应特别检查 home 相关 trip。

如果 trip 的目的表示回家，例如：

```text
purpose == "home"
```

那么目的地应为该 EV 自己的 home LSOA，而不是通过 Huff score 抽样得到一个随机 LSOA。

预期逻辑：

```text
if trip purpose is home:
    destination_lsoa = EV.home_lsoa
else:
    destination_lsoa = sample_by_huff_score(...)
```

---

## 6. EV 状态更新

代码应在每个 trip 后更新 EV 的状态。

每辆 EV 至少应维护：

- 当前所在 LSOA；
- 当前时间；
- 当前 SOC；
- 当前累计行驶距离；
- 当前一周 trip 记录；
- 当前一周 charging 记录。

每完成一个 trip 后，应更新：

```text
EV.current_lsoa = destination_lsoa
EV.current_time = trip.arrival_time
EV.SOC = EV.SOC - energy_consumed
```

如果代码使用距离矩阵，则：

```text
trip_distance = distance(origin_lsoa, destination_lsoa)
energy_consumed = trip_distance * vehicle_consumption_rate
```

需要检查代码是否避免以下问题：

- SOC 变成负数但没有处理；
- trip 距离为负；
- origin 和 destination 为空；
- EV 位置没有在 trip 后更新；
- 多辆 EV 共享同一个状态对象，导致状态污染；
- EV 回家后没有把位置更新到 home LSOA；
- holiday week 推迟后的 trip 时间没有同步到 EV.current_time。

---

## 7. 充电行为建模

### 7.1 充电触发

代码应有明确的充电触发逻辑。

例如：

- SOC 低于阈值；
- trip 后判断是否需要充电；
- 停留时间足够长时才允许充电；
- 到达某些目的地时允许充电；
- 或者代码已有其他充电触发规则。

请检查现有代码中的充电触发条件是否明确，并确保其与仿真目标一致。

下面的充电站选择逻辑只在 EV 需要充电时触发。

### 7.2 Home charging 逻辑

如果 EV 当前所在 LSOA 是该 EV 的 home LSOA，则默认可以进行 home charging。

此时不需要检查公共 charging station 数据。

预期逻辑：

```text
if EV.current_lsoa == EV.home_lsoa:
    charging_type = "home"
    station_id = None
    charging_available = True
```

也就是说，只要 EV 在 home LSOA 并且触发了充电需求，就认为可以在家充电。

代码不应因为 home LSOA 内没有公共充电站而拒绝 home charging。

### 7.3 Public charging station 选择逻辑

如果 EV 当前不在 home LSOA，并且触发了充电需求，则应选择公共充电站。

选择顺序如下。

#### Step 1：优先搜索当前 LSOA

首先检查 EV 当前所在 LSOA 内是否存在可用 charging stations。

```text
candidate_stations = stations where station.lsoa == EV.current_lsoa
```

如果当前 LSOA 内存在候选充电站，则只在这些站点中选择，不应跳到邻近 LSOA。

#### Step 2：当前 LSOA 无充电站时，搜索相邻 LSOA

如果当前 LSOA 内没有任何可用 charging station，则检查相邻 LSOA。

```text
candidate_stations = stations where station.lsoa in neighboring_lsoas(EV.current_lsoa)
```

相邻 LSOA 的定义应在代码中明确，例如：

- 空间上共享边界；
- 距离 centroid 最近的若干个 LSOA；
- 距离阈值内的 LSOA。

#### Step 3：当前 LSOA 和相邻 LSOA 都无充电站时，无法充电

如果当前 LSOA 和相邻 LSOA 都没有候选充电站，则该 EV 本次无法进行公共充电。

预期结果：

```text
charging_available = False
charging_type = "failed_public_charging"
station_id = None
```

代码应记录这次充电失败或未充电事件，避免静默忽略。

### 7.4 公共充电站打分逻辑

当存在多个候选公共充电站时，应根据以下因素进行选择：

- connector 数量越多，优先级越高；
- charging power 越大，优先级越高；
- 距离当前 LSOA centroid 越近，优先级越高。

代码应使用一个明确的 station score，例如：

```text
station_score =
    w_connector * normalized_connector_count
  + w_power * normalized_power
  + w_distance * normalized_inverse_distance_to_lsoa_centroid
```

或者其他等价形式。

关键要求：

```text
more connectors -> higher score
higher power -> higher score
shorter distance to current LSOA centroid -> higher score
```

如果代码是概率抽样，则应按 station score 归一化后抽样。

如果代码是确定性选择，则应选择 station score 最高的充电站。

无论使用概率抽样还是最高分选择，都需要在代码中保持一致，并能被测试验证。

### 7.5 充电结果记录

每次充电行为应记录：

- EV ID；
- charging start time；
- charging end time；
- charging location LSOA；
- charging type：
  - home；
  - public_current_lsoa；
  - public_neighbor_lsoa；
  - failed_public_charging；
- station ID，如果是公共充电；
- charging power；
- charged energy；
- SOC before charging；
- SOC after charging。

如果代码中没有完整记录这些字段，至少应记录后续并网分析所需的时间、地点、功率和能量。

---

## 8. 个体级仿真与聚合

代码应先完成每一辆 EV 的个体级仿真，然后再聚合结果。

正确流程：

```text
individual_ev_results = []

for each EV:
    simulate weekly travel and charging
    save this EV's trip records
    save this EV's charging records

aggregate all EV trip records
aggregate all EV charging records
```

不应直接在区域层面生成总量后再回推到车辆。

### 8.1 出行行为输出

聚合后的 trip output 应至少包含：

- EV ID；
- trip ID；
- simulation week ID；
- is_holiday_week；
- original trip purpose，如果保留；
- final trip purpose；
- purpose_changed_by_holiday，如果适用；
- origin LSOA；
- destination LSOA；
- departure time；
- arrival time；
- holiday_delay_minutes，如果适用；
- trip distance；
- energy consumed；
- SOC before trip；
- SOC after trip。

### 8.2 充电行为输出

聚合后的 charging output 应至少包含：

- EV ID；
- charging event ID；
- simulation week ID；
- is_holiday_week；
- charging LSOA；
- charging station ID，如果适用；
- charging type；
- charging start time；
- charging end time；
- charging power；
- charged energy；
- SOC before charging；
- SOC after charging；
- 是否充电成功。

### 8.3 并网分析要求

最终 charging output 应能够支持后续并网分析。

因此，充电行为至少需要能被聚合到：

```text
LSOA × time interval × charging power / charging energy
```

例如：

```text
for each LSOA and each time slot:
    total_charging_power
    total_charged_energy
    number_of_charging_events
    number_of_EV_charging
```

holiday week 应作为后续分析可用的维度之一：

```text
LSOA × time interval × is_holiday_week × charging power / charging energy
```

---

## 9. 随机性与可复现性

代码中涉及随机抽样的地方包括：

- 如果 EV-person 映射由模型生成，则仿真初始化阶段为每辆 EV 抽取并固定一个 NTS person；
- 每周从该 EV 已绑定 person 的 travel-chain pool 中抽取 weekly chain；
- 对该 person 的 NTS 出行链加入普通随机扰动；
- holiday week 中部分目的替换；
- holiday week 中出行整体推迟的 delay 抽样；
- 根据 Huff score 抽样目的地 LSOA；
- 如果公共充电站选择使用概率抽样，也属于随机过程。

代码应支持设置 random seed，使仿真结果可复现。

需要检查：

```text
same seed + same input data + same holiday week configuration
    -> same simulation result

different seed + same input data + same holiday week configuration
    -> result 可以不同，但统计逻辑应一致
```

不应在不同函数中无控制地重复初始化随机数生成器，导致 seed 失效。

推荐做法：

```text
rng = np.random.default_rng(seed)

simulate_week(..., rng=rng)
assign_nts_person_to_ev_once(..., rng=rng)
sample_chain_from_assigned_person_pool(..., rng=rng)
apply_random_perturbation(..., rng=rng)
apply_holiday_week_transform(..., rng=rng)
sample_destination_by_huff(..., rng=rng)
sample_public_charger(..., rng=rng)
```

---

## 10. 关键边界情况测试

请设计测试或检查代码是否正确处理以下情况。

### 10.1 EV home LSOA 没有公共充电站

预期：

```text
EV 在 home LSOA 时仍然可以 home charging。
```

不应因为没有公共充电站而失败。

### 10.2 EV 当前非 home LSOA，当前 LSOA 有多个公共充电站

预期：

```text
应优先只在当前 LSOA 内选择充电站。
```

选择时 connector 更多、功率更高、距离 centroid 更近的站点得分更高。

### 10.3 EV 当前非 home LSOA，当前 LSOA 没有充电站，但相邻 LSOA 有

预期：

```text
应搜索相邻 LSOA，并从相邻 LSOA 的候选充电站中选择。
```

charging type 应记录为：

```text
public_neighbor_lsoa
```

### 10.4 EV 当前非 home LSOA，当前 LSOA 和相邻 LSOA 都没有充电站

预期：

```text
本次无法公共充电。
```

代码应记录失败事件，而不是报错或静默跳过。

### 10.5 trip purpose 是 home

预期：

```text
destination_lsoa = EV.home_lsoa
```

不应通过 Huff score 随机选择 home 目的地。

### 10.6 某目的下所有候选 LSOA 的 POI 吸引力都为 0

预期代码应有 fallback 逻辑，例如：

- 使用距离衰减抽样；
- 使用均匀抽样；
- 扩大候选 LSOA 范围；
- 或明确返回无法选择目的地。

不应出现除以 0、NaN probability 或程序崩溃。

### 10.7 固定随机种子

预期：

```text
运行两次仿真，使用相同 seed，应得到完全一致的 trip 和 charging output。
```

### 10.8 多辆 EV 并行或循环仿真

预期：

```text
每辆 EV 的状态互相独立。
```

不应出现：

- EV A 的当前位置影响 EV B；
- EV A 的 SOC 被 EV B 修改；
- 多辆 EV 共享同一个 trip list 或 charging list 对象。

### 10.9 EV 应固定绑定一个 NTS person

构造两个 NTS person：

```text
NTS_person_A: work -> home
NTS_person_B: leisure -> home
```

将 EV_001 固定绑定到 NTS_person_A。

预期：

```text
EV_001 在所有 simulation weeks 中都只能使用 NTS_person_A 的 chain pool。
```

不应出现 week 1 使用 person A、week 2 使用 person B 的情况。

### 10.10 每周抽样只能来自 assigned person 的 pool

如果：

```text
EV_001.assigned_nts_person_id = NTS_person_A
NTS_person_A.chain_pool = [A_chain_1, A_chain_2]
NTS_person_B.chain_pool = [B_chain_1]
```

则 EV_001 的 weekly chain 只能来自：

```text
[A_chain_1, A_chain_2]
```

不应抽到：

```text
B_chain_1
```

### 10.11 Normal week 不应触发 holiday week 转换

当 `is_holiday_week = False` 时：

```text
work / education / commute purpose 不应因为 holiday 规则被替换；
trip departure_time / arrival_time 不应被 holiday delay 推迟。
```

普通随机扰动仍可存在，但应能与 holiday delay 区分。

### 10.12 Holiday week 中 home trip 不得替换

当 `is_holiday_week = True` 时：

```text
home trip purpose 仍应为 home；
home trip destination 仍应为 EV.home_lsoa。
```

### 10.13 Holiday week 中部分工作行为被替换

构造一个包含 work trip 的 NTS chain。

当 `is_holiday_week = True` 且 `holiday_replace_probability.work = 1.0` 时：

```text
work trip 应全部被替换为 leisure 或 shopping。
```

当 `holiday_replace_probability.work = 0.0` 时：

```text
work trip 不应被替换。
```

### 10.14 Holiday week 中所有出行都推迟

构造一个简单日链：

```text
trip_1: 08:00 -> 08:30, purpose = work
trip_2: 17:00 -> 17:30, purpose = home
```

当 holiday delay 固定为 60 分钟时，预期：

```text
trip_1: 09:00 -> 09:30
trip_2: 18:00 -> 18:30
```

trip duration 不变，trip 顺序不变。

### 10.15 Holiday week 转换后的目的地选择应使用新 purpose

如果 work trip 被替换为 leisure：

```text
Huff score 应使用 leisure POI，而不是 work POI。
```

### 10.16 Holiday week 转换后的 charging 应使用新时间

如果 trip 时间被 holiday delay 推迟：

```text
charging start time / end time 应基于推迟后的 arrival_time 和停留时间窗口计算。
```

不应仍然使用原始 NTS 时间。

---

## 11. 需要重点检查的代码问题

请重点检查现有代码是否存在以下潜在问题。注意：发现差异时应先报告差异和影响，不要直接假设一定是代码错误。

1. 是否把 NTS 出行链当成单个 trip 使用，而不是一周链条；
2. 是否破坏了 trip 的时间顺序；
3. 是否每辆 EV 有且只有一个固定的 `assigned_nts_person_id` / `assigned_person_id`；
4. 是否在仿真初始化后保持 EV-person mapping 固定，而不是每周重新给同一 EV 分配不同 person；
5. 是否每周只从该 EV 已绑定 person 的 chain pool 中抽样或扰动；
6. 是否错误混合多个 NTS person 的 trip、purpose 或 timing 特征来生成同一辆 EV 的周链；
7. 如果代码允许多个 EV 绑定到同一个 NTS person，是否是明确设计；如果要求一一对应，是否有唯一性检查；
8. 是否错误地把 NTS person 的 home location 覆盖为 EV 的 home LSOA，或反过来；
9. holiday week 是否有显式配置或输入标记；
10. normal week 是否错误触发了 holiday week 转换；
11. holiday week 中 work / commute / business / education 等目的是否按配置部分替换为 leisure / shopping；
12. holiday week 中 home purpose 是否被错误替换；
13. holiday week 中所有 trip 时间是否整体推迟；
14. holiday delay 是否保持 trip duration 与 trip 顺序；
15. holiday delay 是否可能导致 trip 重叠、负 duration 或时间溢出；
16. holiday week 目的替换后，Huff destination choice 是否使用替换后的 purpose；
17. holiday week charging 是否基于推迟后的 trip 时间；
18. Huff score 是否真的使用了 POI 面积和 LSOA 距离；
19. Huff score 是否按目的类型选择对应 POI；
20. 目的地是否按 score 概率抽样，而不是总选最高分；
21. home trip 是否强制回到 EV home LSOA；
22. trip 后 EV current location 是否正确更新；
23. SOC 是否根据行驶距离减少；
24. charging station 是否优先搜索当前 LSOA；
25. 当前 LSOA 无站点时是否再搜索相邻 LSOA；
26. home charging 是否绕过公共充电站数据；
27. 公共充电站选择是否符合 connector 多、power 大、距离近的优先级；
28. 当前和相邻 LSOA 都无充电站时是否正确记录无法充电；
29. trip output 和 charging output 是否包含后续并网所需字段；
30. random seed 是否能保证 EV-person binding、weekly chain sampling、holiday transform、Huff sampling 和 charging sampling 全流程可复现；
31. 若代码逻辑与本文不同，是否已经把差异、潜在影响和可能合理性汇报出来，而不是直接修改。

---

## 12. 推荐的测试结构

请优先设计小规模可控测试数据，而不是直接用完整真实数据测试。

### 12.1 EV mock data

```text
EV_001:
    home_lsoa = LSOA_A
    assigned_nts_person_id = NTS_person_001
    battery_capacity = 60 kWh
    initial_soc = 0.5

EV_002:
    home_lsoa = LSOA_B
    assigned_nts_person_id = NTS_person_002
    battery_capacity = 50 kWh
    initial_soc = 0.8
```

### 12.2 LSOA mock data

```text
LSOA_A:
    centroid = (0, 0)

LSOA_B:
    centroid = (1, 0)

LSOA_C:
    centroid = (5, 0)

LSOA_D:
    centroid = (10, 0)
```

Neighbor relation:

```text
LSOA_A neighbors: LSOA_B
LSOA_B neighbors: LSOA_A, LSOA_C
LSOA_C neighbors: LSOA_B, LSOA_D
LSOA_D neighbors: LSOA_C
```

### 12.3 POI mock data

```text
LSOA_A:
    retail_area = 0
    work_area = 0
    leisure_area = 0

LSOA_B:
    retail_area = 100
    work_area = 10
    leisure_area = 80

LSOA_C:
    retail_area = 50
    work_area = 200
    leisure_area = 300

LSOA_D:
    retail_area = 0
    work_area = 300
    leisure_area = 20
```

Expected behavior:

- shopping trip from LSOA_A should prefer LSOA_B over LSOA_C if distance decay is strong enough；
- work trip should prefer LSOA_C or LSOA_D depending on distance decay and work area；
- leisure trip should prefer LSOA_B or LSOA_C depending on distance decay and leisure area；
- home trip should always return to EV.home_lsoa；
- holiday week 中如果 work 被替换为 leisure，应使用 leisure_area 重新计算 Huff score。

### 12.4 Charging station mock data

```text
Station_1:
    lsoa = LSOA_B
    connectors = 2
    power = 7 kW
    distance_to_centroid = 0.2

Station_2:
    lsoa = LSOA_B
    connectors = 6
    power = 50 kW
    distance_to_centroid = 0.4

Station_3:
    lsoa = LSOA_C
    connectors = 4
    power = 22 kW
    distance_to_centroid = 0.1
```

Expected behavior:

- EV in LSOA_B should choose among Station_1 and Station_2 only；
- Station_2 should have higher score than Station_1 because connectors and power are much higher；
- EV in LSOA_A with no public chargers should use home charging if LSOA_A is its home；
- EV in LSOA_A but not home should search neighbor LSOA_B；
- EV in an LSOA with no current or neighbor charging stations should fail public charging gracefully。

### 12.5 NTS travel-chain mock data

```text
NTS_person_001, chain_pool:
    chain_001_A:
        day 1:
            trip_1: 08:00 -> 08:30, purpose = work
            trip_2: 17:00 -> 17:30, purpose = home
        day 2:
            trip_3: 10:00 -> 10:20, purpose = shopping
            trip_4: 11:30 -> 11:50, purpose = home

NTS_person_002, chain_pool:
    chain_002_A:
        day 1:
            trip_1: 11:00 -> 11:30, purpose = leisure
            trip_2: 14:00 -> 14:30, purpose = home
```

EV-person binding expected behavior:

```text
EV_001.assigned_nts_person_id = NTS_person_001
EV_002.assigned_nts_person_id = NTS_person_002
```

因此：

```text
EV_001 每周只能从 NTS_person_001.chain_pool 抽样；
EV_002 每周只能从 NTS_person_002.chain_pool 抽样。
```

EV_001 不应抽到 NTS_person_002 的 leisure-home chain，EV_002 也不应抽到 NTS_person_001 的 work-home / shopping-home chain。

Normal week expected behavior for EV_001:

```text
trip_1 purpose remains work
trip_2 purpose remains home
trip_3 purpose remains shopping
trip_4 purpose remains home
holiday_delay_minutes = 0 or None
```

Holiday week expected behavior for EV_001 with deterministic config:

```text
is_holiday_week = True
holiday_replace_probability.work = 1.0
holiday_replacement_weights.leisure = 1.0
holiday_delay_minutes = 60
```

Expected transformed chain:

```text
day 1:
    trip_1: 09:00 -> 09:30, purpose = leisure
    trip_2: 18:00 -> 18:30, purpose = home

day 2:
    trip_3: 11:00 -> 11:20, purpose = shopping
    trip_4: 12:30 -> 12:50, purpose = home
```

Important:

- EV_001 始终绑定 NTS_person_001；
- work becomes leisure；
- home remains home；
- shopping remains shopping unless configuration says otherwise；
- all trips are delayed by 60 minutes；
- durations remain unchanged；
- ordering remains unchanged。

---

## 13. 建议的测试用例名称

Codex 可以优先设计以下测试：

```text
test_ev_simulates_independently
test_ev_is_bound_to_single_nts_person
test_weekly_chain_sampled_only_from_assigned_person_pool
test_ev_person_binding_is_seed_reproducible
test_trip_order_is_preserved_after_perturbation
test_home_purpose_always_returns_to_ev_home_lsoa
test_huff_score_uses_purpose_specific_poi_area
test_huff_destination_sampling_is_seed_reproducible
test_huff_handles_zero_attractiveness_without_nan
test_home_charging_does_not_require_public_station
test_public_charging_prefers_current_lsoa
test_public_charging_falls_back_to_neighbor_lsoa
test_public_charging_failure_is_recorded
test_station_score_prefers_more_connectors_higher_power_shorter_distance
test_random_seed_reproduces_full_simulation
test_normal_week_does_not_apply_holiday_transform
test_holiday_week_replaces_work_with_leisure_or_shopping
test_holiday_week_does_not_replace_home_purpose
test_holiday_week_delays_all_trips
test_holiday_delay_preserves_trip_duration_and_order
test_holiday_destination_choice_uses_replaced_purpose
test_holiday_charging_uses_delayed_trip_times
```

---

## 14. 验收标准

代码只有在满足以下条件时，才可以认为基本符合目标仿真逻辑：

```text
1. 每辆 EV 被独立仿真；
2. 每辆 EV 在仿真初始化后有且只有一个固定绑定的 NTS person；
3. 每辆 EV 每周只从其绑定 person 的 travel-chain pool 中抽取或扰动生成周出行链；
4. 不会在不同周把同一辆 EV 重新绑定到不同 NTS person；
5. trip 顺序和时间逻辑正确；
6. holiday week 有显式配置或输入；
7. holiday week 会按配置将部分 work / commute / business / education 等目的替换为 leisure / shopping；
8. holiday week 不会替换 home purpose；
9. holiday week 会使所有 trip 时间整体推迟；
10. holiday delay 不破坏 trip duration、trip 顺序和时间合法性；
11. 非 home 目的地通过 Huff score 按概率抽样；
12. Huff score 使用 holiday 转换后的 final purpose；
13. home 目的地强制回到 EV.home_lsoa；
14. EV 的 current_lsoa 和 SOC 会随 trip 更新；
15. charging 判断使用 holiday 转换后的时间和目的地；
16. home charging 不依赖公共充电站数据；
17. public charging 优先当前 LSOA，其次相邻 LSOA；
18. 公共充电站选择体现 connector 多、power 大、距离近的偏好；
19. 无可用公共充电站时有明确失败记录；
20. 最终输出包含个体级 trip records 和 charging records；
21. 输出结果包含 assigned_nts_person_id、is_holiday_week 或等价字段；
22. 聚合结果能支持 LSOA × time interval 的并网分析；
23. 设置 random seed 后，EV-person binding、weekly chain sampling、holiday transform、Huff sampling 和 charging sampling 可复现；
24. 若代码实现与本文不同，Codex 已先报告差异和影响，并等待我讨论后再决定是否修改。
```

---

## 15. 给 Codex 的最终任务指令

请基于以上逻辑检查当前代码。

你可以采取以下方式：

1. 阅读代码，找出与上述逻辑相关的函数和数据流；
2. 标记每条核心逻辑的实现状态，建议使用以下分类：
   - `implemented_as_specified`：代码与本文逻辑一致；
   - `different_but_possibly_valid`：代码与本文不同，但可能是另一种合理设计；
   - `missing_or_unclear`：代码中未找到实现，或实现意图不清楚；
   - `likely_bug_or_conflict`：代码实现很可能与目标仿真逻辑冲突；
3. 对未实现、不清楚或实现不一致的部分，指出具体文件、函数、变量或数据流；
4. 如果代码逻辑与本文不同，请先报告差异、可能影响、以及它是否可能合理；不要直接把所有差异都当成错误；
5. 在我确认需要修改之前，不要直接修改代码或提交 patch；
6. 设计最小 mock data 和单元测试验证关键逻辑；
7. 特别检查 EV-person 绑定逻辑是否满足：
   - 一辆 EV 只能绑定一个 NTS person；
   - 绑定关系在所有 simulation weeks 中保持固定；
   - 每周 chain sampling 只能来自该 assigned person 的 pool；
   - 不会混合多个 person 的出行链生成同一辆 EV 的周链；
8. 特别检查 holiday week 逻辑是否影响：
   - purpose replacement；
   - trip time delay；
   - Huff destination selection；
   - charging time and location；
   - final trip / charging outputs；
9. 给出可选的修改建议或测试建议，但在我讨论确认之前不要直接应用修改。
