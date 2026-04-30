# Prompt: 修复 bus 模块 review 发现的 3 个真问题 + 补 notebook 收官节

## 0. 上下文

`mobility/bus/` 已按 [01_bus_redesign.md](01_bus_redesign.md) 重写完毕，8/8 测试已通过、notebook `01_single_bus_simulation.ipynb` 4.1s 跑完。但代码 review 发现 3 个会埋雷的真问题 + 1 个交付不完整的小项需要在合并前修掉。

继承约束：仓库根 [AGENTS.md](../../../AGENTS.md) 全部硬规则（单位后缀 / RNG / parquet / 不引入 `holidays/geopandas`）。本 PR 不允许改动 [01_bus_redesign.md](01_bus_redesign.md) 已落地的接口签名以外的任何东西——只修以下 4 项。

---

## 1. P1 · `test_block_inference_bitexact.py` 的"假 bit-exact"

### 现状

[tests/mobility/bus/test_block_inference_bitexact.py](../../tests/mobility/bus/test_block_inference_bitexact.py) 只对**第一个** `(agency_id, service_id)` 分组做断言。这能过的唯一原因是：`infer_blocks` 内部 `block_counter` 从 0 开始递增，而被测分组在原始全表运行中也恰好是第一个被迭代的。换第二个分组，生成的 `INF_<agency>_<svc>_000000` 会与 parquet 里实际存的 `INF_<agency>_<svc>_017842` 之类的不等——测试会立刻崩。所以这个测试**没有真正在校验 1000 个 block** 的 bit-exact，反而给人虚假的安全感。

[01_bus_redesign.md §3](01_bus_redesign.md) 原文要求："抽 1000 个 inferred block 的 input frame，新函数重跑，断言 `block_id` 序列与 parquet 中的现存值完全一致"。

### 要做的事

把 `tests/mobility/bus/test_block_inference_bitexact.py` 重写成 ≥ 1000 个 block 的回归测试。两种合规思路二选一：

**思路 A（推荐 · 集合等价）**：

抽 inferred 子集中的前 N 个完整 `(agency_id, service_id)` 分组，使覆盖的 block 总数 ≥ 1000。新函数对**整个抽样子集**一次性调用 `infer_blocks`（不要按 group 各调一次）。断言：

```python
expected = set(parquet_subset["block_id"])
observed = set(infer_blocks(subset_input, BlockInferenceConfig(
    same_stop_bonus_h=1.0, route_continuity_bonus_h=0.5,
)))
assert expected == observed
```

集合相等避开 `INF_*_<counter>` 后缀的全局偏移问题，又能捕获分组拆分错误（如果分组逻辑变了，集合元素会变）。

**思路 B（更严格 · 全量回归）**：

对**所有**`block_source == "inferred"` 的行（约 1.09M 行）一次性跑 `infer_blocks(full_inferred_input, BlockInferenceConfig(same_stop_bonus_h=1.0, route_continuity_bonus_h=0.5))`，断言生成的 `pd.Series` 与 parquet 中的 `block_id` 列完全一致。这才是真正的 bit-exact。运行时间预估 30–60s，可以接受。

选思路 B，加 `@pytest.mark.slow` marker（不加 conftest 配置也行，pytest 会忽略未知 mark），并在 docstring 里写明运行时长。

### 验收

- 新测试名改为 `test_infer_blocks_full_inferred_bit_exact`
- 参数显式 `same_stop_bonus_h=1.0, route_continuity_bonus_h=0.5`（与原 `build_all_blocks.py` 调用一致）
- 旧测试 `test_infer_blocks_matches_first_historical_inferred_group` 删除（不要保留——它给虚假信号）
- `python -m pytest tests/mobility/bus/test_block_inference_bitexact.py -v` 必须通过

---

## 2. P2 · `simulate_fleet_blocks` 对跨午夜 block 的 fleet load 聚合错乱

### 现状

