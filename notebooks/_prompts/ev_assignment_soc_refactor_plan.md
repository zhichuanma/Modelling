# EV Bus Assignment 与 SOC 跨日继承重构方案

## 1. 背景与当前问题

当前仿真中的 `build_vehicle_day_assignments` 逻辑大致是：

```python
n_assign = min(len(specs), len(day_blocks))

block_positions = rng.choice(
    len(day_blocks),
    size=n_assign,
    replace=False,
)

spec_positions = rng.permutation(len(specs))[:n_assign]

for block_pos, spec_pos in zip(block_positions, spec_positions):
    block = day_blocks.iloc[block_pos]
    spec = specs.iloc[spec_pos]
    assignments.append(...)
```

该逻辑有两个核心问题：

1. **随机配对可能制造“假 infeasible”**  
   某个 block 其实可以被某些 EV bus 跑，但随机 zip 时被分给了一辆不合适的车，于是后续 SOC / charging 仿真阶段才暴露 infeasible。这类 infeasible 不是系统真实不可行，而是 assignment 逻辑过于粗糙造成的。

2. **SOC 每天重置导致跨日运营状态被切断**  
   如果每天都从满电或固定初始 SOC 开始，则仿真等价于一组互相独立的代表日，不能反映连续运营下的车辆电量延续、夜间充电不足累积、车辆跨日调度压力等问题。

因此建议将模型改为：

> 每个 service_date 先随机抽取 representative sample blocks；然后基于车辆可行性约束构造 EV-block feasible pairs；再做一对一可行匹配；只有 matched feasible assignments 进入 SOC 与 depot charging 仿真；车辆 SOC 在日期之间连续继承，而不是每天重置。

---

## 2. 目标仿真口径

新的仿真口径如下：

1. 对每个 `service_date`，从当天 active blocks 中随机抽取一批 `sampled_blocks`。
2. 对每个 sampled block，寻找可运行该 block 的 EV bus。
3. 构造 feasible EV-block pairs。
4. 在 feasible pairs 上做一对一 matching：
   - 每辆 EV bus 同一天最多跑一个 sampled block。
   - 每个 sampled block 同一天最多被一辆 EV bus 覆盖。
   - 只允许 feasible pair 被分配。
5. 未匹配的 sampled block 记录为 `unmatched_sampled_block`，不偷偷重抽，也不强行分给不可行车辆。
6. SOC 仿真按照日期顺序连续推进：
   - Day N 的结束 SOC 是 Day N+1 的初始 SOC。
   - 如果某辆车某天没有 assignment，则仍应根据 depot charging / idle charging 规则更新其 SOC。
   - 不再默认每天满电起步，除非通过显式参数启用 legacy representative-day mode。

---

## 3. 建议新增配置项

建议新增一个 assignment 与 SOC 相关的配置对象。

```python
from dataclasses import dataclass


@dataclass
class AssignmentConfig:
    assignment_mode: str = "sample_then_feasible_match"

    # 每天抽取多少 sample blocks。
    # 默认抽取数量约等于 EV 车队规模。
    sample_block_multiplier: float = 1.0

    # matching 目标。
    # 第一版建议用 max_count 或 greedy feasible matching。
    matching_objective: str = "max_count"

    # 保留 legacy 逻辑，方便 A/B 对比。
    enable_legacy_random_zip: bool = False

    # SOC 口径。
    # legacy_daily_reset: 每天重置初始 SOC。
    # carryover: SOC 跨日继承。
    soc_mode: str = "carryover"

    # 初始仿真第一天 SOC。
    initial_soc: float = 1.0

    # 最低允许结束 SOC。
    min_end_soc: float = 0.10

    # 可用电池比例。
    usable_battery_fraction: float = 0.90
```

第一版也可以不引入 dataclass，而是在主函数中直接添加参数：

```python
def run_simulation(
    ...,
    assignment_mode: str = "sample_then_feasible_match",
    soc_mode: str = "carryover",
    initial_soc: float = 1.0,
    sample_block_multiplier: float = 1.0,
    min_end_soc: float = 0.10,
    usable_battery_fraction: float = 0.90,
):
    ...
```

