# Prompt: Coach Annual 仿真 4 项修复（review follow-up）

## 0. 上下文

`08_coach_annual_simulation.md` 已经把 coach annual 框架搭起来（commits `eb56941..1f42dcf`，26 tests 全绿）。Review 发现 4 项需要修复，全部 mirror bus 既有 idiom。本 PR 只动这 4 项，不扩范围。

继承全部约束（仓库根 `AGENTS.md` + `TASKS.md` 9 条 global constraints）。重点：

1. **不改 bus 任何模块**（只读参考可以）。
2. **不改 `mobility/core/simulator.py`** 既有签名/逻辑。
3. **不改 `mobility/coach/sim_adapter.py`、`trip_chain_coach.py`、`feasibility.py`、`coach_fleet.py`、`selection.py`、`data_loader.py`、`distance.py`** 既有逻辑（上两轮已审过；本 PR 不许动）。
4. **不改 `scripts/run_coach_pipeline.py`** v1 single-journey 入口。
5. 每个 Task 完后跑 `pytest tests/coach/ -x -q`，绿了才能进下一个；Task 2 完后必跑一次 `pytest tests/ -x -q` full suite。
6. 一 Task 一 commit。英文 commit message：`coach: fix-<n> <short description>`。
7. 不 `--no-verify`、不 force push、不 rebase 既有 commit、不 `git push`。
8. Auto mode 被拦：不重试同一条；连续 2 次拦截 → 跳过当前 task，写到 `COACH_FIXES_RESPONSE.md` "Blocked" 段。
9. 任何 "我觉得 review 漏了 X" 的想法 → 写到 `COACH_FIXES_RESPONSE.md` "Out of scope observations"，不要动手。

---

## Task 1 — Chain template 用 journey-set hash 而不是 chain_index

