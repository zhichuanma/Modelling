# Prompt: 修复 coach 模块 review 发现的 7 个真问题 + 4 个 nice-to-have + 测试补齐

## 0. 上下文

`mobility/coach/` 已按 [03_coach_redesign.md](03_coach_redesign.md) 落地，但 review 发现 7 个会埋雷的真问题（数据源偏离规范、`terminus_kwh` 永远为 0、`feasibility` 契约不全、`sample_coach_ev` 签名颠倒、`distance_source` 标签字面量错误、`start_h >= 24` 静默通过、测试依赖 repo 外绝对路径），加 4 个值得在同一 PR 一起做掉的清理项。

继承约束：仓库根 [AGENTS.md](../../../AGENTS.md) 全部硬规则（单位后缀 / RNG / parquet / 不引入 `holidays/geopandas/pyproj`）。**不允许改 [`mobility/coach/txc_parser.py`](../../mobility/coach/txc_parser.py)**——它是冻结基座（575 行）。

---

## 1. P1 · `coach_fleet.py` 数据源完全偏离规范

### 现状

[03_coach_redesign.md §2.5](03_coach_redesign.md) 明确要求：

```python
COACH_FLEET_PATH = Path(__file__).resolve().parents[2] / "data" / "EV_UK_LSOA_2025_with_energy.csv"

def load_coach_fleet(path=COACH_FLEET_PATH) -> pd.DataFrame:
    """Filter vehicle_subtype == 'coach', drop NaN Energy_kWh / efficiency_wh_per_km.
    Returns: EV_ID, Model, Energy_kWh, DC_Power_kW, AC_Power_kW,
             efficiency_wh_per_km, consumption_kwh_per_km, LSOA_code, count.
    """
```