---

## 4. Assignment 重构方案

### 4.1 每天先抽取 sample blocks

新增 helper：

```python
def sample_blocks_for_service_date(
    day_blocks: pd.DataFrame,
    n_vehicles: int,
    rng: np.random.Generator,
    sample_block_multiplier: float = 1.0,
) -> pd.DataFrame:
    """
    Randomly sample representative active blocks for one service_date.

    Blocks are sampled without replacement within the day.
    """
    if day_blocks.empty or n_vehicles <= 0:
        return day_blocks.iloc[0:0].copy()

    n_sample = min(
        len(day_blocks),
        int(np.ceil(n_vehicles * sample_block_multiplier)),
    )

    block_positions = rng.choice(
        len(day_blocks),
        size=n_sample,
        replace=False,
    )

    sampled_blocks = day_blocks.iloc[block_positions].copy()
    sampled_blocks["was_sampled_for_assignment"] = True

    return sampled_blocks
```

说明：

- `sample_block_multiplier = 1.0` 时，每天最多抽取与 EV 车队规模相同数量的 blocks。
- 如果希望给 matching 更多候选，可以设为 `1.5` 或 `2.0`。
- 抽样发生在 block 层面，且日内不放回。

---

### 4.2 判断某辆 EV 是否能跑某个 block

新增 feasibility 判断函数：

```python
def is_ev_block_feasible(
    spec: pd.Series,
    block: pd.Series,
    *,
    current_soc: float,
    min_end_soc: float = 0.10,
    usable_battery_fraction: float = 0.90,
) -> bool:
    """
    Return whether this EV bus can operate this block from its current SOC.
    """
    battery_kwh = spec["battery_kwh"]
    usable_battery_kwh = battery_kwh * usable_battery_fraction

    block_energy_kwh = block["block_energy_kwh"]

    available_energy_kwh = usable_battery_kwh * max(current_soc - min_end_soc, 0.0)

    if block_energy_kwh > available_energy_kwh:
        return False

    # Optional: vehicle type compatibility
    if "vehicle_type" in spec and "required_vehicle_type" in block:
        if pd.notna(block["required_vehicle_type"]):
            if spec["vehicle_type"] != block["required_vehicle_type"]:
                return False

    # Optional: depot compatibility
    if "depot_id" in spec and "depot_id" in block:
        if pd.notna(spec["depot_id"]) and pd.notna(block["depot_id"]):
            if spec["depot_id"] != block["depot_id"]:
                return False

    # Optional: agency / operator compatibility
    if "agency_id" in spec and "agency_id" in block:
        if pd.notna(spec["agency_id"]) and pd.notna(block["agency_id"]):
            if spec["agency_id"] != block["agency_id"]:
                return False

    return True
```

关键变化：

> Feasibility 不能只看 battery capacity，还要看当前车辆的 `current_soc`。因为 SOC 跨日继承后，同一辆车在不同日期的可用能量不同。

---

### 4.3 构造 feasible EV-block pairs

```python
def build_feasible_ev_block_pairs(
    specs: pd.DataFrame,
    sampled_blocks: pd.DataFrame,
    vehicle_soc_state: dict[str, float],
    *,
    min_end_soc: float = 0.10,
    usable_battery_fraction: float = 0.90,
) -> pd.DataFrame:
    """
    Build all feasible EV-block pairs for one service_date.

    Feasibility is evaluated using each vehicle's current SOC at the start
    of the service_date.
    """
    rows = []

    specs_reset = specs.reset_index(drop=True)
    blocks_reset = sampled_blocks.reset_index(drop=True)

    for spec_pos, spec in specs_reset.iterrows():
        vehicle_spec_id = spec["vehicle_spec_id"]
        current_soc = vehicle_soc_state.get(vehicle_spec_id, 1.0)

        for block_pos, block in blocks_reset.iterrows():
            feasible = is_ev_block_feasible(
                spec,
                block,
                current_soc=current_soc,
                min_end_soc=min_end_soc,
                usable_battery_fraction=usable_battery_fraction,
            )

            if feasible:
                rows.append(
                    {
                        "spec_pos": spec_pos,
                        "block_pos": block_pos,
                        "vehicle_spec_id": vehicle_spec_id,
                        "block_id": block["block_id"],
                        "service_date": block["service_date"],
                        "current_soc_at_assignment_start": current_soc,
                    }
                )

    return pd.DataFrame(rows)
```

