# Prompt: 从零重建 `mobility/bus/` + 写 `notebooks/01_single_bus_simulation.ipynb`

## 0. 上下文

`mobility/bus/` 已被清空（见 `git log` 中删除 5 个 `.py` 的提交）。任务是按 [mobility/cars/](../../mobility/cars/) 的纪律重新设计这个子包，并交付一个**单 bus 单日**的叙事 notebook。

冻结输入：

- `outputs/all_blocks.parquet` (93 MB, 1,668,452 trips, 263,120 blocks, 17 列：`trip_id, agency_id, route_id, service_id, direction_id, block_id, block_source, start_h, end_h, distance_km, start_stop, end_stop, start_lat, start_lon, end_lat, end_lon, shape_id`)
- 旧实现保留在 git history（commit `6299588` 之前）。`infer_blocks` 算法需 `git show 6299588:Modelling/mobility/bus/gtfs_parser.py` 取回

核心约束遵循仓库根 [AGENTS.md](../../../AGENTS.md)：
- 单位后缀 `_kw / _kwh / _soc / _km / _h` 强制
- 时间常量从 [mobility/core/constants.py](../../mobility/core/constants.py) 导入，禁止魔法数 `0.25 / 0.5`
- 随机性走 `np.random.default_rng(seed)`，禁止 `np.random.seed`
- 不引入 `holidays / geopandas / pyproj / shapely / pytz`
- 新增列必须带单位后缀
- parquet 落盘走 pyarrow，禁止 `to_csv` 作为中间产物

---

## 1. 必须在设计中解决的 5 个已知问题

这 5 条**不是 caveat，是验收的一部分**：

### 1.1 跨午夜 trip 不许静默丢弃

`all_blocks.parquet` 中 9.5% 的 block (24,867 个) 至少有一个 `end_h ≥ 24` 的 trip。新 `trip_chain_bus` 必须把这种 block 拆成 2 个 `DailySchedule`：

- `day=0` 装 trips 截到 24h
- `day=1` 装 wrapped tail，时间减 24h

返回 `list[DailySchedule]` 交给 `simulate_single_ev` 处理（`mobility.core.simulator` 已支持多日列表）。

单 trip 横跨午夜（`dep < 24, arr >= 24`）：在 day=0 截到 24.0、在 day=1 接续 `0..arr-24`，能耗按时长比例切分以保持守恒。

### 1.2 inferred 与 native block 的连续性差距必须暴露

实测：相邻 trip `end_stop == next_start_stop` 比例为 native 73.3% vs inferred 49.5%。在 `data_loader.summarize_block_quality()` 中按 `block_source` 分组汇报；selection 工具 `block_source` 参数默认 `'native'`。

### 1.3 distance_km 来源透明

`shape_id` 非空的 trip 用 GTFS shape 长度，否则用 stop-haversine 回退。data_loader 必须附加一列 `distance_source` (`'shape' | 'stop_haversine'`)，summarize 中汇报全表占比（实测 47% / 53%）。stop-haversine 系统性低估 15–25%——notebook 旁注必须说明，但本轮**不修正**。

### 1.4 "depot" 是首末 stop 的占位抽象

`trip_chain_bus` 生成的 depot 段 ParkingEvent 必须用 `location_purpose="depot_terminus"`（不是 `"depot"`），避免与未来的真实 depot 模型混淆。坐标取首 trip `start_lat/lon` 和末 trip `end_lat/lon`，分别记入 `terminus_start_lat/lon`、`terminus_end_lat/lon` 元数据字段（如果 ParkingEvent 不支持，存到 DailySchedule 的字典属性里）。

### 1.5 没有 calendar / service-day 概念

`service_id` 在 parquet 里但 GTFS calendar 没传播。本轮**不解决**。selection 工具不允许 protagonist identity card 输出"日期"字段——只输出 `service_id` 和字符串 `"a representative service day"`。

---

## 2. 模块设计 — `mobility/bus/`

6 个文件，每个 < 300 行，单一职责。

### 2.1 `data_loader.py`