但 [coach_fleet.py:14](../../mobility/coach/coach_fleet.py#L14) 实际加载的是 `Data/EV/EV_prepared/BEV_Bus_Coach_unique_with_params_with_AC.csv`，并做了 4-CSV 合并（BEV_Bus_Coach + variants_json + manual_worklist + subtype_lookup），返回小写列名。这是 [03_coach_redesign.md §5](03_coach_redesign.md) 警告的过度工程，且使 cars/coach 之间风格不再一致。

`sample_coach_ev` 也错了：当前签名是 `(rng, fleet_df)` 返回 `dict`；规范是 `(fleet_df, rng, *, weight_by_count=True)` 返回 `pd.Series`。

### 要做的事

重写 [coach_fleet.py](../../mobility/coach/coach_fleet.py)：

1. `COACH_FLEET_PATH` 指向 `Modelling/data/EV_UK_LSOA_2025_with_energy.csv`
2. `load_coach_fleet(path=COACH_FLEET_PATH)` 只做：读 CSV → `vehicle_subtype == 'coach'` → drop NaN `Energy_kWh` 与 `efficiency_wh_per_km` → 派生 `consumption_kwh_per_km = efficiency_wh_per_km / 1000.0` → 返回规范 9 列
3. 删除 variants_json / manual_worklist / subtype_lookup 合并路径（regression/审计是上游 ETL 的事，不属于这个仿真包）
4. `sample_coach_ev(fleet_df, rng, *, weight_by_count=True) -> pd.Series` 严格按规范签名实现，返回 `pd.Series` 行
5. 调用方（[sim_adapter.py:43](../../mobility/coach/sim_adapter.py#L43)、[notebooks/_build_02_coach_narrative.py](../_build_02_coach_narrative.py)）一并改为按位置读 `Energy_kWh / consumption_kwh_per_km`

### 验收

- `tests/mobility/coach/test_coach_fleet.py` 通过 fixture CSV（见 P7）跑 ≥ 2 行（YUTONG TC12、GTE14），所有行 `Energy_kWh > 0` 且 `consumption_kwh_per_km > 0`
- `sample_coach_ev(fleet_df, rng=np.random.default_rng(0))` 在固定 seed 下确定性、返回 `pd.Series`
- notebook A.7 节列表里的字段名与规范 9 列一致

---

## 2. P2 · `terminus_kwh` 永远是 0

### 现状

[sim_adapter.py:71-78](../../mobility/coach/sim_adapter.py#L71) 用：

```python
terminus_kwh = sum(
    event.energy_charged_kwh
    for sched in result["schedules"]
    for event in sched.parking_events
    if event.location_purpose == "terminus_dwell"
)
```

但 [`_terminus_event`](../../mobility/coach/trip_chain_coach.py#L93) 创建 `ParkingEvent` 后从不写 `energy_charged_kwh`，且 `simulate_single_ev`（[mobility/core/simulator.py](../../mobility/core/simulator.py)）也不会回写到 `ParkingEvent` 上。所以 `terminus_kwh` 在所有路径上都是 0。[test_sim_adapter_feasible.py:26](../../tests/mobility/coach/test_sim_adapter_feasible.py#L26) 只断言 `>= 0.0`，掩盖了这个 bug。

### 要做的事

按 bus 的做法（[mobility/bus/sim_adapter.py:20-28](../../mobility/bus/sim_adapter.py#L20)）从 `load_kw` + 时间索引算：

```python
def _energy_in_window_kwh(load_kw: np.ndarray, start_h: float, end_h: float, step_h: float) -> float:
    start_idx = int(round(start_h / step_h))
    end_idx   = int(round(end_h / step_h))
    return float(load_kw[start_idx:end_idx].sum() * step_h)
```

聚合时遍历每个 schedule 的 `parking_events`，把 `location_purpose == 'terminus_dwell'` 段的能量加起来。注意跨午夜场景下 schedule 的 `day` 偏移要乘进去。

### 验收

新增/加严测试：

- `test_sim_adapter_feasible.py`：断言 `result["terminus_kwh"] > 0` 当 `terminus_charge_kw=50` 且 SOC 起始 < 1.0 时（不要再用 `>= 0.0`）
- 守恒断言：`abs(terminus_kwh - energy_charged_kwh) < 1e-6`（coach 只有 terminus_dwell 一种充电场景，两者必须相等）

---

## 3. P3 · `feasibility.py` 契约不完整

### 现状

[03_coach_redesign.md §2.3](03_coach_redesign.md) 规定返回 dict 包含：

```python
{'feasible_single_charge', 'energy_required_kwh',
 'usable_energy_kwh', 'shortfall_kwh', 'min_soc_required'}
```

[feasibility.py:39](../../mobility/coach/feasibility.py#L39) 实际返回 `usable_battery_kwh` 而不是 `usable_energy_kwh`，并完全没有 `min_soc_required`。

### 要做的事

修 [feasibility.py](../../mobility/coach/feasibility.py)：

```python
def journey_feasibility(distance_km: float, *, battery_kwh: float,
                        consumption_kwh_per_km: float,
                        safety_margin: float = 0.05) -> dict:
    energy_required_kwh = distance_km * consumption_kwh_per_km
    usable_energy_kwh   = battery_kwh * (1.0 - safety_margin)
    shortfall_kwh       = max(0.0, energy_required_kwh - usable_energy_kwh)
    min_soc_required    = energy_required_kwh / battery_kwh + safety_margin
    return {
        "feasible_single_charge": shortfall_kwh == 0.0,
        "energy_required_kwh":    float(energy_required_kwh),
        "usable_energy_kwh":      float(usable_energy_kwh),
        "shortfall_kwh":          float(shortfall_kwh),
        "min_soc_required":       float(min_soc_required),
    }
```

下游 [sim_adapter.py](../../mobility/coach/sim_adapter.py) 与 [notebooks/_build_02_coach_narrative.py](../_build_02_coach_narrative.py) Stage E 相应改字段名。

### 验收

- `test_feasibility.py` 加断言：`'min_soc_required' in result and 0 <= result['min_soc_required'] <= 2.0`
- 移除任何对 `usable_battery_kwh` 的引用（grep 全包）

---

## 4. P4 · `distance_source` 标签字面量错

### 现状

[distance.py:78](../../mobility/coach/distance.py#L78) 当前生成 `f"haversine_x{road_detour_factor:.2f}"`（例如 `"haversine_x1.30"`）。规范 [§1.1 / §2.2](03_coach_redesign.md) 要求**字面量** `"haversine_x_detour"`。当前测试钉住了错误契约。

### 要做的事

- [distance.py:78](../../mobility/coach/distance.py#L78) 改回字面量 `"haversine_x_detour"`
- detour factor 单独存到一个新列 `road_detour_factor` (float) 写进 `outputs/all_coach_journeys.parquet`（这样 audit 还能复现），但 `distance_source` 只取 `{'haversine_x_detour', 'unknown'}` 两个值
- [test_distance.py](../../tests/mobility/coach/test_distance.py) 改成断言字面量 `'haversine_x_detour'`，以及 `road_detour_factor=1.5` 时输出列里的 detour 列等于 1.5 但 source label 不变

### 验收

`grep -r "haversine_x" mobility/coach tests/mobility/coach` 只剩字面量 `"haversine_x_detour"` 一个变体。

---

## 5. P5 · `start_h >= 24` 静默通过 + cross-midnight 边界

### 现状

[03_coach_redesign.md §1.2](03_coach_redesign.md) 规范："`start_h ≥ 24`：理论上不该出现，如果出现 raise"。当前 [trip_chain_coach.py:79](../../mobility/coach/trip_chain_coach.py#L79) 用 `start < 0 or end > 2*HOURS_PER_DAY` 拦截，对 `start_h >= 24` 不报错——会静默走 else 分支生成奇怪 schedule。

[trip_chain_coach.py:50](../../mobility/coach/trip_chain_coach.py#L50) 还会把 `end == 48`（合法的 day1 24:00 收尾）当 reject。

### 要做的事

把入口检查改成两条：

```python
if start_h >= HOURS_PER_DAY:
    raise ValueError(f"start_h={start_h} must be < 24 (vehicle journeys never start in 'next day' clock)")
if end_h > 2 * HOURS_PER_DAY:
    raise ValueError(f"end_h={end_h} exceeds 48h; cross-midnight beyond day1 not supported")
```

并把 `_split_trip` 的能耗按 `(end - start) / total_duration` 切分（保持当前能量守恒），但要给跨午夜分支补单元测试覆盖 `end_h == 24.0` 边界（一个 trip 恰好 23:00–24:00 不应该触发拆分，但 23:30–24:30 应该拆出 day0 + day1）。

### 验收

- 新增 `tests/mobility/coach/test_trip_chain_boundaries.py`：
  - `start_h=24.5` raise
  - `end_h=49.0` raise
  - `start_h=23.0, end_h=24.0` 不拆分（day0 单 schedule）
  - `start_h=23.5, end_h=25.0` 拆 day0+day1，能量守恒到 1e-9
- 旧的 [test_cross_midnight.py](../../tests/mobility/coach/test_cross_midnight.py) 保留，60/60 km 守恒测试不动

---

## 6. P6 · `selection.py` 缺规范参数

### 现状

[03_coach_redesign.md §2.6](03_coach_redesign.md)：

```python
sample_protagonist_journey(..., runtime_h_range=(1.0, 8.0),
                           require_no_cross_midnight=True,
                           require_known_distance=True)
sample_contrast_journey(..., require_distance_gap=0.5, ...)
```

[selection.py:36](../../mobility/coach/selection.py#L36) 实际只过滤 known-distance + cross-midnight，缺 `runtime_h_range` 和 `require_distance_gap`。

### 要做的事

补齐三个参数。`require_distance_gap=0.5` 的语义：抽出来的 contrast 必须满足 `|c.distance_km - p.distance_km| / max(p.distance_km, 1) >= 0.5`，否则在剩余候选里继续抽（最多重试 N 次后 raise，不要无限循环）。`runtime_h_range` 在抽样前做 `df.loc[(df.runtime_h >= lo) & (df.runtime_h <= hi)]` 过滤。

### 验收

加 `tests/mobility/coach/test_selection.py`（沿用现有文件加 case）：

- 固定 seed `np.random.default_rng(20260501)`，protagonist 满足 `1 ≤ runtime_h ≤ 8` 且 `distance_source == 'haversine_x_detour'`
- contrast 与 protagonist 距离差 ≥ 50%
- 6 个 operator 在大量重复抽样（n=200）下都能至少被抽到 1 次（统计而非确定性断言；用 `assert len(set_of_operators) >= 5` 容忍 1 个 operator 缺席的偶发）

---

## 7. P7 · 测试依赖 repo 外绝对路径

### 现状

[test_coach_fleet.py:9](../../tests/mobility/coach/test_coach_fleet.py#L9) `load_coach_fleet()` 无参调用命中 `../Data/EV/EV_prepared/...`——CI 上不存在这个路径，测试必挂。规范 [§3](03_coach_redesign.md) 明令"禁止依赖 `../Data/EV_behavior/`"。

### 要做的事

新增 fixture `tests/mobility/coach/fixtures/coach_fleet_minimal.csv`：

- 5–10 行，覆盖 YUTONG TC12 / GTE14 两款（至少 1 行 NaN energy 测过滤）
- 列与 `EV_UK_LSOA_2025_with_energy.csv` 一致（特别是 `vehicle_subtype` 列）
- 文件 < 2 KB

`test_coach_fleet.py` 改成显式传 fixture 路径：

```python
FIXTURE = Path(__file__).parent / "fixtures" / "coach_fleet_minimal.csv"
def test_load_coach_fleet_filters_to_coach():
    df = load_coach_fleet(FIXTURE)
    assert (df["vehicle_subtype"] == "coach").all()
    assert (df["Energy_kWh"] > 0).all()
```

### 验收

- `pytest tests/mobility/coach/ -v` 在没有 `../Data/` 的环境下 100% 通过
- `grep -rn '../Data' tests/mobility/coach/` 无结果

---

## 8. N1–N4 · Nice-to-have（同 PR 顺手做掉）

### N1 · Stage E `* 0.95` magic number

[_build_02_coach_narrative.py:295](../_build_02_coach_narrative.py#L295) 用 `usable = battery_kwh * 0.95` 内联做 5% safety margin。改用 `journey_feasibility(...)` 调用结果，避免默认值漂移导致 frontier 与 `result["feasibility"]` 不一致。

### N2 · 距离循环未向量化

[data_loader.py:130-138](../../mobility/coach/data_loader.py#L130) 每 journey 调一次 `vehicle_journey_distance_km`，而 [distance.py:61](../../mobility/coach/distance.py#L61) 内部每次重建 NaPTAN coords 索引。14k journeys × 434k NaPTAN 行 ≈ 5–10× 加速空间。

修法：在 `distance.py` 顶层加 `build_coords_lookup(stops_geom: pd.DataFrame) -> dict[str, tuple[float, float]]`；`vehicle_journey_distance_km` 接受可选 `coords: dict | None`（None 时自建以兼容旧调用）；`build_all_coach_tables`（[data_loader.py](../../mobility/coach/data_loader.py)）在循环外建一次 `coords` 传入。

### N3 · `selection.py` 与 `sim_adapter.py` 三个相似 helper

`_field` / `_row_get` / `_spec_value` 跨 [selection.py](../../mobility/coach/selection.py)、[sim_adapter.py](../../mobility/coach/sim_adapter.py)、[trip_chain_coach.py](../../mobility/coach/trip_chain_coach.py) 三个文件实现近同源逻辑（按 key 取 `pd.Series` / `dict` 字段并 default）。整合到 [mobility/coach/_compat.py](../../mobility/coach/) 暴露 `field(row, key, default=None)`，三处调用方统一。

### N4 · `data_loader.py` 错误信息可读性

[data_loader.py](../../mobility/coach/data_loader.py) 的 `_validate_columns` raise 时不打印实际存在列；改成 `raise ValueError(f"missing {missing!r}; got {list(df.columns)}")`。

### 验收

- N1：notebook Stage E 不再有内联 `0.95` magic
- N2：`build_all_coach_tables` 实测时长（log 时间）从 ~Xs 降到 ~X/5 量级，写一句注释说明
- N3：`grep -rn "_field\|_row_get\|_spec_value" mobility/coach` 只剩 `_compat.py` 一份
- N4：人工触发缺列错误，错误信息含 `got [...]`

---

## 9. 测试覆盖补齐（在 P1–P7 之外）

| 新增测试 | 断言 |
|---|---|
| `test_sim_adapter_terminus_kwh.py` | `terminus_kwh ≈ energy_charged_kwh` 守恒（P2 闭环） |
| `test_trip_chain_boundaries.py` | `start_h>=24` raise / `end_h>48` raise / 24h 边界正确（P5 闭环） |
| `test_distance.py` 加 case | `road_detour_factor=1.5` 时距离 = haversine × 1.5 但 `distance_source == 'haversine_x_detour'` |
| `test_selection.py` 加 case | 6 operator 可达性（统计断言） |
| `test_data_loader.py` 加 case | 当 stops_geom 缺一个 stop 时该 journey `distance_source == 'unknown'`（unknown 分支） |
| `test_coach_fleet.py` 重写 | fixture-based，验证 NaN 过滤、weight_by_count 在大 N 下分布近似 |

---

## 10. 不要做的事

- 不要改 [`mobility/coach/txc_parser.py`](../../mobility/coach/txc_parser.py)（575 行冻结基座）
- 不要 import `mobility.bus.*` / `mobility.cars.*`
- 不要在测试里联网或写盘到 `outputs/`
- 不要把 `np.random.seed` / `random.seed` / `random.choice(无 rng)` 写进任何文件
- 不要把 `terminus_charge_kw` 默认值升到 100 kW（保持 50.0）
- 不要恢复 4-CSV 合并管线——上游 ETL 是另一回事
- 不要把 `road_detour_factor` 默认值改成 1.30 以外的值
- 不要 `--no-verify` 跳过 hooks

---

## 11. PR description 必须包含

```markdown
## Summary
Seven review fixes on the coach simulation layer plus four cleanups:
- Restore spec data source for load_coach_fleet (EV_UK_LSOA_2025_with_energy.csv).
- Compute terminus_kwh from load_kw windows; was silently 0.
- Add min_soc_required and rename usable_energy_kwh in feasibility contract.
- Fix sample_coach_ev signature and return pd.Series.
- Restore literal 'haversine_x_detour' distance_source label; surface detour factor as a separate column.
- Raise on start_h >= 24 in trip_chain_coach; correct 24h boundary.
- Switch test_coach_fleet to a repo-local fixture.
- Plus: vectorise distance loop, dedupe row-field helper, polish notebook Stage E.

## Verification
- pytest tests/mobility/coach/ -v   →   N/N passed (paste output)
- python -m mobility.coach.build_all_journeys   →   wrote outputs/all_coach_journeys.parquet
- jupyter nbconvert --execute notebooks/02_single_coach_simulation.ipynb   →   completed in <X>s

## Dependency changes
None.

## Deviations from AGENTS.md
None.

## txc_parser.py changes
None (frozen base).
```

---

## 12. 实测参考

| 指标 | 期望 |
|---|---|
| `tests/mobility/coach/` 测试数 | ≥ 13（原 9 + 新增 4） |
| `pytest tests/mobility/coach/` 总时长 | < 30s |
| `build_all_coach_tables` 重跑时长 | < 30s（含 NaPTAN 索引一次性构建） |
| `grep -rn 'usable_battery_kwh\|haversine_x1\.\|sample_coach_ev(rng' mobility tests notebooks` | 0 命中 |
| Notebook 02 restart-and-run-all | < 30s |