[mobility/bus/sim_adapter.py:81-87](../../mobility/bus/sim_adapter.py#L81) 的 `_add_load`：

```python
def _add_load(fleet_load_kw, block_load_kw):
    if block_load_kw.shape[0] > fleet_load_kw.shape[0]:
        expanded = np.zeros(block_load_kw.shape[0], dtype=float)
        expanded[: fleet_load_kw.shape[0]] = fleet_load_kw
        fleet_load_kw = expanded
    fleet_load_kw[: block_load_kw.shape[0]] += block_load_kw
    return fleet_load_kw
```

遇到 192-step 的跨午夜 block 时它把全表 `fleet_load_kw` 扩到 192 步，于是聚合产物变成"前 96 步是所有 block 的当日贡献，后 96 步只有跨午夜 bus 的次日尾巴"——这个数组**没有有效物理意义**，既不是单日 representative profile，也不是双日窗口。

### 要做的事

修 `_add_load`：跨午夜 block 的 day1 load 必须 **wrap 回 day0**，让 fleet load 永远是 96 步 representative-service-day profile。

```python
def _add_load(fleet_load_kw: np.ndarray, block_load_kw: np.ndarray) -> np.ndarray:
    """Wrap multi-day block loads back into a 96-step representative day.

    Day-0 segment (steps 0..STEPS_PER_DAY_DECISION) maps directly.
    Day-1 segment (steps STEPS_PER_DAY_DECISION..) wraps to the same step indices.
    """
    n = STEPS_PER_DAY_DECISION
    full_days = block_load_kw.shape[0] // n
    for d in range(full_days):
        fleet_load_kw += block_load_kw[d * n : (d + 1) * n]
    remainder = block_load_kw.shape[0] - full_days * n
    if remainder > 0:
        fleet_load_kw[:remainder] += block_load_kw[full_days * n :]
    return fleet_load_kw
```

需要在 `sim_adapter.py` 顶部 `from mobility.core.constants import STEPS_PER_DAY_DECISION`（已经有，确认即可）。返回的 `fleet_load_kw` 仍然是 `STEPS_PER_DAY_DECISION` 长度的数组。在 `simulate_fleet_blocks` 内部初始化也要保证 `fleet_load_kw = np.zeros(STEPS_PER_DAY_DECISION)` 不再被扩展。

### docstring 更新

`simulate_fleet_blocks` 的 docstring 必须显式写：

> Fleet load is aggregated as a single 96-step representative service day. Cross-midnight blocks contribute their day-1 tail wrapped back to the same hour-of-day on day 0, on the assumption that the fleet runs steady-state across consecutive service days.

### 验收

新增 `tests/mobility/bus/test_sim_adapter_fleet_wrap.py`：

- 构造 2 个 block：(i) 单日 8h–17h、(ii) 跨午夜 23h–25h
- 调 `simulate_fleet_blocks`，断言：
  - `fleet_load_kw.shape == (STEPS_PER_DAY_DECISION,)`
  - 跨午夜 block 在 day1 的 0–1h 充电功率被加到 fleet load 的 0–1h 步上（不是 24–25h，因为没有 24h+ 索引）
  - 单日 block 与跨午夜 block 的 `total_charge_kwh = sum(load_kw) * STEP_HOURS_DECISION` 与单独 `simulate_block` 的结果数值一致（守恒）

---

## 3. P3 · `CHANGELOG.md` 缺失

### 现状

`Modelling/CHANGELOG.md` 不存在，仓库根 `CHANGELOG.md` 也不存在。[AGENTS.md §6.2](../../../AGENTS.md) 要求每个阶段 PR 必须追加一条 changelog。

### 要做的事

在 `Modelling/CHANGELOG.md` 新建文件，初始内容：

```markdown
# Changelog

All notable changes to the Modelling package.

## Bus module redesign · single-bus narrative (YYYY-MM-DD)
- Rebuilt `mobility/bus/` around `DailySchedule` semantics consistent with `mobility/cars/`.
- `trip_chain_bus.block_to_daily_schedules` correctly handles the 9.5% of blocks
  that span midnight, returning a 2-day list instead of silently truncating.
- Added `data_loader.summarize_block_quality` to surface native-vs-inferred
  continuity, distance provenance, and cross-midnight prevalence as first-class
  metrics rather than caveats.
- `block_inference.infer_blocks` is a bit-exact port of the legacy greedy
  algorithm; preserved by a full-inferred-subset regression test.
- Added `notebooks/01_single_bus_simulation.ipynb` with explicit Stage A.5
  data-quality disclosure and a final identity-card summary.
- Removed the legacy single-day `mobility/bus/sim_adapter.py` and the stale
  `outputs/sim_per_bus.parquet` / `sim_fleet_load_kw.npy` / `sim_fleet_load.csv`
  artefacts — they were built before the cross-midnight fix.
```

把 `YYYY-MM-DD` 替换为今天日期。

### 验收

- 文件存在于 `Modelling/CHANGELOG.md`
- 至少包含上面 6 个要点

---

## 4. Q2 · Notebook 缺 Stage H · Final identity card

### 现状

[notebooks/01_single_bus_simulation.ipynb](../../notebooks/01_single_bus_simulation.ipynb) 在 Stage F (sensitivity heatmap) 之后直接 `print(f"Notebook runtime: ...")` 收尾。[01_bus_redesign.md §4](01_bus_redesign.md) 要求 Stage H "13 个数字的 markdown 表 + wall-clock"。

### 要做的事

在 [notebooks/_build_01_bus_narrative.py](../_build_01_bus_narrative.py) 的最后一个 code cell（sensitivity 那一节）**之前**插入一个 `print(...)` 之前的新 cell，再插一个 `## H. Final identity card` markdown header。具体：

1. 把当前最后一个 code cell（sensitivity）末尾的 `print(f"Notebook runtime: ...")` 删除——它要挪到 H 节末尾。
2. 追加：
   ```python
   cells.append(md("## H. Final identity card"))
   cells.append(code("""
       final_card = pd.DataFrame([{
           "block_id": protagonist_id,
           "agency_id": str(protagonist_block['agency_id'].iloc[0]),
           "n_trips_original": int(protagonist_block.shape[0]),
           "n_schedule_days": len(baseline['schedules']),
           "total_km": round(baseline['total_km'], 2),
           "total_consumed_kwh": round(baseline['total_consumed_kwh'], 2),
           "battery_kwh": BUS_BATTERY_KWH,
           "consumption_kwh_per_km": BUS_CONSUMPTION_KWH_PER_KM,
           "depot_charge_kw": DEPOT_CHARGE_KW,
           "soc_end_baseline": round(baseline['soc_end'], 4),
           "soc_min_baseline": round(baseline['soc_min'], 4),
           "soc_end_with_layover": round(with_layover['soc_end'], 4),
           "energy_charged_kwh_baseline": round(baseline['energy_charged_kwh'], 2),
       }]).T.rename(columns={0: 'value'})
       display(final_card)

       print(f"Notebook runtime: {time.time() - NOTEBOOK_START:.1f}s")
   """))
   ```
3. 重生 notebook：`python notebooks/_build_01_bus_narrative.py`。

注意：13 个字段对应 prompt 原文要求的 `block_id / agency / n_trips / total_km / span_h / depot_dwell_h / layover_dwell_h / soc_end_baseline / soc_end_with_layover_charging / energy_charged_kwh` + 几个仿真参数。可以按上面的版本，也可以追加 `depot_dwell_h` / `layover_dwell_h`（从 `parking_table` 里 `groupby('purpose')['duration_h'].sum()` 算）凑足 13 项——选一种保持一致即可。

### 验收

- `python notebooks/_build_01_bus_narrative.py` 一次性重生 `.ipynb`
- `jupyter nbconvert --to notebook --execute notebooks/01_single_bus_simulation.ipynb --output _t.ipynb --ExecutePreprocessor.timeout=120` 成功
- 新增 cell 的 markdown header 是 `## H. Final identity card`
- 输出表格至少 13 行（包含 wall-clock 那 1 行不计算在内）

---

## 5. 不要做的事

- 不要改 `block_inference.py` 的算法（默认参数、打分顺序、循环结构都不许动）。本 PR 只测它，不改它。
- 不要改 `trip_chain_bus.py` 的接口签名。Q1/Q3/Q4 留下个 PR。
- 不要重 build `outputs/all_blocks.parquet`。
- 不要把删除的 `outputs/sim_*.{parquet,npy,csv}` 重新生成。
- 不要在 notebook 里写 `df.to_csv` / `df.to_parquet`。
- 测试运行不许联网，不许写盘到 `outputs/`。
- 不要 `--no-verify` 跳过 hooks。

---

## 6. PR description 必须包含

沿用 [AGENTS.md §6](../../../AGENTS.md)：

```markdown
## Summary
Three review fixes on the bus module redesign:
- Strengthen block_inference bit-exact regression to the full inferred subset.
- Wrap day-1 fleet load back to the 96-step representative service day.
- Add Modelling/CHANGELOG.md and finish notebook 01 with a Stage H identity card.

## Verification
- pytest tests/mobility/bus/ -v   →   N/N passed (paste output)
- jupyter nbconvert --execute notebooks/01_single_bus_simulation.ipynb   →   completed in <X>s
- python notebooks/_build_01_bus_narrative.py   →   wrote 01_single_bus_simulation.ipynb

## Dependency changes
None.

## Deviations from AGENTS.md
None.

## Inference algorithm changes
None.
```

---

## 7. 实测参考（验收时校对）

| 指标 | 期望 |
|---|---|
| `tests/mobility/bus/` 测试数 | ≥ 9 (原 8 个 + 新增 fleet_wrap) |
| `test_infer_blocks_full_inferred_bit_exact` 运行时间 | < 90s |
| Notebook restart-and-run-all 总时长 | < 60s |
| Fleet load 数组长度（`simulate_fleet_blocks` 返回） | 严格等于 `STEPS_PER_DAY_DECISION` (= 96) |
| Stage H final card 行数 | ≥ 13 |