```python
def load_all_blocks(path: Path = DEFAULT_PATH) -> pd.DataFrame:
    """读 parquet，校验 17 列，附加 distance_source 列，校验关键列 0 NA。"""

def summarize_block_quality(df: pd.DataFrame) -> pd.DataFrame:
    """返回单行汇总：n_blocks, n_trips, pct_native, pct_shape_distance,
       stop_continuity_native, stop_continuity_inferred,
       pct_cross_midnight_blocks, layover_h_p50, layover_h_p95."""

def filter_to_clean_blocks(
    df: pd.DataFrame,
    *,
    block_source: tuple[str, ...] = ("native",),
    max_total_km: float = 1000.0,
    min_total_km: float = 30.0,
    allow_cross_midnight: bool = False,
) -> pd.DataFrame:
    """显式过滤，不静默丢数据；过滤标准在返回 DataFrame.attrs 中记录。"""
```

### 2.2 `block_inference.py`

**bit-exact 保留**旧 `gtfs_parser.py:infer_blocks` (209 行 greedy，含 `same_stop_bonus_h / route_continuity_bonus_h / max_layover_h / max_deadhead_km / max_shift_h`)。

允许的重构：

- 拆 `_select_best_candidate(...)` 内联函数
- inputs 改成 typed dataclass `BlockInferenceConfig`
- `haversine_km` 内嵌而不是从其他模块 import

**禁止**：调整打分顺序、改默认参数、用 numba/cython 加速、引入 vectorisation。

### 2.3 `trip_chain_bus.py`

```python
def block_to_daily_schedules(
    block_df: pd.DataFrame,
    ev_id: str,
    *,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
) -> list[DailySchedule]:
    """处理跨午夜：返回 1 或 2 个 DailySchedule。

    每个 DailySchedule 内：
    - trips 按 start_h 排序，能耗 = distance_km * consumption_kwh_per_km
    - 首 trip 之前的空白 → ParkingEvent(location_purpose='depot_terminus',
                                          can_charge=True, charge_power_kw=depot_charge_kw)
    - 末 trip 之后的空白 → 同上
    - 中间 layover：can_charge = allow_layover_charging
                              AND duration_h >= min_layover_for_charging_h
    - 单 trip 横跨午夜（dep<24, arr>=24）：在 day0 截到 24.0、day1 接续 0..arr-24，
      能耗按时长比例切分。
    """
```

辅助函数允许私有；不允许暴露 `block_to_daily_schedule` (单数) 这种容易被误用的接口。

### 2.4 `sim_adapter.py`

```python
def simulate_block(
    block_df: pd.DataFrame,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    soc_init: float = 1.0,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """单 block 端到端：trip_chain → simulate_single_ev (multi-day-safe)。

    返回 {
      'schedules': list[DailySchedule],
      'soc': np.ndarray,        # 长度 = 96 * len(schedules)
      'load_kw': np.ndarray,    # 同上
      'soc_end': float,
      'soc_min': float,
      'energy_charged_kwh': float,
      'depot_kwh': float,
      'layover_kwh': float,
      'total_km': float,
      'total_consumed_kwh': float,
    }
    """

def simulate_fleet_blocks(
    df: pd.DataFrame, *,
    battery_kwh: float, consumption_kwh_per_km: float, depot_charge_kw: float,
    progress_interval: int = 0, **kwargs,
) -> tuple[pd.DataFrame, np.ndarray]:
    """类比旧 simulate_bus_fleet，但走新 trip_chain，正确处理跨午夜。"""
```

### 2.5 `selection.py`

```python
def sample_protagonist_block(
    df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    n_trips_range: tuple[int, int] = (10, 30),
    total_km_range: tuple[float, float] = (30.0, 1000.0),
    block_source: str = "native",
    require_no_cross_midnight: bool = True,
    agency_top_k: int | None = 20,
) -> str:  # block_id

def sample_contrast_block(
    df: pd.DataFrame,
    rng: np.random.Generator,
    protagonist_id: str,
    *,
    require_different_agency_or_km: bool = True,
    km_diff_threshold: float = 0.30,
    **kwargs,
) -> str:
    """与 protagonist 满足 (不同 agency) OR (总 km 差 ≥ 30%) 之一即可。"""

def render_block_identity_card(
    df: pd.DataFrame, block_id: str,
) -> pd.DataFrame:
    """字段：block_id, agency_id, n_trips, total_km, span_h, service_id,
       distance_source_breakdown, stop_continuity, n_routes,
       service_day_label='a representative service day'."""
```

### 2.6 `__init__.py`

仅 re-export 公共表面：