---

### 4.4 做 feasible matching，而不是 random zip

第一版可以用 greedy matching，依赖最少，便于快速替换现有逻辑。

```python
def greedy_feasible_matching(
    specs: pd.DataFrame,
    sampled_blocks: pd.DataFrame,
    feasible_pairs: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """
    Greedy one-to-one matching between EV buses and sampled blocks.

    Each EV can be used at most once.
    Each sampled block can be assigned at most once.
    Only feasible pairs are allowed.
    """
    if feasible_pairs.empty:
        return pd.DataFrame()

    pairs = feasible_pairs.copy()

    # Randomize pair order to avoid deterministic ordering bias.
    pairs = pairs.sample(
        frac=1.0,
        random_state=int(rng.integers(0, 2**32 - 1)),
    )

    used_specs = set()
    used_blocks = set()
    matched_rows = []

    specs_reset = specs.reset_index(drop=True)
    blocks_reset = sampled_blocks.reset_index(drop=True)

    for _, pair in pairs.iterrows():
        spec_pos = int(pair["spec_pos"])
        block_pos = int(pair["block_pos"])

        if spec_pos in used_specs:
            continue
        if block_pos in used_blocks:
            continue

        spec = specs_reset.iloc[spec_pos]
        block = blocks_reset.iloc[block_pos]

        matched_rows.append(
            {
                "service_date": block["service_date"],
                "vehicle_spec_id": spec["vehicle_spec_id"],
                "block_id": block["block_id"],
                "assignment_status": "matched_feasible",
                "assignment_method": "sample_then_feasible_greedy",
                "spec_pos": spec_pos,
                "block_pos": block_pos,
                "soc_at_assignment_start": pair["current_soc_at_assignment_start"],
            }
        )

        used_specs.add(spec_pos)
        used_blocks.add(block_pos)

    return pd.DataFrame(matched_rows)
```

后续可以替换为 maximum bipartite matching：

```text
left nodes: EV buses
right nodes: sampled blocks
edges: feasible EV-block pairs
objective: maximize matched block count, mileage, energy, or weighted priority
```

---

## 5. SOC 跨日继承方案

### 5.1 新增车辆 SOC 状态表

SOC 不应只存在于单日仿真内部，而应作为跨日状态维护。

建议在主仿真循环开始前初始化：

```python
def initialize_vehicle_soc_state(
    specs: pd.DataFrame,
    initial_soc: float = 1.0,
) -> dict[str, float]:
    """
    Initialize SOC state for all EV buses at the beginning of the simulation horizon.
    """
    return {
        row["vehicle_spec_id"]: initial_soc
        for _, row in specs.iterrows()
    }
```

仿真过程中维护：

```python
vehicle_soc_state: dict[str, float]
```

含义：

```text
vehicle_soc_state[vehicle_spec_id] = 该车辆在当前 service_date 开始时的 SOC
```

---

### 5.2 按日期顺序推进仿真

主循环必须按 `service_date` 排序，而不是随意 groupby 后独立处理。

```python
service_dates = sorted(blocks["service_date"].unique())

vehicle_soc_state = initialize_vehicle_soc_state(
    specs,
    initial_soc=config.initial_soc,
)

for service_date in service_dates:
    day_blocks = blocks[blocks["service_date"] == service_date]

    # 1. sample blocks
    # 2. build feasible pairs using current SOC
    # 3. match feasible assignments
    # 4. simulate driving and charging for this date
    # 5. update vehicle_soc_state for next date
```

---

### 5.3 Assignment 使用当日开始 SOC

当天构造 feasible pairs 时，必须读取 `vehicle_soc_state`：

