# Prompt: 修复 bus 年度仿真层 review 发现的 3 个真问题 + 3 个 nice-to-have + 测试与叙事补齐

## 0. 上下文

`mobility/bus/{annual_simulation,calendar,year_schedule}.py` + 相关测试 + `notebooks/03_bus_annual_simulation.ipynb` 已在提交 `cb223be` 落地。架构没问题（薄编排壳，不重新实现 SOC 物理），但 review 发现 3 个真问题（跨午夜尾巴静默丢 layover、`service_id` 缺失静默零里程、`warm_up_days=0` 写死且不可配），加 3 个规模化隐患和测试 + 叙事补齐。

继承约束：仓库根 [AGENTS.md](../../../AGENTS.md) 全部硬规则。本 PR **不允许改** [`mobility/bus/sim_adapter.py`](../../mobility/bus/sim_adapter.py)、[`mobility/bus/trip_chain_bus.py`](../../mobility/bus/trip_chain_bus.py)、[`mobility/core/simulator.py`](../../mobility/core/simulator.py)（它们已 review 通过）。所有改动收敛在 annual 层。

---

## 1. P1 · 跨午夜尾巴可能静默吞掉次日 layover

### 现状

[year_schedule.py:246-263](../../mobility/bus/year_schedule.py#L246) 当 `service_date+1` 也是活跃日时，把 day-1 尾巴和次日的 day-0 trips 一起 `append` 到次日 schedule。然后 [year_schedule.py:153](../../mobility/bus/year_schedule.py#L153) 的 `_attach_parking` 用 `(departure, arrival, trip_id)` 排序并跳过 `right.departure_time <= left.arrival_time` 的 layover——对深夜末班 + 凌晨头班的组合，**会静默丢失** layover 也不报错。

例：N-1 日 23:30 出发凌晨 02:00 到 → 次日 03:00 头班，N 日聚合后看起来"连续运营"，但中间 02:00–03:00 这 1h 的 layover 没了，能耗与充电估算偏低。

### 要做的事

在 [year_schedule.py](../../mobility/bus/year_schedule.py) 的 `_attach_parking` 加重叠检测：

```python
def _attach_parking(trips, ...):
    overlaps = []
    for left, right in zip(trips, trips[1:]):
        if right.departure_time < left.arrival_time:
            overlaps.append((left.trip_id, right.trip_id,
                             left.arrival_time, right.departure_time))
    if overlaps:
        warnings.warn(
            f"Trip overlap on aggregated day: {overlaps[:3]}"
            f"{'...' if len(overlaps) > 3 else ''}. "
            f"Day-1 tail of a cross-midnight block collided with the next "
            f"service day; layovers in the overlap window are dropped.",
            stacklevel=2,
        )
    ...
```

不必让它 raise（年度跑量太大），但必须有 warning 让用户能 grep 到。同时在 [annual_simulation.py:69](../../mobility/bus/annual_simulation.py#L69) `simulate_block_year` 的 result dict 里加 `n_overlap_warnings: int` 字段，让聚合层可以审计。

### 验收

新增 `tests/mobility/bus/test_annual_overlap_warning.py`：

- 构造一个跨午夜 block (`23:30 → 02:00`) + 同 block 在次日有 `01:30 → 04:00` 的 trip
- `pytest.warns(UserWarning, match="Trip overlap")` 触发
- `result["n_overlap_warnings"] >= 1`

---

## 2. P2 · `service_id` 缺失静默 365 天零里程

### 现状

[annual_simulation.py:181](../../mobility/bus/annual_simulation.py#L181) `service_date_index.get(service_id, ())` 找不到时返回空 tuple，整个 block 当 365 天全部 inactive 跑——零 distance、零 SOC 变化、零里程，**没有任何提示**。一处 `service_id` 类型不匹配（int vs string）就足以让大段 fleet 静默无效。

### 要做的事

[annual_simulation.py:181](../../mobility/bus/annual_simulation.py#L181) 改：

```python
active_dates = service_date_index.get(service_id)
if active_dates is None:
    warnings.warn(
        f"block_id={block_id!r} has service_id={service_id!r} not present "
        f"in calendar (or calendar_dates) — block will produce zero distance "
        f"for the entire feed year. Check calendar.txt parsing & dtype.",
        stacklevel=2,
    )
    active_dates = ()
```

并在 `simulate_fleet_year`（[annual_simulation.py:130](../../mobility/bus/annual_simulation.py#L130)）末尾汇总：

```python
n_blocks_with_zero_active_dates = (per_block_df["n_active_dates"] == 0).sum()
if n_blocks_with_zero_active_dates > 0:
    warnings.warn(f"{n_blocks_with_zero_active_dates} blocks had zero active "
                  f"dates in the feed year (likely service_id mismatch).")
```

把 `n_active_dates: int` 加到 per-block result 字段里。

### 验收

新增 `tests/mobility/bus/test_annual_missing_service_id.py`：

- 构造 block 引用 calendar 里没有的 `service_id="DOES_NOT_EXIST"`
- `pytest.warns(UserWarning, match="not present in calendar")` 触发
- `result["n_active_dates"] == 0`，`total_km == 0.0`

---

## 3. P3 · `warm_up_days=0` 写死且不可配

### 现状

[annual_simulation.py:103](../../mobility/bus/annual_simulation.py#L103) 硬传 `warm_up_days=0`。core 默认 [`WARMUP_DAYS=14`](../../mobility/core/constants.py) 存在的理由是：第一个活跃日从 `soc_init=1.0` 开始可能与稳态偏差很大，warmup 用前 N 天循环跑直到 SOC 收敛。年度 365 天的第一周如果起点 SOC 不真实，per-day 报告就有 bias。

### 要做的事

把 `warm_up_days` 提升为 [`simulate_block_year`](../../mobility/bus/annual_simulation.py#L69) 与 [`simulate_fleet_year`](../../mobility/bus/annual_simulation.py#L130) 的显式参数，默认值 = 0（保持向后兼容），但 docstring 显式说明：

```python
def simulate_block_year(
    block_df, service_date_index, ev_spec, ...,
    warm_up_days: int = 0,
    soc_init: float = 1.0,
) -> dict:
    """...

    warm_up_days defaults to 0 for fast notebook smoke tests; for production
    scale runs use 14 (matches mobility.core.constants.WARMUP_DAYS) to let the
    first reported day start from a near-steady-state SOC instead of the
    soc_init assumption.
    """
```

在 [_build_03_bus_annual_narrative.py](../_build_03_bus_annual_narrative.py) Stage A.5 honest-labels 节加一行说明 notebook 用 `warm_up_days=0` 是为了 < 30s 的 smoke 跑量，并非生产配置。

### 验收

新增 `tests/mobility/bus/test_annual_warmup_threading.py`：

- 同一 block，`warm_up_days=0` vs `warm_up_days=14`：第一日 `soc[0]` 不同（warmup 收敛后从更低值起步）；总能耗收敛差异 < 5%
- `simulate_fleet_year(..., warm_up_days=14)` 端到端跑通

---

## 4. N1 · 365 个 DailySchedule 对象/block 内存

### 现状

[annual_simulation.py:87-105](../../mobility/bus/annual_simulation.py#L87) 每 block 生成 365 个 `DailySchedule` 对象，绝大多数 `trips=[]` 加一个 24h 桩 ([year_schedule.py:158-165](../../mobility/bus/year_schedule.py#L158))。UK 全量 ~30k blocks → ~1100 万对象。

### 要做的事

抽出 `_INACTIVE_DAY_SINGLETON: DailySchedule | None = None` 模块级缓存：第一次构建后所有 inactive 日复用同一对象（`DailySchedule` 在 simulator 内部应该被读取而非 mutate；如果 simulator 会 mutate，则改成 `_inactive_day_factory()` 每次返回浅拷贝）。

先在 [`mobility/core/simulator.py`](../../mobility/core/simulator.py) 里 grep 是否对 schedule 对象做 in-place mutation；如有，则不能复用，留 nice-to-have 注释 `# TODO: dedup once simulator becomes read-only`。

### 验收

`memory_profiler` 或 `tracemalloc` 在 100 block × 365 day 跑量下，schedule 对象内存峰值降 50% 以上（写一行注释即可，不强求 CI 校验）。

---

## 5. N2 · `result["soc"]` 在 fleet 跑量下 OOM 隐患

### 现状

[annual_simulation.py:113](../../mobility/bus/annual_simulation.py#L113) result dict 同时挂 `load_matrix_kw`（n_days × 96 ≈ 280 KB/block）和 `soc`（35040 floats ≈ 280 KB/block）。`simulate_fleet_year` 每 block 消费完就丢，但**直接用 `[simulate_block_year(b) for b in blocks]` 的调用方** 30k blocks × 560 KB ≈ **16 GB 直接 OOM**。

### 要做的事

加显式 `keep_soc: bool = True, keep_load_matrix: bool = True` kwargs。`simulate_fleet_year` 内部聚合时调 `simulate_block_year(..., keep_soc=False, keep_load_matrix=False)`（aggregator 只需要 `total_km / total_consumed_kwh / energy_charged_kwh / soc_min / soc_end / n_active_dates`，不需要时间序列）。

docstring 显式警告：

```python
"""...
Memory: at default keep_soc=True, each block result holds an n_days*96 load
matrix (~280 KB) and an n_days*96 soc array (~280 KB). For fleet-wide loops,
prefer simulate_fleet_year (which sets both False internally) or pass
keep_soc=False, keep_load_matrix=False explicitly.
"""
```

### 验收

`test_annual_simulation.py` 加 case：`simulate_block_year(..., keep_soc=False)` 返回 dict 不含 `soc` key（或为 None），其余字段不变。

---

## 6. N3 · `annual_load_matrix_to_frame` 向量化

### 现状

[annual_simulation.py:239-249](../../mobility/bus/annual_simulation.py#L239) 是 35040 次 Python `dict + append`。一次性成本可接受但 1 周内会被反复调用。

### 要做的事

向量化：

```python
def annual_load_matrix_to_frame(matrix: np.ndarray, dates: pd.DatetimeIndex) -> pd.DataFrame:
    n_days, n_steps = matrix.shape
    step_hours = np.arange(n_steps) * STEP_HOURS_DECISION
    return pd.DataFrame({
        "date":        np.repeat(dates.values, n_steps),
        "step_index":  np.tile(np.arange(n_steps), n_days),
        "hour_of_day": np.tile(step_hours, n_days),
        "load_kw":     matrix.ravel(),
    })
```

比当前实现快 ~50×。

### 验收

`test_annual_load_matrix_frame.py`（新文件）：

- 3 天 × 96 步 → 288 行
- 日期、step_index、load_kw 与原 Python 版逐行对齐

---

## 7. 测试覆盖补齐（在 P1–P3 之外）

| 新增测试 | 断言 |
|---|---|
| `test_annual_soc_continuity.py` | 同一 block 跨日：`day_n.soc[0] == day_{n-1}.soc[-1]`（绝对相等，不要近似），覆盖至少 3 个连续活跃日 |
| `test_annual_vehicle_sample_once.py` | `simulate_fleet_year(blocks, ..., n_days=100)` 跑完，每个 `block_id` 对应的 `vehicle_gen_model` 在所有日期上恒定（per-block 抽一次而非 per-day） |
| `test_calendar_edge_cases.py` | 闰日（2027-02-28 在 feed_year 内）、`calendar_dates.exception_type=1` 加日期、`calendar_dates.txt` 缺失（[calendar.py:78](../../mobility/bus/calendar.py#L78)）、numeric `service_id`（CSV `dtype="string"` 后还能匹配） |
| `test_annual_fleet_cross_midnight.py` | fleet 上下文下，跨午夜 block 的 day-2 充电正确累加到 `fleet_load_matrix[next_day_row, :]` |
| `test_annual_soc_init_threading.py` | `soc_init=0.5` 端到端：第一个活跃日 `soc[0] == 0.5`（warmup=0 时），warmup>0 时 `soc[0] != 0.5` |

---

## 8. Notebook 03 叙事补齐

### 现状

[_build_03_bus_annual_narrative.py](../_build_03_bus_annual_narrative.py) 在"诚实标签"层面是好的（[L23-28](../_build_03_bus_annual_narrative.py#L23) 的 feed-year vs 2025 caveat、[L254-275](../_build_03_bus_annual_narrative.py#L254) 的 honest labels），但**没兑现"年度故事"**：没有 weekday/weekend 切片、没有 holiday week call-out、没有月度聚合、没有 active-heavy vs sparse service 对比。基本上是把单日 notebook 复制 365 次。

### 要做的事

在 Stage E（或新插入的 E.5）追加：

1. **Weekday vs weekend 箱图**：把所有活跃日的 `daily_total_kwh` 按 `pd.DatetimeIndex.dayofweek < 5` 分两组画 boxplot。expected: 周末显著低（看不到就要在 narrative 里诚实写出"代表服务日没差异是因为我们没拆 weekday/weekend 服务"）
2. **Holiday week call-out**：在 fleet daily energy 时间序列上标注圣诞周 (Dec 22–28) 与复活节周（feed year 实际日期）。如果数据里看到能耗下降，narrative 写一句；如果没下降，写"GTFS calendar 没标注 holiday，所以 representative service day 假设把 holiday 当普通 weekday 跑"
3. **月度热力图**：行 = month (1–12)，列 = hour-of-day (0–23)，色 = 平均 fleet load。一张图同时表达年内季节性和日内 peak 时刻
4. **Active-heavy vs sparse service 对比**：选 `n_active_dates > 300` 与 `n_active_dates < 50` 各一个 block，画 SOC 全年时间序列对比。前者贴近天花板，后者长期 idle

### 验收

- `_build_03_bus_annual_narrative.py` 重生 notebook，新 cell 数 +4
- restart-and-run-all 总时长仍 < 30s
- 4 张新图 figsize=(12, 4.5), dpi=110, 关上/右 spine

---

## 9. 不要做的事

- 不要改 [`mobility/bus/sim_adapter.py`](../../mobility/bus/sim_adapter.py) / [`trip_chain_bus.py`](../../mobility/bus/trip_chain_bus.py) / [`mobility/core/simulator.py`](../../mobility/core/simulator.py)
- 不要改 `simulate_fleet_year` 的对外签名（只允许在末尾加 kwargs）
- 不要把 `warm_up_days` 默认值改成非 0（保持向后兼容；让用户显式选）
- 不要在 notebook 里 `pip install` / `df.to_csv` / `df.to_parquet`
- 不要在测试里联网或写盘到 `outputs/`
- 不要 import `mobility.coach.*` / `mobility.cars.*`
- 不要 `--no-verify` 跳过 hooks
- 不要把 `result["soc"]` 直接删掉（会 break notebook）——改成可选字段

---

## 10. PR description 必须包含

```markdown
## Summary
Three review fixes on the bus annual layer plus three cleanups and notebook polish:
- Warn on cross-midnight tail / next-day-service trip overlap (was silent layover loss).
- Warn on missing service_id (was silent 365-day zero distance).
- Make warm_up_days an explicit kwarg with documented default 0.
- Plus: optional keep_soc/keep_load_matrix to avoid OOM at fleet scale; vectorise
  annual_load_matrix_to_frame; cache inactive-day schedule.
- Notebook 03: weekday/weekend boxplot, holiday call-out, month×hour heatmap,
  active vs sparse contrast.

## Verification
- pytest tests/mobility/bus/ -v   →   N/N passed (paste output)
- jupyter nbconvert --execute notebooks/03_bus_annual_simulation.ipynb   →   completed in <X>s
- python notebooks/_build_03_bus_annual_narrative.py   →   wrote 03_bus_annual_simulation.ipynb

## Dependency changes
None.

## Deviations from AGENTS.md
None.

## Public-API changes
- simulate_block_year / simulate_fleet_year: added keep_soc, keep_load_matrix,
  warm_up_days kwargs (all back-compat with prior calls).
- Per-block result dict: new n_active_dates, n_overlap_warnings fields.
```

---

## 11. 实测参考

| 指标 | 期望 |
|---|---|
| `tests/mobility/bus/` 测试数 | ≥ 5 个新增（原文件不删） |
| `pytest tests/mobility/bus/` 总时长 | < 60s |
| Notebook 03 restart-and-run-all | < 30s |
| `grep -rn 'warm_up_days=0' mobility/bus` | 仅在 docstring 与默认值处 |
| `simulate_fleet_year` 内部对 `simulate_block_year` 的调用必须显式 `keep_soc=False, keep_load_matrix=False` | 是 |