```python
from .data_loader import load_all_blocks, summarize_block_quality, filter_to_clean_blocks
from .sim_adapter import simulate_block, simulate_fleet_blocks
from .selection import sample_protagonist_block, sample_contrast_block, render_block_identity_card
```

其他符号通过子模块路径访问。

---

## 3. 测试 — `tests/mobility/bus/`

至少 6 个 pytest 文件，每个 < 100 行：

| 文件 | 断言 |
|---|---|
| `test_data_loader.py` | schema 17 列齐全；`distance_source` 标注与 `shape_id.notna()` 一致；`summarize_block_quality` 关键比例与本 prompt 实测值差 ≤ 5% |
| `test_block_inference_bitexact.py` | 从 `all_blocks.parquet` 抽 1000 个 inferred block 的 input frame，新函数重跑，断言 `block_id` 序列与 parquet 中现存值完全一致 |
| `test_trip_chain_cross_midnight.py` | 一个手工构造的跨午夜 block 必须返回 2 个 DailySchedule；trip 总能耗在拆分前后守恒 (atol=1e-6)；day1 的 ParkingEvent 起点不晚于 0.0 |
| `test_trip_chain_layover_policy.py` | `min_layover_for_charging_h=0.5` 时 18 分钟的 layover `can_charge=False`；`allow_layover_charging=False` 时所有 layover `can_charge=False`，与 min 阈值无关 |
| `test_sim_adapter_simulate_block.py` | 5-trip 手工 block 的 `energy_charged_kwh ≈ depot_kwh + layover_kwh` (atol=1e-3)；SOC 在 trip 段单调不增、在充电段单调不减；`soc_min` 与 SOC 数组 `.min()` 完全一致 |
| `test_selection.py` | 固定 seed `np.random.default_rng(20260430)`，`sample_protagonist_block` 返回的 block 满足 native + 不跨午夜 + n_trips∈[10,30]；多次调用返回一致 |

测试运行不允许联网，不允许写盘到 `outputs/`。

---

## 4. Notebook — `notebooks/01_single_bus_simulation.ipynb`

配套 builder `_build_01_bus_narrative.py`，与 [_build_00_modelling_narrative.py](../_build_00_modelling_narrative.py) 同结构（`md()` / `code()` helper，`nbf.write` 落盘）。可一键 `python _build_01_bus_narrative.py` 重生 `.ipynb`。

### 章节

| # | 标题 | 关键操作 |
|---|---|---|
| 0 | Units & time grid | 同 notebook 00 的 stub schedule（300 kWh / 100 kW / 18h dwell + 6h charge），跑 `simulate_single_day`，验证 `load_kw × STEP_HOURS = energy_kwh` |
| A | What `all_blocks.parquet` is | 调 `load_all_blocks` + `summarize_block_quality`，画 `n_trips / total_km / span_h` 直方图（log-y） |
| A.5 | **Honest labels for the data** | 一张 5 行表格，逐条列出 §1 的 5 个问题及实测值（cross-midnight 比例、inferred 连续性、distance_source 占比、depot 抽象、service-day 缺失） |
| B | Picking a protagonist | `MAIN_BUS_SEED = 20260430`，`ALT_BUS_SEED = MAIN_BUS_SEED + 1`，调 selection；输出 protagonist + contrast 的 identity card |
| C | Block → schedule | 调 `block_to_daily_schedules`，列 trips + parking_events 表；水平甘特图（trip 橙色、depot_terminus 蓝色、layover 灰色），按 `route_id` 标记 |
| D | Baseline SOC trajectory | `allow_layover_charging=False`，画 SOC（绿）+ load_kw（蓝阶梯）+ 每 trip 能耗扣点（红三角）；旁注 CC-CV 拐点 `CV_THRESHOLD['NMC']=0.80` |
| E | What if we charge during layovers | 同 block 跑两次：`allow=False` vs `allow=True, layover_kw=50, min_layover_for_charging_h=0.25`；叠图 + 4 数对照表（`soc_end / soc_min / energy_charged_kwh / depot_share`） |
| F | Sensitivity grid | 3×3：`battery_kwh ∈ {200, 300, 400}` × `consumption ∈ {0.9, 1.2, 1.5}`，记 `soc_end / soc_min`，画热力图，标注哪些组合 `soc_min < 0.1` |
| G | Fleet context (optional) | 全国 `total_km/day` 直方图，红/橙竖线标 protagonist + contrast。若 `outputs/sim_per_bus.parquet` 不存在（应已被删），打印一行 `"fleet rollup not available — skipped"` 并跳过本节，**不要在 notebook 里 trigger 全国仿真** |
| H | Final identity card | 13 个数字的 markdown 表 + wall-clock |