```python
feasible_pairs = build_feasible_ev_block_pairs(
    specs=specs,
    sampled_blocks=sampled_blocks,
    vehicle_soc_state=vehicle_soc_state,
    min_end_soc=config.min_end_soc,
    usable_battery_fraction=config.usable_battery_fraction,
)
```

这样，如果某辆车前一晚没有充满，它第二天可运行的 blocks 会相应减少。

---

### 5.4 每日仿真结束后更新 SOC

需要让 charging simulation 返回每辆车当天结束后的 SOC。

建议接口改为：

```python
day_charging_result, day_vehicle_end_soc = simulate_day_charging(
    service_date=service_date,
    matched_assignments=matched_assignments,
    specs=specs,
    blocks=sampled_blocks,
    vehicle_soc_state=vehicle_soc_state,
    charging_config=charging_config,
)
```

其中：

```python
day_vehicle_end_soc = {
    "EV_001": 0.82,
    "EV_002": 0.47,
    ...
}
```

然后更新跨日状态：

```python
vehicle_soc_state.update(day_vehicle_end_soc)
```

注意：没有 assignment 的车辆也应该有 SOC 更新逻辑。

例如：

```python
def update_idle_vehicle_soc(
    vehicle_spec_id: str,
    current_soc: float,
    charging_policy: ChargingConfig,
) -> float:
    """
    Update SOC for vehicles without a driving assignment.

    Depending on the depot charging policy, idle vehicles may charge up to
    target SOC, remain unchanged, or follow depot-level power constraints.
    """
    if charging_policy.charge_idle_vehicles:
        return min(charging_policy.target_soc, current_soc + charging_policy.max_idle_charge_soc_gain)

    return current_soc
```

---

## 6. 建议的主流程伪代码

```python
def run_ev_bus_simulation(
    specs: pd.DataFrame,
    blocks: pd.DataFrame,
    config: AssignmentConfig,
    base_seed: int = 0,
):
    assignment_rows = []
    diagnostic_rows = []
    charging_rows = []
    soc_state_rows = []

    vehicle_soc_state = initialize_vehicle_soc_state(
        specs,
        initial_soc=config.initial_soc,
    )

    service_dates = sorted(blocks["service_date"].unique())

    for service_date in service_dates:
        rng = make_stable_daily_rng(base_seed, service_date)
        day_blocks = blocks[blocks["service_date"] == service_date]

        # Record start-of-day SOC.
        for vehicle_spec_id, soc in vehicle_soc_state.items():
            soc_state_rows.append(
                {
                    "service_date": service_date,
                    "vehicle_spec_id": vehicle_spec_id,
                    "soc_stage": "start_of_day",
                    "soc": soc,
                }
            )

        sampled_blocks = sample_blocks_for_service_date(
            day_blocks=day_blocks,
            n_vehicles=len(specs),
            rng=rng,
            sample_block_multiplier=config.sample_block_multiplier,
        )

        feasible_pairs = build_feasible_ev_block_pairs(
            specs=specs,
            sampled_blocks=sampled_blocks,
            vehicle_soc_state=vehicle_soc_state,
            min_end_soc=config.min_end_soc,
            usable_battery_fraction=config.usable_battery_fraction,
        )

        matched_assignments = greedy_feasible_matching(
            specs=specs,
            sampled_blocks=sampled_blocks,
            feasible_pairs=feasible_pairs,
            rng=rng,
        )

        if not matched_assignments.empty:
            assignment_rows.append(matched_assignments)

        day_charging_result, day_vehicle_end_soc = simulate_day_charging(
            service_date=service_date,
            matched_assignments=matched_assignments,
            specs=specs,
            sampled_blocks=sampled_blocks,
            vehicle_soc_state=vehicle_soc_state,
            config=config,
        )

        if not day_charging_result.empty:
            charging_rows.append(day_charging_result)

        # Carry SOC to next service_date.
        vehicle_soc_state.update(day_vehicle_end_soc)

        # Record end-of-day SOC.
        for vehicle_spec_id, soc in vehicle_soc_state.items():
            soc_state_rows.append(
                {
                    "service_date": service_date,
                    "vehicle_spec_id": vehicle_spec_id,
                    "soc_stage": "end_of_day",
                    "soc": soc,
                }
            )

        n_day_blocks = len(day_blocks)
        n_sampled_blocks = len(sampled_blocks)
        n_feasible_pairs = len(feasible_pairs)
        n_matched_blocks = len(matched_assignments)
        n_unmatched_sampled_blocks = n_sampled_blocks - n_matched_blocks

        diagnostic_rows.append(
            {
                "service_date": service_date,
                "n_ev_specs": len(specs),
                "n_active_block_instances_for_service_date": n_day_blocks,
                "n_sampled_block_instances_for_service_date": n_sampled_blocks,
                "n_feasible_ev_block_pairs_for_service_date": n_feasible_pairs,
                "n_matched_feasible_block_instances_for_service_date": n_matched_blocks,
                "n_unmatched_sampled_block_instances_for_service_date": n_unmatched_sampled_blocks,
                "sampled_block_coverage_share": (
                    n_sampled_blocks / n_day_blocks if n_day_blocks else 0.0
                ),
                "matched_sample_share": (
                    n_matched_blocks / n_sampled_blocks if n_sampled_blocks else 0.0
                ),
                "matched_active_block_share": (
                    n_matched_blocks / n_day_blocks if n_day_blocks else 0.0
                ),
                "assignment_method": "sample_then_feasible_match",
                "soc_mode": config.soc_mode,
            }
        )

    assignments = pd.concat(assignment_rows, ignore_index=True) if assignment_rows else pd.DataFrame()
    diagnostics = pd.DataFrame(diagnostic_rows)
    charging = pd.concat(charging_rows, ignore_index=True) if charging_rows else pd.DataFrame()
    soc_states = pd.DataFrame(soc_state_rows)

    return assignments, diagnostics, charging, soc_states
```