**问题**：[mobility/coach/chain_builder.py:137-156](../../mobility/coach/chain_builder.py#L137-L156) 当前 `coach_chain_template_id = f"{operator_code}_{chain_index:03d}"`。两个日期上 first-fit 都把"第 1 条 chain"标成 `_001`，但两条 chain 的 journey 组合可能不同（bank holiday / 学期变化导致 J2 缺席、J3 替补）。然后 [annual_simulation.py:236-243](../../mobility/coach/annual_simulation.py#L236-L243) 的 `_chain_template` 对同一 template_id 跨日期 `drop_duplicates("journey_id")` 并集，silently 把不同组合合并成一个 super-chain → over-count demand。

**修复决策**：用 journey-set hash 作为 template 身份，drop chain_index。

- 在 `chain_builder.py` 末尾 chain 全部建好之后，对每个 `(operator_code, active_date, chain_index)` 计算：
  ```python
  journey_set_hash = hashlib.sha1(
      ",".join(sorted(str(row["journey_id"]) for row in chain)).encode()
  ).hexdigest()[:10]
  template_id = f"{operator_code}_{journey_set_hash}"
  ```
  使用 `hashlib`（标准库，无新依赖）。

- 修改输出 schema：
  - `coach_chain_id = f"{operator_code}_{active_date.isoformat()}_{chain_index:03d}"`——保持不变，作为 daily instance label。
  - `coach_chain_template_id = f"{operator_code}_{journey_set_hash}"`——**不再含 chain_index**；两个日期上 journey set 相同 → 同 template_id；不同 → 不同 template_id。

- 在 `_chain_template` ([annual_simulation.py:236-243](../../mobility/coach/annual_simulation.py#L236-L243)) 顶部加 defensive 断言：
  ```python
  per_date_sets = group.groupby("date")["journey_id"].agg(lambda s: tuple(sorted(s.astype(str))))
  if per_date_sets.nunique() > 1:
      raise AssertionError(
          f"chain template {chain_id} has inconsistent journey sets across dates: {per_date_sets.unique()}"
      )
  ```
  这条断言永远不应该触发——是 Task 1 修复的不变性证据。如果触发了说明 hash 实现有 bug。

- 新增测试 `tests/coach/test_chain_builder.py`：
  - 构造两个日期 D1、D2。D1 上 journeys [J1, J2] 可串成一个 chain；D2 上 J2 改成 J3（[J1, J3] 也能串）。
  - 断言：D1 的 template_id != D2 的 template_id；两个 chain_index 都是 001 但 template_id 不同。
  - 同时保留现有 3 个测试不变（它们当前用的 chain_index=1 单日期，hash 化后 template_id 形状变了，断言要相应更新——`"OP_001"` 改成 hash 形式或改 startswith 检查）。

- 更新 [test_chain_builder.py:55](../../tests/coach/test_chain_builder.py#L55) 那条 `set(chains["coach_chain_template_id"]) == {"OP_001", "OP_002"}` 的硬编码断言，改成"两个 template_id 不同 且都 startswith 'OP_'"。

---

## Task 2 — Warmup 与 bus 完全一致

**问题**：[annual_simulation.py:84-122](../../mobility/coach/annual_simulation.py#L84-L122) 的 `_simulate_with_active_warmup` 在 active schedules 上窗口化、用 `warm_up_days=0` 双调用——不是 bus 的 idiom。bus 在 [bus/annual_simulation.py:89-124](../../mobility/bus/annual_simulation.py#L89-L124) 用 calendar-day 窗口 + simulator 内置 `warm_up_days` 参数。

**修复决策**：直接镜像 bus 的 `_simulate_with_annual_warmup`。

- 重命名 `_simulate_with_active_warmup` → `_simulate_with_annual_warmup`。
- 在新函数 docstring 顶部加一条 caveat：
  ```
  Note: Coach chain templates are sparse compared to bus blocks—a template
  may be inactive in the first few calendar days of the feed year. As a
  result the warm_up_days window often contains many inactive (24h depot
  dwell) days that do not drive realistic SoC burn-in. Recommend
  warm_up_days >= 21 for production runs to raise the probability that the
  first real journey lands inside the warm-up window.
  ```
- 函数体改成：
  ```python
  def _simulate_with_annual_warmup(
      schedules,
      battery_kwh: float,
      *,
      soc_init: float,
      warm_up_days: int,
      chemistry: str,
  ) -> tuple[np.ndarray, np.ndarray, float]:
      if warm_up_days < 0:
          raise ValueError("warm_up_days must be non-negative.")
      if warm_up_days == 0:
          return simulate_single_ev(
              schedules, battery_kwh,
              soc_init=soc_init, warm_up_days=0, chemistry=chemistry,
          )
      if warm_up_days >= len(schedules):
          raise ValueError("warm_up_days must be smaller than the annual schedule length.")
      _, _, soc_after_warmup = simulate_single_ev(
          schedules[: warm_up_days + 1],
          battery_kwh,
          soc_init=soc_init,
          warm_up_days=warm_up_days,
          chemistry=chemistry,
      )
      soc, load_kw, _ = simulate_single_ev(
          schedules, battery_kwh,
          soc_init=soc_after_warmup, warm_up_days=0, chemistry=chemistry,
      )
      return soc, load_kw, float(soc_after_warmup)
  ```
  与 bus 行为完全一致：用 calendar 前 `warm_up_days + 1` 天驱动 burn-in，simulator 内置机制保留 day N+1 的 start SoC，然后用这个 SoC 作为 soc_init 跑整年。**warmup 窗口被回放在 retained year 中**——这与 bus 一致，是 documented behaviour，不是 bug。
- 更新 `simulate_coach_chain_year` 的调用站从 `_simulate_with_active_warmup` 切到 `_simulate_with_annual_warmup`。
- **不要**再做"active schedules 过滤"。bus 不做，coach 也不做。
- 新增测试 `tests/coach/test_annual_simulation.py::test_warm_up_days_burns_in_soc`：
  - chain 2 journey（同 `_journeys()` 那个），fleet 一台 EV，`warm_up_days=14`、`soc_init=1.0`、`terminus_charge_kw=50.0`。
  - 断言 1：`load_kw.shape[0] == STEPS_PER_DAY_DECISION × len(annual_dates())`（输出仍是整年，不是 (365-14) 截断）。
  - 断言 2：`soc_after_warmup < 1.0`（burn-in 真的把 SoC 拉下来了；初始假设满电不现实）。
  - 断言 3：`per_chain` 行数仍为 1，`n_active_days == len(annual_dates())`（warmup 不改变这些）。
  - 断言 4：`load_kw[:STEPS_PER_DAY_DECISION].sum()` 与不开 warmup 时同样位置的值**不**相等（证明前 14 天 SoC 起点确实变了）。
- 不再单独 expose `_simulate_with_active_warmup`——直接删除函数。如有 import 该名字的地方，改成新名字。

---

## Task 3 — Depot vs layover 充电策略与 bus 一致

**问题**：[year_schedule.py:86-138](../../mobility/coach/year_schedule.py#L86-L138) 的 `_attach_chain_parking` 把同一天 trip 之间的 inter-journey dwell 与首尾 dwell 统统标 `terminus_dwell` + `can_charge=True` + `terminus_charge_kw`。允许 first-fit 50 km 换位的 chain 上，下午 4 小时 mid-route dwell 直接被记为 200 kWh 充电——模型乐观且与 bus 不一致。bus 在 [bus/year_schedule.py:180-249](../../mobility/bus/year_schedule.py#L180-L249) 区分 `depot_terminus`（首/尾/inactive 全天）vs `layover`（trip 间），后者默认 `can_charge=False`。

**修复决策**：把 bus 的位置语义照搬。不依赖 LSOA 字段——纯按"在 chain 一天的时间位置"决定 purpose。

- 修改 `chain_to_year_schedules` 签名（[year_schedule.py:141-149](../../mobility/coach/year_schedule.py#L141-L149)），新增三个 kw-only 参数：
  ```python
  def chain_to_year_schedules(
      chain_journeys: pd.DataFrame,
      active_dates: Iterable[dt.date],
      *,
      pre_journey_dwell_h: float = 6.0,
      terminus_dwell_purpose: str = "depot_terminus",   # 改默认值
      consumption_kwh_per_km: float | None = None,
      terminus_charge_kw: float = 50.0,
      allow_layover_charging: bool = False,             # 新增
      layover_charge_kw: float = 0.0,                   # 新增
      min_layover_for_charging_h: float = 0.0,          # 新增
  ) -> list[DailySchedule]:
  ```
- 重写 `_attach_chain_parking` ([year_schedule.py:86-138](../../mobility/coach/year_schedule.py#L86-L138))：
  - **No trips on this day**（inactive day）→ purpose=`"depot_terminus"`、`can_charge=True`、`charge_power_kw=terminus_charge_kw`、full 0–24h。与现状等价。
  - **Pre-journey dwell**（一天第一段 trip 之前）→ purpose=`"depot_terminus"`、`can_charge=True`、`charge_power_kw=terminus_charge_kw`。
  - **Inter-journey dwell**（同一天连续 trip 之间）→ purpose=`"layover"`、`can_charge = bool(allow_layover_charging and duration_h >= min_layover_for_charging_h)`、`charge_power_kw = layover_charge_kw if can_charge else 0.0`。
  - **Post-journey dwell**（一天最后一段 trip 之后）→ purpose=`"depot_terminus"`、`can_charge=True`、`charge_power_kw=terminus_charge_kw`。
  - 注意：参数 `terminus_dwell_purpose` 现在默认 `"depot_terminus"`（不再是 `"terminus_dwell"`）。这是为了与 bus 的 string 完全对齐——下游 `_parking_load_energy_kwh` 之类按 purpose 字串聚合时不会错位。
- 在 `simulate_coach_chain_year` ([annual_simulation.py:150-226](../../mobility/coach/annual_simulation.py#L150-L226)) 签名末尾新增 `allow_layover_charging / layover_charge_kw / min_layover_for_charging_h` 三个 kw-only 参数，pass-through 到 `chain_to_year_schedules`。
- 在 `simulate_coach_fleet_year` ([annual_simulation.py:263-345](../../mobility/coach/annual_simulation.py#L263-L345)) 同样 pass-through（通过 `**kw`，但默认值要在 fleet wrapper 显式声明）。
- 在 `scripts/run_coach_annual_pipeline.py` argparse 新增：
  - `--allow-layover-charging` (action="store_true", default=False)
  - `--layover-charge-kw` (type=float, default=0.0)
  - `--min-layover-for-charging-h` (type=float, default=0.0)
  并 plumb 进 `simulate_coach_fleet_year` 调用。
- **现有测试预期回归**：[test_annual_simulation.py:77](../../tests/coach/test_annual_simulation.py#L77) 的 `energy_charged_kwh > 0.0` 在新默认下仍成立（因为首尾 dwell 仍充电），但 mid-trip 那 4 小时不再充电——`per_chain.loc[0, "energy_charged_kwh"]` 的 numeric 值会下降。如果有任何测试断言具体能量数值（非 > 0），需要更新。
- 新增测试 `tests/coach/test_year_schedule.py::test_layover_default_does_not_charge_and_opt_in_does`：
  - chain：J1 (8–10h, 80 km), J2 (14–16h, 80 km)。
  - 默认（`allow_layover_charging=False`）：
    - 找到 10–14 的 ParkingEvent，`purpose=="layover"`、`can_charge==False`、`charge_power_kw==0.0`。
    - 找到 pre-dwell (2–8) 和 post-dwell (16–24)：`purpose=="depot_terminus"`、`can_charge==True`、`charge_power_kw>0`。
  - 开 layover：
    - `allow_layover_charging=True`、`layover_charge_kw=50.0`、`min_layover_for_charging_h=2.0`。
    - 10–14 dwell `can_charge==True`、`charge_power_kw==50.0`。
  - 再开 layover 但 min 设到 5 小时：10–14（4 小时）`can_charge==False`。
- 新增测试 `tests/coach/test_annual_simulation.py::test_layover_off_lowers_energy_charged_vs_on`：
  - 同 chain 跑两次，一次默认、一次 `allow_layover_charging=True / layover_charge_kw=50`。
  - 断言 `energy_charged_kwh_layover_on > energy_charged_kwh_layover_off`。
- 现有 `test_year_schedule.py::test_chain_to_year_schedules_has_active_and_inactive_days` ([test_year_schedule.py:38](../../tests/coach/test_year_schedule.py#L38)) 那条 `event.location_purpose == "terminus_dwell"` 断言要改成 `"depot_terminus"`。

---

## Task 4 — 跨午夜 + 年末 overflow + strict SoC 连续性

**问题 4a**：[year_schedule.py:183-184](../../mobility/coach/year_schedule.py#L183-L184) 在 `target_date not in schedules` 时 silently `continue`——年末日的跨午夜 journey 的 day=1 segment 直接消失。能耗、距离、SoC 衰减全丢。

**问题 4b**：[test_annual_simulation.py:96](../../tests/coach/test_annual_simulation.py#L96) 的 `abs(soc[N] - soc[N-1]) < 0.05` 阈值是单段 trip 总能耗的量级，挡不住 1 小时窗口丢失级别的 bug——SoC 连续性实际并未被证实。

**修复决策**：内部扩展 1 天，输出仍 365 天；测试改成 strict 梯度连续。

### 4a — 扩展 schedules 至 366 天，输出 crop 回 365

修改 `chain_to_year_schedules` ([year_schedule.py:141-207](../../mobility/coach/year_schedule.py#L141-L207))：

- `dates = annual_dates()` 后再 append 一天：
  ```python
  internal_dates = list(annual_dates()) + [annual_dates()[-1] + dt.timedelta(days=1)]
  ```
- `schedules` dict 用 `internal_dates` 建，所有 366 天都有 DailySchedule entry。
- Trip 注入逻辑不变；现在 `target_date not in schedules` 永远不会命中（除非有人手动给 `active_dates` 传了远超 feed-year 的日期，那种情况下我们仍 `continue`）。
- 第 366 天（年末 + 1）**不**走 `_attach_chain_parking` 的常规分支——它应该是一个 phantom day：
  - 若该天 trips 非空（年末日有 cross-midnight overflow）：只保留 trips，不注入任何 ParkingEvent（既不 pre-dwell，也不 post-dwell，也不 inter-dwell）。`schedule.parking_events = []`。这样 `simulate_single_ev` 在第 366 天只跑 driving，没有充电。
  - 若该天 trips 为空：仍**不**注入 inactive day 的 24h depot dwell（避免凭空多 24 小时充电）。`schedule.parking_events = []`。
- 函数仍返回 366-day list；调用方负责 crop。在 metadata 标记 `is_overflow_day = (date == internal_dates[-1])`。

修改 `simulate_coach_chain_year` ([annual_simulation.py:150-226](../../mobility/coach/annual_simulation.py#L150-L226))：

- `chain_to_year_schedules` 返回 366 天后，`_simulate_with_annual_warmup` 接收的就是 366 天。
- `simulate_single_ev` 输出 `soc/load_kw` 长度 = `366 × STEPS_PER_DAY_DECISION`。
- 在 return 之前 crop：
  ```python
  output_steps = len(annual_dates()) * STEPS_PER_DAY_DECISION
  soc = soc[:output_steps]
  load_kw = load_kw[:output_steps]
  ```
- `total_kwh / annual_distance_km` 的 sum 仍走 **所有 366 天的 schedules**——overflow 那段 driving 的能耗仍被记入（不丢能耗归因）。
- `energy_charged_kwh = float(np.sum(load_kw) * STEP_HOURS_DECISION)`——load_kw 已 crop，phantom 第 366 天的充电（应该是 0，因为我们不注入 ParkingEvent）不进入。
- 在 result dict 加一个透明字段：
  ```python
  "overflow_trip_count": int(sum(len(schedules[i].trips) for i in range(len(annual_dates()), len(schedules))))
  ```
  方便 audit。
- 在 result dict 加 `n_schedule_days = len(schedules)`（366）和 `n_output_days = len(annual_dates())`（365），与 bus 风格一致。

修改 `_load_profile_frame` ([annual_simulation.py:246-260](../../mobility/coach/annual_simulation.py#L246-L260))：

- 现在 `load_kw.shape[0]` 应该是 `365 × STEPS`（crop 后）。当前断言保留即可，但 `dates = annual_dates()` 仍是 365 天——schema 不变。

### 4b — Strict cross-midnight 测试

更新 [test_annual_simulation.py:81-96](../../tests/coach/test_annual_simulation.py#L81-L96) `test_cross_midnight_chain_soc_is_continuous_at_day_boundary`：

- 当前阈值 `< 0.05` 改为 strict 梯度连续：
  ```python
  step_h = STEP_HOURS_DECISION  # import from mobility.core.constants
  expected_step_drop = (
      80.0 * 0.5 / 400.0  # 距离×消耗÷电池 = 整段 trip SoC 下降
  ) / (2.0 / step_h)  # ÷ 总 step 数
  # 验证 day boundary 处梯度连续：左侧步长 == 右侧步长 == expected_step_drop
  left_step_drop = float(soc[STEPS_PER_DAY_DECISION - 2] - soc[STEPS_PER_DAY_DECISION - 1])
  right_step_drop = float(soc[STEPS_PER_DAY_DECISION] - soc[STEPS_PER_DAY_DECISION + 1])
  assert left_step_drop > 0  # 在驾驶，SoC 下降
  assert right_step_drop > 0
  assert abs(left_step_drop - expected_step_drop) < 1e-9
  assert abs(right_step_drop - expected_step_drop) < 1e-9
  ```
- 同时保留原本"SoC 数组长度 == 365 × STEPS" 与 "全 finite" 两条断言。

### 4c — 新增年末 overflow 测试

新文件不需要，加到 `tests/coach/test_year_schedule.py`：

```python
def test_year_end_cross_midnight_overflow_preserves_soc_and_energy() -> None:
    # chain 含一条跨午夜 journey，active 在 COACH_FEED_YEAR_END
    # 断言 1: chain_to_year_schedules 返回 366 天（year_end + 1）
    # 断言 2: 第 366 天的 trips 非空（overflow 落在那里）
    # 断言 3: 第 366 天 parking_events 为空（不注入幻象 dwell）
    # 断言 4: simulate_coach_chain_year 的 load_kw 长度 == 365 × STEPS（crop）
    # 断言 5: result["total_kwh"] 包含 overflow 那段的能耗
    # 断言 6: result["overflow_trip_count"] >= 1
```

具体 EV/journey 参数：battery 400 kWh、consumption 0.5、journey 80 km、`start_h=22`、`end_h=26`、`active_dates=[COACH_FEED_YEAR_END]`、`terminus_charge_kw=0`（隔离充电干扰）。

---

## Task 5 — Infeasibility-triggered layover retry with OCM eligibility

**动机**：Task 3 把 layover 默认关掉后，first-fit chain 上紧凑串好几段 journey 的 case 会大概率触发 `soc_floor_hit_h`——这反映出 "chain 算法构造的 chain 在没有 mid-charge 时根本跑不下来"。模型自洽，但 notebook 04 的 infeasibility 图会变难看，且 fleet 仿真里这种 chain 的 demand 全被记 0（feasible=False → 充电量没意义）。

**修复决策**：加一个 retry 层。第一遍 layover off；如果 infeasible 且 chain 路上某些 LSOA 有 coach-eligible OCM public charger，第二遍只在那些 LSOA 上启用 layover 充电。bus 没这套，所以这是 coach 独有扩展，放在独立 Task 不混进 Task 3。

### 5a — Coach-eligible OCM 供给侧（新模块）

- 新文件 `mobility/coach/charging_supply.py`。
- 公开 API：
  - `COACH_ELIGIBLE_OCM_BANDS = ("Rapid (50–149 kW)", "Ultra-Rapid (150+ kW)")`——默认排除 Fast (8–49 kW) 与 slow。
  - `DEFAULT_OCM_PATH = ROOT / "data" / "UK_OCM_stations_labeled.csv"`。
  - `load_coach_eligible_stations(path: str | Path = DEFAULT_OCM_PATH, *, bands: tuple[str, ...] = COACH_ELIGIBLE_OCM_BANDS, min_capacity_kw: float = 50.0) -> pd.DataFrame`——返回 `(StationID, lsoa_code, TotalCapacity_kW)` 长表，filter 后。
  - `eligible_lsoa_kw(stations: pd.DataFrame) -> pd.Series`——index=`lsoa_code`，value=`sum(TotalCapacity_kW)`，按 LSOA 聚合 eligible 总容量。
- 单元测试 `tests/coach/test_charging_supply.py`：
  - 构造合成 OCM frame，3 条记录：(Fast band, 30 kW), (Rapid band, 60 kW), (Rapid band, 150 kW)。
  - 断言 `load_coach_eligible_stations` 过滤掉 Fast 那条；返回 2 行。
  - 断言 `eligible_lsoa_kw` 在同 LSOA 的两条 Rapid 上聚合到 210 kW。
- **不读** `outputs/charger_registry.parquet`（bus 那边的 synthetic 合成结果），与 `08_*.md` 既定原则一致——避免循环验证。

### 5b — Per-LSOA layover eligibility 在 `_attach_chain_parking` 内生效

修改 [year_schedule.py:86-138](../../mobility/coach/year_schedule.py#L86-L138) 的 `_attach_chain_parking`：

- 新增 kw-only 参数 `eligible_layover_lsoas: set[str] | None = None`。
- 行为：
  - `eligible_layover_lsoas is None`：与 Task 3 完成后行为一致（全局 layover ON/OFF 由 `allow_layover_charging` 控制）。
  - `eligible_layover_lsoas` 是 set：layover dwell **每段独立**检查 `dwell.location_lsoa in eligible_layover_lsoas`——只有 in-set 才能在此 dwell 充电；out-of-set 即使 `allow_layover_charging=True` 也 `can_charge=False`。
- `chain_to_year_schedules` 同步 plumb `eligible_layover_lsoas` 参数到 `_attach_chain_parking`。

### 5c — Retry wrapper

新增 `mobility/coach/annual_simulation.py` 顶层函数 `simulate_coach_chain_year_with_retry`：

```python
def simulate_coach_chain_year_with_retry(
    chain_id: str,
    chain_journeys: pd.DataFrame,
    ev_spec,
    active_dates,
    *,
    eligible_layover_lsoas: set[str] | None = None,
    layover_charge_kw_for_retry: float = 50.0,
    min_layover_for_charging_h_for_retry: float = 1.0,
    **kw,
) -> dict:
    """Run pass 1 with layover off; if infeasible and eligible LSOAs exist on
    the chain, run pass 2 with layover on at those LSOAs only.
    """
    pass1 = simulate_coach_chain_year(
        chain_id, chain_journeys, ev_spec, active_dates,
        allow_layover_charging=False, **kw,
    )
    if pass1["feasible"] or eligible_layover_lsoas is None:
        pass1["retry_used"] = False
        pass1["retry_reason"] = ""
        return pass1
    chain_lsoas = {
        str(value) for value in chain_journeys.get("end_lsoa", pd.Series(dtype=str)).dropna().astype(str)
        if value
    } | {
        str(value) for value in chain_journeys.get("start_lsoa", pd.Series(dtype=str)).dropna().astype(str)
        if value
    }
    chain_eligible = chain_lsoas & eligible_layover_lsoas
    if not chain_eligible:
        pass1["retry_used"] = False
        pass1["retry_reason"] = "no_eligible_lsoa_on_chain"
        return pass1
    pass2 = simulate_coach_chain_year(
        chain_id, chain_journeys, ev_spec, active_dates,
        allow_layover_charging=True,
        layover_charge_kw=float(layover_charge_kw_for_retry),
        min_layover_for_charging_h=float(min_layover_for_charging_h_for_retry),
        eligible_layover_lsoas=chain_eligible,
        **kw,
    )
    pass2["retry_used"] = True
    pass2["retry_reason"] = "infeasible_pass1_eligible_lsoa_present"
    pass2["pass1_infeasibility_reasons"] = pass1["infeasibility_reasons"]
    pass2["pass1_feasible"] = pass1["feasible"]
    pass2["eligible_layover_lsoas"] = sorted(chain_eligible)
    return pass2
```

注意 `simulate_coach_chain_year` 也要相应在签名上吃 `eligible_layover_lsoas` 参数并 pass-through 到 `chain_to_year_schedules`。

### 5d — Fleet wrapper + CLI

修改 `simulate_coach_fleet_year`：

- 新增 kw-only 参数 `eligible_layover_lsoas: set[str] | None = None`。
- 内部循环：把每个 chain 走 `simulate_coach_chain_year_with_retry` 而不是 `simulate_coach_chain_year`（当 `eligible_layover_lsoas is not None` 时；为 None 时维持当前 behaviour 调用 `simulate_coach_chain_year`，零 retry overhead）。
- per-chain 输出 dataframe 新增 4 列：`retry_used` (bool)、`retry_reason` (str)、`pass1_feasible` (bool, NaN if retry_used=False)、`eligible_layover_lsoa_count` (int, 0 if retry_used=False)。

修改 `scripts/run_coach_annual_pipeline.py`：

- 新 argparse flag `--enable-eligible-layover-retry` (action="store_true", default=False)。
- 新 argparse `--ocm-path` (type=Path, default=`charging_supply.DEFAULT_OCM_PATH`)。
- 若 flag ON：调 `load_coach_eligible_stations(ocm_path)` → `eligible_lsoa_kw(...)` → 拿到 `set(eligible_lsoa_kw.index)` 传给 `simulate_coach_fleet_year`。
- Flag OFF（默认）：行为退化到 Task 3 完成后的 baseline，零 OCM 读盘。

### 5e — 测试

新建 `tests/coach/test_annual_simulation_retry.py`：

- **Case 1（无 retry：feasible chain）**：fleet 一台大电池 EV、chain 短，pass 1 直接 feasible。断言 `retry_used==False`、`feasible==True`。
- **Case 2（infeasible 但无 eligible LSOA）**：小电池 EV + 长 chain + `eligible_layover_lsoas=set()`。断言 `retry_used==False`、`retry_reason=="no_eligible_lsoa_on_chain"`、`feasible==False`。
- **Case 3（infeasible 且有 eligible LSOA → retry feasible）**：小电池 EV + 长 chain（两段 journey 间 dwell 落在 LSOA "E01_OK"）+ `eligible_layover_lsoas={"E01_OK"}`，retry 配置 `layover_charge_kw=120, min_layover_for_charging_h=1.0`。断言 `retry_used==True`、`retry_reason=="infeasible_pass1_eligible_lsoa_present"`、`pass1_feasible==False`、`feasible==True`（pass 2）、`energy_charged_kwh > pass1.energy_charged_kwh`。
- **Case 4（infeasible 但 eligible LSOA 不在 chain 上）**：chain 的 LSOA 是 {"E01_X", "E01_Y"}，`eligible_layover_lsoas={"E01_Z"}`。断言 `retry_used==False`、`retry_reason=="no_eligible_lsoa_on_chain"`。

### 5f — Honest labels & notebook

- 在 `docs/coach_annual_next_steps.md` 把第 2、3 条更新：
  - 第 2 条（Per-event terminus matching）加一句 "v1 of this is implemented in Task 5: retry-with-eligible-LSOA-layover, gated by `--enable-eligible-layover-retry`; full Huff allocation across journeys remains future work."
  - 第 3 条（Public charger eligibility for coach）加一句 "Initial coach-eligibility filter (`COACH_ELIGIBLE_OCM_BANDS`) is in `mobility/coach/charging_supply.py`; refinements (operator-specific access rules, bay geometry, dwell-friendly siting) remain future work."
- Notebook 04：在 Honest Labels 表追加两行：
  - `("Layover charging policy", "off by default; opt-in at OCM-eligible LSOAs", "see --enable-eligible-layover-retry CLI flag")`
  - `("Retry pass", "two-pass when --enable-eligible-layover-retry is set", "pass 1 layover-off; pass 2 layover-on only at chain LSOAs that have Rapid+ OCM public stations")`

### 5g — 不要做的事（Task 5 专属）

- **不**做完整 Huff allocation——只是 binary in-set / out-of-set 判定。
- **不**做 OCM 站的 utilization / queueing。
- **不**让 retry 改变 chain template 身份；同一 template_id 在 retry-on 和 retry-off 两种模式下指代同一组 journey。
- **不**把 retry 设为默认 ON——默认 off 让 Task 3 的 baseline 保持纯净。
- **不**改 OCM CSV 文件本身（read-only）。
- **不**让 retry 影响 `n_active_days / total_kwh / annual_distance_km`（driving 端不变，只改 charging 端）。

---

## 验收

- `pytest tests/coach/ -x -q` 在每个 Task commit 之前绿；HEAD 全绿，测试数 26 → ≥ 35（Task 1/2/3/4 各 +1 测试 + Task 5 +5 测试 = +9）。
- `pytest tests/ -x -q` 在 Task 2 完成后跑过；除上一轮已知 `test_home_charging` 失败外不引入新 failure。
- `python notebooks/_build_04_coach_annual_narrative.py` 仍能重生 notebook；`jupyter nbconvert --to notebook --execute --inplace notebooks/04_coach_annual_simulation.ipynb` < 150 秒（layover-off 默认下，per-chain energy_charged_kwh 数字会改变，notebook 文字若硬编码了某个数值需要相应调整；若没硬编码则无需改 notebook）。
- 公开 API grep 仍全部命中（这一 PR 不改任何 single-journey API）：
  ```
  grep -n "def simulate_coach_journey" mobility/coach/sim_adapter.py
  grep -n "def journey_to_daily_schedules" mobility/coach/trip_chain_coach.py
  grep -n "def journey_feasibility" mobility/coach/feasibility.py
  grep -n "def sample_coach_ev\|def load_coach_fleet" mobility/coach/coach_fleet.py
  ```
- 新增字段（透明度）：`per_chain` parquet 多 `overflow_trip_count` 列；`simulate_coach_chain_year` result 多 `n_schedule_days / n_output_days / overflow_trip_count`。

---

## PR description 模板

```markdown
## Summary
Five code-review fixes for the coach annual simulation layer:

- fix-1: `coach_chain_template_id` now embeds a SHA1 hash of the chain's
  sorted journey_id set instead of a per-date chain_index. Two dates whose
  first-fit chain happens to be index 1 but contains different journeys
  now get distinct template_ids, preventing `_chain_template` from silently
  merging different journey sets into one super-chain.
- fix-2: Replace `_simulate_with_active_warmup` with the bus-equivalent
  `_simulate_with_annual_warmup` idiom (calendar-day window + simulator's
  built-in `warm_up_days` parameter). Output remains full feed-year length.
- fix-3: Bus-equivalent depot/layover charging policy. Pre-journey,
  post-journey, and inactive-day dwell are `depot_terminus` (always charge);
  inter-journey dwell is `layover` (default no charge, opt-in via
  `allow_layover_charging` / `layover_charge_kw` / `min_layover_for_charging_h`).
  CLI exposes these as flags.
- fix-4: Year-end cross-midnight journeys are no longer silently dropped.
  `chain_to_year_schedules` extends internally to 366 days so the overflow
  trip segment is simulated; output `soc / load_kw` are cropped back to
  365 days for public schema; `total_kwh / annual_distance_km` retain the
  full energy attribution. Cross-midnight SoC continuity test tightened
  to strict per-step gradient (was 0.05 tolerance, now 1e-9).
- fix-5: Infeasibility-triggered layover retry. New module
  `mobility/coach/charging_supply.py` loads `data/UK_OCM_stations_labeled.csv`
  filtered to coach-eligible bands (Rapid/Ultra-Rapid, >=50 kW). New wrapper
  `simulate_coach_chain_year_with_retry` runs pass 1 with layover off; if
  pass 1 is infeasible AND the chain has at least one journey LSOA that
  hosts an eligible OCM station, pass 2 enables layover charging only at
  those LSOAs. Gated behind new CLI flag `--enable-eligible-layover-retry`
  (default off). Per-chain output gains `retry_used / retry_reason /
  pass1_feasible / eligible_layover_lsoa_count` columns.

## Verification
- pytest tests/coach/ -x -q  → all green (26 → N)
- pytest tests/ -x -q       → no new failures (Task 2 gate)
- python notebooks/_build_04_coach_annual_narrative.py
- jupyter nbconvert --to notebook --execute --inplace ...  → <150s
- Public-API greps unchanged.

## Public-API changes
- `simulate_coach_chain_year` and `simulate_coach_fleet_year` gain three
  optional kw-only parameters with bus-compatible defaults
  (`allow_layover_charging=False`, `layover_charge_kw=0.0`,
  `min_layover_for_charging_h=0.0`).
- `simulate_coach_chain_year` also gains `eligible_layover_lsoas` (Task 5).
- `chain_to_year_schedules` default `terminus_dwell_purpose` changed from
  `"terminus_dwell"` to `"depot_terminus"` to match bus.
- `per_chain` parquet schema gains `overflow_trip_count` (Task 4) plus
  `retry_used / retry_reason / pass1_feasible / eligible_layover_lsoa_count`
  (Task 5).
- New public functions: `mobility.coach.charging_supply.load_coach_eligible_stations`,
  `eligible_lsoa_kw`; `mobility.coach.annual_simulation.simulate_coach_chain_year_with_retry`.

## Deviations from AGENTS.md
None.
```

---

## 不要做的事

- 不修 `mobility/coach/sim_adapter.py / trip_chain_coach.py / feasibility.py / coach_fleet.py / selection.py / data_loader.py / distance.py / stop_geometry.py / build_all_journeys.py` 既有代码。`stop_geometry.py` 的 `attach_lsoa_to_journeys`（Task 5 加的）也不动。
- 不引入 utilization / queueing。
- 不引入 layover 充电的默认开启——bus 默认是关的，coach 跟一样。
- 不改 [scripts/run_coach_pipeline.py](../../scripts/run_coach_pipeline.py) 的 v1 single-journey 入口。
- 不引入新的外部依赖（`hashlib` 是标准库）。
- 不在 ParkingEvent 加新字段（如果非要 audit 就放 schedule.metadata）。
- 不删 `_simulate_with_active_warmup` 之外的既有函数。
- 不动 `notebooks/04_coach_annual_simulation.ipynb` 的整体结构；若 Task 3 默认值变化导致 cell 输出数值改变（如 layover 关掉后 `energy_charged_kwh` 下降），仅在该 cell 的 markdown caveat 加一句"layover charging disabled by default; opt-in via CLI"。
- 不 `git push`、不 force push、不 rebase、不 `--no-verify`。

---

## Deliverable

仓库根写 `COACH_FIXES_RESPONSE.md`（结构照 `COACH_ANNUAL_RESPONSE.md`）：

- 每 fix 的 commit hash + 一行摘要表。
- 测试命令与结果（每 Task 后 `pytest tests/coach/` 输出 + Task 2 后 full suite 输出）。
- "Out of scope observations" 段。
- "Numeric impact" 段——量化 fix-3（layover off by default）对 notebook 04 已观察到的 per_chain `energy_charged_kwh` / `total_kwh` 的影响（之前 vs 之后），以及 fix-5 retry 启用后 infeasibility 率的下降（至少给 protagonist chain 一对数字）。
- "Blocked by classifier" 段，若有。

---

## Failure handling

任一 Task 前置条件被证伪（bus 函数已改、`simulate_single_ev` 签名已变、`STEP_HOURS_DECISION` 重命名等）：stop，写 blocker 到 `COACH_FIXES_RESPONSE.md` "Blocked" 段，skip 该 Task，继续下一个。不要猜。