### 代码规范

- 全程 < 60 秒（不计首次读 parquet）
- 所有图 `figsize=(12, 4.5), dpi=110`，关上/右 spine
- 不导入 `mobility.cars` 子包
- notebook 顶部 `import` 段必须包含 `mobility.bus` 的 6 个公共符号
- 不写 `df.to_csv` 或 `df.to_parquet`
- 不在 notebook 内写 `%pip install`

---

## 5. 不要做的事

- 不要重 build `all_blocks.parquet`（会重跑 30 分钟 GTFS 流式）
- 不要新引入依赖
- 不要为 layover 真的去匹配充电站（机会充电在本轮纯 what-if）
- 不要把 `block_inference.py` 改快、改默认参数或改打分顺序——任何变动必须在 PR description 单开 "Inference algorithm change" 段
- 不要在任何文件里写 `np.random.seed` / `random.seed` / `random.choice` 的无 rng 调用
- 不要把 cross-midnight trip 用 `df = df[df.end_h<24]` 过滤掉，必须走 §1.1 的拆分路径
- 不要在 ParkingEvent 上新增字段而不带单位后缀（违反 [AGENTS.md](../../../AGENTS.md) §3.1）
- 不要把 stop-haversine 距离低估静默"修正"——这是已知偏差，本轮只标注不动它
- 不要导入旧 `gtfs_parser.py` 中除 `infer_blocks` 之外的函数（`parse_gtfs_time / shape_length_km / build_trip_span` 等不再需要——`all_blocks.parquet` 已是处理后的输入）

---

## 6. PR 交付物（沿用 [AGENTS.md](../../../AGENTS.md) §6）

1. **代码变更** 严格落在：
   - `Modelling/mobility/bus/{__init__,data_loader,block_inference,trip_chain_bus,sim_adapter,selection}.py`
   - `Modelling/notebooks/{01_single_bus_simulation.ipynb,_build_01_bus_narrative.py}`
   - `Modelling/tests/mobility/bus/test_*.py`
2. **`CHANGELOG.md` 追加一条**（项目根的 CHANGELOG，如果不存在则新建于 `Modelling/CHANGELOG.md`）：
   ```
   ## Bus module redesign · single-bus narrative (YYYY-MM-DD)
   - Rebuilt mobility/bus/ around DailySchedule semantics consistent with mobility/cars/
   - trip_chain_bus correctly handles 9.5% of blocks that span midnight
   - Added notebook 01 with explicit data-quality disclosures
   - block_inference.py preserves the legacy greedy algorithm bit-exactly
   ```
3. **PR description** 包含：
   - `## Summary`：3–5 行
   - `## Verification`：贴 6 个 pytest 输出 + notebook restart-and-run-all 关键单元截图
   - `## Dependency changes`：None（如有，单独段说明）
   - `## Deviations from AGENTS.md`：None 或逐条列出
   - `## Inference algorithm changes`：None（必须）
4. 不允许 force push、不允许 `--no-verify`、不允许在 main 直接提交

---

## 7. 实测参考值（验收时对比用）

供 `summarize_block_quality` 测试和 notebook A.5 节使用：

| 指标 | 实测值 |
|---|---|
| 总 trips | 1,668,452 |
| 总 blocks | 263,120 |
| `block_source == 'native'` 占比 | 34.6% (577,895 / 1,668,452) |
| `shape_id` 非空占比 | 46.6% (777,980) |
| native 相邻 trip stop-continuity | 73.3% |
| inferred 相邻 trip stop-continuity | 49.5% |
| 跨午夜 block 占比 | 9.5% (24,867) |
| layover_h 中位数（全部） | 0.100 h (6 min) |
| layover_h P95 native | 2.033 h |
| layover_h P95 inferred | 0.300 h |
| 满足 protagonist 默认筛选条件的候选 block 数 | 19,191 |

---

*Last reviewed: 2026-04-30. 修改本 prompt 与代码 PR 应分开提交。*