---

## 7. SOC 仿真函数需要调整的接口

当前如果 charging simulation 默认每辆车每天从满电开始，需要改成显式传入 start SOC。

### 旧接口示意

```python
def simulate_charging_load(assignments, specs, blocks, ...):
    # internally assumes full battery at start of each day
    ...
```

### 新接口建议

```python
def simulate_day_charging(
    service_date: pd.Timestamp,
    matched_assignments: pd.DataFrame,
    specs: pd.DataFrame,
    sampled_blocks: pd.DataFrame,
    vehicle_soc_state: dict[str, float],
    config: AssignmentConfig,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Simulate one service_date using carryover SOC.

    Inputs:
      - vehicle_soc_state: start-of-day SOC for every EV bus.

    Outputs:
      - charging load records for this service_date.
      - end-of-day SOC for every EV bus, including idle vehicles.
    """
    ...
```

核心要求：

1. 每辆 assigned EV 的 driving energy 从 `vehicle_soc_state[vehicle_spec_id]` 中扣除。
2. Depot charging 根据车辆结束运行时间、charger power、target SOC、电网限制等规则补电。
3. 函数必须返回该日结束后的 SOC。
4. 未被分配 block 的 EV 也必须有 SOC 结果，否则跨日状态会丢失。

---

## 8. Diagnostics 建议新增字段

### Assignment diagnostics

```text
n_active_block_instances_for_service_date
n_sampled_block_instances_for_service_date
n_feasible_ev_block_pairs_for_service_date
n_matched_feasible_block_instances_for_service_date
n_unmatched_sampled_block_instances_for_service_date
sampled_block_coverage_share
matched_sample_share
matched_active_block_share
assignment_method
```

### SOC diagnostics

建议新增单独的 `vehicle_soc_states` 输出表：

```text
service_date
vehicle_spec_id
soc_stage                 # start_of_day / end_of_day
soc
battery_kwh
usable_battery_kwh
assigned_block_id
charged_energy_kwh
traction_energy_kwh
end_soc_below_min_flag
```

其中：

```text
end_soc_below_min_flag = True
```

应作为强 warning 或 error，因为 assignment feasibility 本应保证车辆不会低于 `min_end_soc`，除非 charging / energy model 与 feasibility model 口径不一致。

---

## 9. Legacy mode 保留建议

建议保留两个 legacy 开关，便于结果对比与回归测试。

```python
assignment_mode = "legacy_random_zip"
soc_mode = "legacy_daily_reset"
```

和新逻辑对比：

```python
assignment_mode = "sample_then_feasible_match"
soc_mode = "carryover"
```

这样可以输出 A/B 结果：

```text
legacy_random_zip + legacy_daily_reset:
  原始结果，用于基准对比。

sample_then_feasible_match + carryover:
  新结果，assignment 可行性更强，SOC 连续性更真实。
```

---

## 10. 最小 PR 范围

第一版 PR 建议只做以下改动：

1. 将旧 random zip assignment 抽成 legacy function。
2. 新增 `sample_blocks_for_service_date()`。
3. 新增 `is_ev_block_feasible()`，并把 `current_soc` 纳入 feasibility 判断。
4. 新增 `build_feasible_ev_block_pairs()`。
5. 新增 `greedy_feasible_matching()`。
6. `build_vehicle_day_assignments()` 或主仿真入口增加 `assignment_mode`。
7. 新增 `soc_mode="carryover"`。
8. 初始化 `vehicle_soc_state`，并按 `service_date` 顺序推进。
9. 修改 charging simulation 接口，使其接收 start-of-day SOC 并返回 end-of-day SOC。
10. 新增 `assignments`、`diagnostics`、`charging`、`vehicle_soc_states` 四类输出。

暂不纳入第一版 PR：

- MILP / OR-Tools 全局优化。
- 多辆车接力同一个 block。
- 跨 depot 车辆调拨。
- 跨日固定车辆排班优化。
- Depot 总功率约束下的全局最优 charging schedule。

---

## 11. 推荐代码注释口径

可以在代码或 README 中加入如下说明：

```text
Assignment logic:

For each service_date, the simulation first samples representative active blocks
without replacement. It then constructs feasible EV-block pairs using each
vehicle's current start-of-day SOC and static compatibility constraints such as
battery capacity, vehicle type, agency, and depot. A one-to-one feasible matching
is then performed between EV buses and sampled blocks. Each EV can be assigned to
at most one sampled block per service_date, and each sampled block can be assigned
to at most one EV. Only matched feasible assignments are passed to the SOC and
depot charging simulation. Sampled blocks without any feasible matched EV are
recorded as unmatched rather than silently resampled or randomly assigned to
infeasible vehicles.

SOC logic:

The simulation uses carryover SOC across service_dates. The end-of-day SOC of
each vehicle becomes the start-of-day SOC for the next service_date. Vehicles do
not automatically reset to full battery at the beginning of each day unless the
legacy_daily_reset SOC mode is explicitly enabled. Idle vehicles may still charge
according to the depot charging policy, and their updated SOC is carried forward.
```

中文说明：

```text
本仿真在每个 service_date 上先从 active blocks 中不放回抽取 sample blocks，
作为该日代表性 duty demand；随后基于车辆当前日初 SOC、电池容量、车型、
agency、depot 等约束构造 EV-block 可行边；再做一对一匹配。日内每辆 EV
最多跑一个 sampled block，每个 sampled block 最多被一辆 EV 覆盖。只有成功
匹配的 feasible assignments 进入 SOC 与 depot charging 仿真；抽中但无法匹配
的 blocks 被记录为 unmatched，而不是偷偷重抽或随机分给不可行车辆。

SOC 采用跨日继承口径：每辆车在 Day N 的 end-of-day SOC 会成为 Day N+1 的
start-of-day SOC。车辆不会在每天开始时自动恢复满电，除非显式启用
legacy_daily_reset 模式。未分配运行任务的 idle vehicles 也应根据 depot charging
policy 更新 SOC，并将更新后的 SOC 继续传递到下一天。
```

---

## 12. 最终重构原则

一句话总结：

> 随机性只用于抽取 representative sample blocks；车辆分配必须 feasibility-aware；SOC 必须作为车辆状态跨日继承；charging simulation 只能消费 matched feasible assignments，并返回下一日所需的 end-of-day vehicle SOC state。
