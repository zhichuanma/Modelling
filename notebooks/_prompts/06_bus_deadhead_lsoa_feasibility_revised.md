# Prompt: Bus duty cycle 完整重做 — `infer_blocks` 时间可行性 + 分层 deadhead + LSOA/DZ 多边形 + Fleet 不可行性审计

> 用途：把本文直接交给 coding agent。本文是对原 plan 的实施级修订版：保留总体路线，但修正了坐标系、polygon hole 语义、SOC clamp 审计、deadhead 统计、notebook 全量运行风险、测试数量等关键歧义。
>
> Last reviewed: 2026-05-06  
> Scope: `Nature_EV_2025/Modelling/mobility/bus` + `mobility/core/spatial.py` + tests + notebook 01 + 必要 scripts。

---

## 0. 本版相对原 plan 的关键修正

1. **Scotland Shapefile 转 GeoJSON 必须先转 EPSG:4326**。原 plan 的 `gpd.read_file(...).to_file(..., driver="GeoJSON")` 不够安全；如果 shapefile 是 British National Grid，Modelling 端用经纬度 point-in-polygon 会全部错位。
2. **`mobility/core/spatial.py` 只做 polygon 查询，不做 centroid fallback**。fallback 继续留在 `mobility/bus/data_loader.attach_lsoa`，复用现有 centroid join 逻辑，避免 core 依赖 bus 数据文件或形成循环 import。
3. **不要把 MultiPolygon / holes 全部 flatten 成同一层 rings**。需要保留 `polygon -> exterior + holes` 的层级，否则洞、多 part polygon、边界点会出现错误归属。
4. **`infer_blocks` 除 deadhead 时间硬过滤外，还必须保留 `max_shift_h` 硬过滤和 deterministic tie-break**，避免新算法生成不可复现 block。
5. **deadhead 注入不能静默跳过“时间不够”的长距离 pair**。跳过时要返回 audit stats，例如 `deadhead_skipped_time_count/km`，否则 native block 中 20–200 km 级别 deadhead 会继续被低估且不可见。
6. **fleet infeasibility 不能只读 clamped SOC 数组推断 shortfall**。必须在 bus feasibility 里做一条不 clamp 的 shadow SOC walk，`shortfall_kwh = max(0, -min_shadow_soc * battery_kwh)`。
7. **full-fleet audit 建议落到 script，notebook Stage I 优先读取 audit 输出**。Notebook 不写 parquet/csv；若 audit 文件不存在，可跑 deterministic sample 并明确标注 sample，不要让 restart-and-run-all 失控。
8. **新增测试数修正为至少 19 个**：time feasibility 3 + deadhead injection 5 + point-in-polygon 3 + attach_lsoa polygon 3 + feasibility 5。若加入 skipped-deadhead audit，可多 1–2 个测试。

---

## 1. 背景与硬约束

`mobility/bus/` 已经按 `01_bus_redesign.md`、`02_bus_review_fixes.md`、`05_bus_annual_review_fixes.md` 完成单日和全年仿真。但实测仍有 4 类风险需要一次性收口：

1. **Inferred block 时间不可行**：旧 `infer_blocks` 贪心打分只看 wait、same stop、route continuity，没有 30 km/h deadhead 物理约束。
2. **Trip 间 deadhead 没进入能耗链**：`end_stop != next_start_stop` 时，车辆实际移动被 layover 吃掉，能耗被低估。
3. **LSOA 最近质心匹配会错挂边界/海岸/机场附近 stop**：需要升级为 point-in-polygon，并只对失败点回退 centroid。
4. **SOC 触底被 core simulator clamp 到 0**：fleet 层面需要后置 feasibility audit，不能改 `_soc_walk`。

### 1.1 不可违反的约束

遵守仓库根 `AGENTS.md`：

- 不引入 `geopandas / pyproj / pyshp / shapely / fiona / holidays` 到 `Modelling` 运行时依赖。
- 不改 `mobility/core/simulator._soc_walk`。
- 不跨包 import `mobility.coach.*` 或 `mobility.cars.*`。
- 不在 notebook 里 `pip install`、`df.to_csv`、`df.to_parquet`。
- 不用 `--no-verify` 跳过 hooks。
- 所有新增随机/采样必须 deterministic，并显式 seed；没有必要不要引入 RNG。
- 新字段必须保持向后兼容：dataclass 只追加带默认值字段；旧 parquet/schema 的既有字段不删除。

### 1.2 现有实测参考

```text
Block 来源:
  inferred  174,349  (66.3%)
  native     88,771  (33.7%)

Block 内相邻 trip 对的 stop 不连续率:
  整体    42.2% (592,793 / 1,405,332)
  native  26.7%
  inferred 50.5%

不连续对的 haversine 距离:
  整体     median 0.06 km, p90 0.27 km
  native   median 0.05 km, p99 20.37 km, MAX 216.71 km
  inferred median 0.06 km, p99 0.88 km, max 1.00 km

时间可行性，按 30 km/h 平均 deadhead 速度:
  inferred 内 15.7% 时间不可行
  native    内  0.4% 时间不可行
```

---

## 2. 关键文件与数据位置

| 用途 | 文件 |
|---|---|
| block inference 主目标 | `mobility/bus/block_inference.py` |
| bus trip chain / deadhead 注入 | `mobility/bus/trip_chain_bus.py` |
| Trip / ParkingEvent 数据契约 | `mobility/core/data_structures.py` |
| bus single/fleet simulation adapter | `mobility/bus/sim_adapter.py` |
| LSOA 现状 centroid join | `mobility/bus/data_loader.py::attach_lsoa` |
| core spatial 新模块 | `mobility/core/spatial.py` |
| bus feasibility 新模块 | `mobility/bus/feasibility.py` |
| bus bit-exact 测试参考 | `tests/mobility/bus/test_block_inference_bitexact.py` |
| notebook 参考 | `notebooks/_build_01_bus_narrative.py` + `notebooks/01_single_bus_simulation.ipynb` |
| 用户已有 small-area boundary | `../Data/Loads/`，相对 `Modelling/` |
| LSOA/DZ 合并参照 | `../Data/Loads/loads.ipynb` cells 6–12, 20–28 |

### 2.1 Boundary 三件套

| 区域 | 文件 | 格式 | code 列 | 命名空间 |
|---|---|---|---|---|
| England + Wales | `../Data/Loads/Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4_-4299016806856585929.geojson` | GeoJSON | `LSOA21CD` | `EW_LSOA21` |
| Scotland | `../Data/Loads/SG_DataZoneBdry_2022/SG_DataZone_Bdry_2022.shp` | Shapefile input | `dzcode` | `Scotland_DZ2022` |
| Scotland converted | `../Data/Loads/SG_DataZone_Bdry_2022.geojson` | GeoJSON output | `dzcode` | `Scotland_DZ2022` |
| Northern Ireland | `../Data/Loads/DZ2021.geojson` | GeoJSON | `DZ2021_cd` | `NI_DZ2021` |

注意：`start_lsoa/end_lsoa` 列名保持兼容，但 Scotland/NI 实际写入的是 Data Zone code。`*_lsoa_source` 用来区分 code namespace。

---

## 3. Pre-flight：实施前必须先做

所有命令默认从 `Nature_EV_2025/Modelling` 执行。

### 3.1 找到 block build 入口

```bash
git log --all --diff-filter=A -- '**/build_all_blocks*'
find .. -maxdepth 6 \( -name 'build_all_blocks*' -o -name 'build_blocks*' \) -not -path '*/.*' 2>/dev/null
```

如果找不到 canonical build command：

- 不要伪造 `outputs/all_blocks.parquet`。
- 不要重写 bit-exact baseline。
- 在 PR / agent report 中明确列出缺失项：raw GTFS 输入路径、上一版 parquet 生成脚本、预期 build command。

### 3.2 确认 EW / NI boundary 已存在

```bash
ls -la ../Data/Loads/Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4*.geojson
ls -la ../Data/Loads/DZ2021.geojson
ls -la ../Data/Loads/SG_DataZoneBdry_2022/SG_DataZone_Bdry_2022.shp
```

### 3.3 在 `../Data/Loads/loads.ipynb` 转换 Scotland shapefile

这一步允许使用 `geopandas`，因为它发生在 `Data/Loads/loads.ipynb` 的数据准备环境，不进入 `Modelling` 运行时依赖。

```python
from pathlib import Path
import geopandas as gpd

loads_dir = Path(".")  # loads.ipynb 位于 Data/Loads/ 时
scot_shp = loads_dir / "SG_DataZoneBdry_2022" / "SG_DataZone_Bdry_2022.shp"
scot_geojson = loads_dir / "SG_DataZone_Bdry_2022.geojson"

gdf = gpd.read_file(scot_shp)
if gdf.crs is None:
    raise ValueError("Scotland Data Zone shapefile has no CRS; cannot safely convert to lon/lat.")

gdf = gdf.to_crs("EPSG:4326")
gdf.to_file(scot_geojson, driver="GeoJSON")
print(scot_geojson, len(gdf), gdf.crs)
```

### 3.4 验证三份 GeoJSON 都是 lon/lat 坐标

Modelling 端也要做 runtime validation。这里给 pre-flight smoke check：

```bash
python - <<'PY'
import json
from pathlib import Path

paths = [
    Path('../Data/Loads/Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4_-4299016806856585929.geojson'),
    Path('../Data/Loads/SG_DataZone_Bdry_2022.geojson'),
    Path('../Data/Loads/DZ2021.geojson'),
]

def first_xy(coords):
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    return coords[:2]

for path in paths:
    data = json.loads(path.read_text())
    xy = first_xy(data['features'][0]['geometry']['coordinates'])
    x, y = float(xy[0]), float(xy[1])
    ok = -12.0 <= x <= 4.0 and 48.0 <= y <= 62.5
    print(path.name, xy, 'lonlat_ok=', ok)
    if not ok:
        raise SystemExit(f'{path} does not look like EPSG:4326 lon/lat')
PY
```

---

## 4. P1 — 重写 `block_inference.infer_blocks`：时间可行性硬过滤 + deadhead-aware scoring

### 4.1 目标

旧算法只过滤 layover 和 deadhead 距离，导致 inferred 链中出现大量“看似等待时间足够，实际 deadhead 开不过去”的接续。新算法必须把 deadhead 物理时间作为硬约束。

### 4.2 `BlockInferenceConfig` 新字段

在 `mobility/bus/block_inference.py` 的 `BlockInferenceConfig` 末尾追加字段，默认值如下：

```python
@dataclass
class BlockInferenceConfig:
    same_stop_bonus_h: float = 1.0
    route_continuity_bonus_h: float = 0.5
    max_layover_h: float = 4.0
    max_shift_h: float = 16.0

    # NEW: deadhead-aware inference
    deadhead_speed_kmh: float = 30.0
    min_dwell_after_deadhead_h: float = 0.05
    max_inferred_deadhead_km: float = 5.0
    deadhead_penalty_h_per_km: float = 0.05
```

### 4.3 Candidate 评估逻辑

替换主循环里的 candidate 评估段。必须先硬过滤，再打分。

```python
deadhead_km = haversine_km(
    prev_end_lat,
    prev_end_lon,
    cand_start_lat,
    cand_start_lon,
)
deadhead_h = deadhead_km / config.deadhead_speed_kmh
gap_h = candidate_start_h - prev_end_h

# Hard filters: no scoring can override these.
if not (0.0 < gap_h <= config.max_layover_h):
    continue
if deadhead_km > config.max_inferred_deadhead_km:
    continue
if gap_h < deadhead_h + config.min_dwell_after_deadhead_h:
    continue
if candidate_end_h - block_start_h > config.max_shift_h:
    continue

score = gap_h - deadhead_h
score += config.deadhead_penalty_h_per_km * deadhead_km
score -= config.same_stop_bonus_h * float(prev_end_stop_id == cand_start_stop_id)
score -= config.route_continuity_bonus_h * float(prev_route_id == cand_route_id)
```

Tie-break 必须 deterministic。推荐排序键：

```python
(score, candidate_start_h, candidate_end_h, str(route_id), str(trip_id))
```

如果现有代码已有稳定排序，则保留并补齐 trip_id/route_id tie-break。

### 4.4 不变量

- 对原生 block：`block_source == "native"` 的 block_id 与内部 trip 归属不得改变。
- 只对缺失 block_id 的行做 inference。
- 默认权重需满足：`same_stop_bonus_h (1.0) > route_continuity_bonus_h (0.5) > deadhead_penalty_h_per_km * max_inferred_deadhead_km (0.25)`。
- 对无效经纬度：不能把无坐标 candidate 当成 0 km deadhead；应跳过该 deadhead-based candidate，或只允许 same-stop 且 stop_id 相同的接续。实现时写清楚注释并覆盖测试。
- 不为旧 inferred block_id 做兼容映射；P5 会重生 parquet 和 bit-exact baseline。

### 4.5 测试

新增 `tests/mobility/bus/test_block_inference_time_feasibility.py`：

1. `7:00–8:00` trip A，candidate `8:03` 从 30 km 外 stop B 出发；`30 / 30 = 1h`，gap 仅 0.05h，应拒绝接链。
2. 同样 candidate 改为 `9:30` 出发，gap 1.5h，应允许接链。
3. 单调性测试：`deadhead_km=5, same_stop=False` 的 score 必须大于 `deadhead_km=0, same_stop=True`，即后者更优。

---

## 5. P2 — 距离分层 deadhead 注入：把车辆空驶变成真实 `Trip`

### 5.1 目标

在相邻 service trips 的 end stop 与 next start stop 不同且距离足够大时，注入一段 `is_deadhead=True` 的 Trip，使 simulator 正常扣能耗，并让 layover 自动缩短。

### 5.2 `Trip` dataclass 追加字段

在 `mobility/core/data_structures.py` 的 `Trip` dataclass 末尾追加默认值字段：

```python
is_deadhead: bool = False
deadhead_class: str = ""  # "", "short", "long"
```

同时检查所有 `Trip(...)` 构造点、`asdict`、parquet serialization、plotting code。旧 service trip 不传这两个字段时行为必须不变。

### 5.3 bus 内部 distance helper

不要 import `mobility.coach.distance`。推荐新增或复用 bus-only helper：

```python
# mobility/bus/distance.py, or local helper if project style prefers.
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ...
```

`block_inference.py` 与 `trip_chain_bus.py` 可共同使用 bus-local helper，避免重复实现漂移。

### 5.4 Deadhead 常量

```python
DEADHEAD_NOISE_KM = 0.5
DEADHEAD_SHORT_KM = 5.0
DEADHEAD_SPEED_KMH = 30.0
DEADHEAD_MIN_DWELL_H = 0.05
```

分类规则：

- `< 0.5 km`：stop_id / coordinate 噪声，不注入。
- `>= 0.5 km and < 5.0 km`：注入 `deadhead_class="short"`。
- `>= 5.0 km`：注入 `deadhead_class="long"`。

### 5.5 `_inject_deadhead_trips` 设计

建议返回 augmented trips 和 stats，避免 silent skip。

```python
@dataclass
class DeadheadInjectionStats:
    short_count: int = 0
    long_count: int = 0
    total_km: float = 0.0
    total_kwh: float = 0.0
    skipped_time_count: int = 0
    skipped_time_km: float = 0.0
    skipped_missing_coord_count: int = 0


def _inject_deadhead_trips(
    trips: list[Trip],
    *,
    consumption_kwh_per_km: float,
) -> tuple[list[Trip], DeadheadInjectionStats]:
    ...
```

核心逻辑：

```python
augmented: list[Trip] = []
stats = DeadheadInjectionStats()

for idx, left in enumerate(trips):
    augmented.append(left)
    if idx == len(trips) - 1:
        break

    right = trips[idx + 1]
    if left.is_deadhead or right.is_deadhead:
        continue

    if not all(np.isfinite(x) for x in [left.end_lat, left.end_lon, right.start_lat, right.start_lon]):
        stats.skipped_missing_coord_count += 1
        continue

    deadhead_km = haversine_km(left.end_lat, left.end_lon, right.start_lat, right.start_lon)
    if deadhead_km < DEADHEAD_NOISE_KM:
        continue

    deadhead_h = deadhead_km / DEADHEAD_SPEED_KMH
    deadhead_depart_h = left.arrival_time
    deadhead_arrive_h = deadhead_depart_h + deadhead_h

    if deadhead_arrive_h + DEADHEAD_MIN_DWELL_H > right.departure_time:
        stats.skipped_time_count += 1
        stats.skipped_time_km += float(deadhead_km)
        continue

    deadhead_class = "short" if deadhead_km < DEADHEAD_SHORT_KM else "long"
    energy_kwh = float(deadhead_km * consumption_kwh_per_km)

    augmented.append(Trip(
        # Use actual Trip field names in data_structures.py.
        trip_id=f"DH_{left.trip_id}__{right.trip_id}",
        departure_time=deadhead_depart_h,
        arrival_time=deadhead_arrive_h,
        distance_km=float(deadhead_km),
        energy_consumed_kwh=energy_kwh,
        origin_purpose="deadhead",
        destination_purpose="deadhead",
        origin_lsoa=left.destination_lsoa,
        destination_lsoa=right.origin_lsoa,
        is_deadhead=True,
        deadhead_class=deadhead_class,
        # Also populate required stop/coord fields from left.end_* to right.start_*.
    ))

    if deadhead_class == "short":
        stats.short_count += 1
    else:
        stats.long_count += 1
    stats.total_km += float(deadhead_km)
    stats.total_kwh += energy_kwh

return augmented, stats
```

Implementation note：上面 constructor 只展示核心字段。实际代码必须根据 `Trip` dataclass 的真实必填字段补全：start/end stop id、lat/lon、route id、agency id、service day 等。不要发明与 dataclass 不一致的新字段名。

### 5.6 注入位置

优先在 `block_to_daily_schedules` 中对**完整 block 的 chronological trips** 先注入 deadhead，再 split/attach parking。不要只在 `for day in sorted(schedules)` 的 day 内部处理，否则跨午夜相邻 trip pair 会漏掉。

若现有结构已经先 split 成 `schedules[day]`，则需要重构为：

1. 构建 block-level `raw_trips`。
2. `_inject_deadhead_trips(raw_trips, ...)`。
3. 再按 day 分配。
4. 每个 day 内 `_attach_parking(augmented_day_trips)`。

### 5.7 Notebook/Gantt 视觉策略

- service trip：保留原有颜色/样式。
- `is_deadhead=True and deadhead_class="short"`：默认不画成显眼 trip 块；但能耗已进入 simulator。
- `is_deadhead=True and deadhead_class="long"`：Gantt 显式画灰色/neutral trip 块，便于识别极端空驶。
- 图例和 caption 必须说明：short deadhead 隐藏在视觉层，不是从能耗层删除。

### 5.8 测试

新增 `tests/mobility/bus/test_deadhead_injection.py`：

1. 0.3 km gap → 不注入。
2. 2 km gap，1h 时间窗 → 注入 short deadhead；`energy = 2 * consumption_kwh_per_km`。
3. 30 km gap，30 km/h，1h 时间窗 → `deadhead_h=1h`，加上最小 dwell 后时间不够；不注入，`skipped_time_count=1`。
4. 30 km gap，1.5h 时间窗 → 注入 long deadhead；后续 layover 自动缩短到约 0.5h 减 dwell 假设下的可用剩余时间。
5. 跨午夜 block → 注入不破坏 day0/day1 拆分，且 deadhead 发生在正确的 absolute hour。

---

## 6. P3 — LSOA / Data Zone 多边形归属

### 6.1 目标

将 `mobility/bus/data_loader.attach_lsoa` 从最近 centroid 升级为：

```text
polygon match first -> centroid fallback only for no-match/offshore/missing-boundary points
```

Modelling 端只读 GeoJSON，用 stdlib `json` + `numpy`。不引入 geospatial dependencies。

### 6.2 路径定义

在 `mobility/core/spatial.py` 中避免脆弱的 `parents[2].parent` 写法。推荐：

```python
from pathlib import Path

MODELLING_ROOT = Path(__file__).resolve().parents[2]   # .../Nature_EV_2025/Modelling
PROJECT_ROOT = MODELLING_ROOT.parent                   # .../Nature_EV_2025
DATA_LOADS = PROJECT_ROOT / "Data" / "Loads"

LSOA_BOUNDARY_PATHS = (
    (
        DATA_LOADS / "Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4_-4299016806856585929.geojson",
        "LSOA21CD",
        "EW_LSOA21",
    ),
    (
        DATA_LOADS / "SG_DataZone_Bdry_2022.geojson",
        "dzcode",
        "Scotland_DZ2022",
    ),
    (
        DATA_LOADS / "DZ2021.geojson",
        "DZ2021_cd",
        "NI_DZ2021",
    ),
)
```

### 6.3 Core spatial API

新增 `mobility/core/spatial.py`。建议实现以下 API：

```python
def load_lsoa_boundary_index(
    paths: tuple = LSOA_BOUNDARY_PATHS,
) -> dict:
    """Load UK LSOA/Data Zone GeoJSON boundaries with stdlib json only.

    Returns a dict-like index containing:
        codes: np.ndarray[str], shape (N,)
        sources: np.ndarray[str], shape (N,)
        bboxes: np.ndarray[float64], shape (N, 4), min_lon, min_lat, max_lon, max_lat
        areas: np.ndarray[float64], approximate lon/lat planar area for tie-break
        polygons: list[list[list[np.ndarray]]]
            Feature -> polygons -> rings -> np.ndarray[(K, 2)] in lon/lat order.
            For each polygon: rings[0] is exterior, rings[1:] are holes.

    Missing files:
        log warning and skip that namespace.
        if all files missing, return an empty but well-formed index.

    Validation:
        reject or skip a namespace if sampled coordinates do not look like EPSG:4326 lon/lat.
    """


def query_lsoa_polygons(
    lats: np.ndarray,
    lons: np.ndarray,
    index: dict,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Point-in-polygon lookup without centroid fallback.

    Returns:
        codes:   np.ndarray[str], "" for no polygon match
        sources: np.ndarray[str], one of boundary namespaces or "no_match"
        methods: np.ndarray[str], "polygon" or "no_match"
    """
```

Do **not** implement centroid fallback inside `core/spatial.py` unless centroid coordinates are explicitly passed in. Keep fallback in `bus/data_loader.attach_lsoa`.

### 6.4 Polygon containment semantics

Implement pure numpy / stdlib helpers. No shapely.

Required semantics：

- GeoJSON coordinate order is **lon, lat**。
- Polygon contains point if point is inside/touches exterior ring and not inside/touching any interior hole。
- MultiPolygon contains point if any polygon part contains point。
- Boundary point can match multiple neighbouring polygons; final tie-break picks smallest `areas` value, then lexical code for determinism。
- For holes, point on hole boundary is treated as outside that feature。
- Use small epsilon, e.g. `1e-12`, for segment boundary checks。

Recommended helper names：

```python
def _point_on_segment(x, y, x1, y1, x2, y2, *, eps=1e-12) -> bool: ...
def _point_in_ring(x, y, ring: np.ndarray) -> bool: ...
def _point_in_polygon_with_holes(x, y, rings: list[np.ndarray]) -> bool: ...
def _point_in_multipolygon(x, y, polygons: list[list[np.ndarray]]) -> bool: ...
```

### 6.5 性能策略

必须先 dedup stop coordinates，再查 polygon，再 join 回 trip rows。

`query_lsoa_polygons` 推荐采用 chunked bbox prefilter：

1. 对 `M` 个 unique points 按 chunk 处理，例如 `chunk_size=512` 或 `1024`。
2. 对每个 chunk vectorize bbox mask：
   `min_lon <= lon <= max_lon and min_lat <= lat <= max_lat`。
3. 只对 bbox 命中的 `(point, feature)` pair 做 ring ray-cast。
4. 如果单点多个 feature 命中，按 `(area, code)` deterministic tie-break。

目标：`attach_lsoa` 在 full fleet unique stops 上 < 90s。若 naive bbox loop 超时，实现一个纯 Python/numpy coarse grid index，不引入 R-tree dependency。

### 6.6 `bus/data_loader.attach_lsoa` 集成

`attach_lsoa` 流程：

1. 从 start/end lat/lon 中收集 valid unique coordinate pairs。
2. 调 `load_lsoa_boundary_index()`，再 `query_lsoa_polygons()`。
3. 对 `method == "no_match"` 的 unique coordinates，调用现有 nearest-centroid fallback 逻辑。
4. 将 unique coordinate mapping join 回原 DataFrame。
5. 保留旧输出字段，追加新字段：

```text
start_lsoa
start_lsoa_source
start_lsoa_match_method
end_lsoa
end_lsoa_source
end_lsoa_match_method
```

字段语义：

- `*_lsoa`：small-area code；E/W 为 LSOA21，Scotland 为 DZ2022，NI 为 DZ2021。
- `*_lsoa_source`：`EW_LSOA21` / `Scotland_DZ2022` / `NI_DZ2021` / `centroid_fallback` / `no_match`。
- `*_lsoa_match_method`：`polygon` / `centroid_fallback` / `no_match`。

### 6.7 `attrs["lsoa_join"]`

保留旧 attrs keys：`max_distance_km`、`n_unmatched`、`max_distance_km_threshold` 等。追加：

```python
{
    "method": "polygon_with_centroid_fallback",
    "polygon_pct": float,
    "centroid_fallback_pct": float,
    "no_match_pct": float,
    "max_centroid_fallback_km": float,
    "source_breakdown": {
        "EW_LSOA21": float,
        "Scotland_DZ2022": float,
        "NI_DZ2021": float,
        "centroid_fallback": float,
        "no_match": float,
    },
}
```

Percentage denominator：所有 valid start/end coordinate assignments，即最多 `2 * len(df)`，无效坐标不计入 denominator；如果当前旧逻辑把无效坐标计入 unmatched，则在 attrs 注释中明确。

### 6.8 测试

新增 `tests/mobility/core/test_point_in_polygon.py`：

1. 5×5 square：`(1,1)` inside，`(6,6)` outside，`(5,5)` boundary 按 boundary-inclusive 规则 inside。
2. Polygon with hole：洞内点 outside；外环内且洞外点 inside；hole boundary outside。
3. MultiPolygon：两个 disjoint part 中的点都 inside；part 外点 outside。

新增 `tests/mobility/bus/test_attach_lsoa_polygon.py`：

1. 用 `tmp_path` 写 3 个小 GeoJSON，每个 namespace 至少一个 polygon；monkeypatch `LSOA_BOUNDARY_PATHS` 或 loader 参数，不读真实 25/75 MB 文件。
2. 4 个 stop 经纬度归属正确，且 `*_lsoa_source` 覆盖 `EW_LSOA21`、`Scotland_DZ2022`、`NI_DZ2021`。
3. `attrs["lsoa_join"]["polygon_pct"] + centroid_fallback_pct + no_match_pct ≈ 100`。

---

## 7. P4 — `mobility/bus/feasibility.py` + fleet infeasibility audit

### 7.1 目标

不修改 `mobility/core/simulator._soc_walk`，但在 bus 层给出：

- 是否 infeasible。
- 第一次 SOC 触底时间。
- 触底对应 trip_id。
- unmet energy shortfall。
- 归因 reason。

### 7.2 新模块

新增 `mobility/bus/feasibility.py`。不要 import coach feasibility；可以参考风格但 bus 独立实现。

```python
INFEASIBILITY_REASONS = (
    "single_trip_exceeds_battery",
    "starts_below_min_required",
    "depot_only_insufficient",
    "midday_depletion",
)
```

推荐返回 shape：

```python
{
    "infeasible": bool,
    "first_floor_hit_step": int | None,
    "first_floor_hit_h": float | None,
    "first_floor_trip_id": str | None,
    "shortfall_kwh": float,
    "infeasibility_reason": str | None,
    "n_steps_at_floor": int,
}
```

### 7.3 必须做 shadow SOC walk

因为 core simulator 会 clamp SOC 到 0，不能仅从 clamped `soc` 得到 shortfall。bus feasibility 需要独立重放 schedule，但不 clamp：

```python
def shadow_soc_walk(
    schedules: list,
    *,
    battery_kwh: float,
    soc_init: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
) -> dict:
    """Replay trips/parking with no clamp.

    Returns arrays or lists with:
      time_h, soc_unclamped, event_id/trip_id, event_type.
    """
```

Charging/discharging assumptions must match `simulate_block` as closely as possible。若 simulator 使用了更复杂的 charge curve，先实现相同 linear rate 近似，并在 docstring 写明。

`shortfall_kwh`：

```python
shortfall_kwh = max(0.0, -float(np.nanmin(soc_unclamped)) * battery_kwh)
```

### 7.4 `block_preflight`

建议以 schedule events 为输入，而不是只看 `block_df`。只看 `block_df` 无法可靠知道 depot/layover charging potential。

```python
def block_preflight(
    schedules: list,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
    soc_init: float,
    reserve_soc_fraction: float = 0.0,
) -> dict:
    """Cheap checks before/alongside simulation; never raises."""
```

### 7.5 归因优先级

按以下顺序归因；第一个命中即 reason：

1. `single_trip_exceeds_battery`：任意 service/deadhead trip 的 `energy_consumed_kwh > battery_kwh * (1 - reserve_soc_fraction)`。
2. `starts_below_min_required`：第一段 trip 出发前可用 SOC 不足以完成该 trip（考虑 day start 到 first trip 之间可用 depot charging，如 schedule 有该 parking）。
3. `depot_only_insufficient`：`allow_layover_charging=False` 时，全天 trip 总能耗超过 `soc_init*battery + depot_charging_potential_kwh` 的物理上限。
4. `midday_depletion`：以上都不命中，但 shadow SOC 或 clamped SOC 显示中途触底，归因为时间错配/charging opportunity 不足。

可行 case：`infeasible=False`，`infeasibility_reason=None`，`shortfall_kwh=0.0`。

### 7.6 `scan_block_infeasibility`

```python
def scan_block_infeasibility(
    soc: np.ndarray,
    schedules: list,
    battery_kwh: float,
    *,
    soc_init: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
    reserve_soc_fraction: float = 0.0,
    soc_floor: float = 1e-9,
    time_grid_h: np.ndarray | None = None,
) -> dict:
    """Post-simulation scan; never raises."""
```

Use both signals：

- `soc <= soc_floor` from simulator to locate clamp/floor steps。
- `shadow_soc_walk` to estimate true shortfall and reason。

If `time_grid_h` is unavailable, infer first floor hit time from shadow walk event times。

### 7.7 `sim_adapter` 集成

`mobility/bus/sim_adapter.py::simulate_block` result 追加字段：

```python
{
    "infeasible": bool,
    "first_floor_hit_h": float | None,
    "first_floor_trip_id": str | None,
    "shortfall_kwh": float,
    "infeasibility_reason": str | None,
    "deadhead_short_count": int,
    "deadhead_long_count": int,
    "deadhead_total_km": float,
    "deadhead_total_kwh": float,
    "deadhead_skipped_time_count": int,
    "deadhead_skipped_time_km": float,
}
```

`simulate_fleet_blocks` 的 per-block DataFrame 同步包含这些列。若某个 block simulation 失败，不要 raise 终止全 fleet；返回该 block 的 infeasible/audit fields，并保留错误信息列（若项目已有错误列，沿用已有命名）。

### 7.8 测试

新增 `tests/mobility/bus/test_feasibility.py`：

1. `single_trip_exceeds_battery`：50 kWh battery + 200 km × 1.2 kWh/km trip → reason 命中。
2. `depot_only_insufficient`：300 km/day + 仅 4h depot @ 50 kW + `allow_layover_charging=False` → reason 命中。
3. `midday_depletion`：单 trip 不超 battery，总能耗也小于总可充电量，但关键 trip 前没有充电机会 → reason 命中。
4. `starts_below_min_required`：`soc_init=0.05` + first trip 50 kWh + 100 kWh battery → reason 命中。
5. 可行 case：标准 protagonist block + BYD EBUS class vehicle → `infeasible=False` / reason `None`。

---

## 8. P5 — `outputs/all_blocks.parquet` 重生 + bit-exact baseline 更新

### 8.1 前置条件

只有找到 canonical build command 后才能重生 parquet。推荐命令名如存在：

```bash
python -m mobility.bus.build_all_blocks
```

但不要假设它存在；以 pre-flight 发现结果为准。

### 8.2 流程

```bash
cp outputs/all_blocks.parquet outputs/all_blocks.parquet.legacy.bak
# apply P1/P2/P3 code changes
python -m mobility.bus.build_all_blocks
```

如果 build command 需要 raw GTFS / checkpoint 参数，写入 PR description 和 agent report。

### 8.3 bit-exact 测试

重写 `tests/mobility/bus/test_block_inference_bitexact.py`：

- 仍采用“重生 parquet 上重跑算法，集合等价”的模式。
- 新 `BlockInferenceConfig` 默认值必须在测试中显式传入，防止默认值漂移导致 silent baseline change。
- Native blocks 必须 bit-exact 保持。
- Inferred blocks 允许 namespace/contents 更新，但必须 deterministic。

### 8.4 新增 audit script

新增 `scripts/compare_legacy_blocks.py`：

输入：

- `outputs/all_blocks.parquet.legacy.bak`
- `outputs/all_blocks.parquet`

输出：

- `outputs/inference_comparison.csv`

报告至少包含：

| 指标 | 说明 |
|---|---|
| total rows old/new | 行数变化 |
| block count old/new | block 数变化 |
| native row/block unchanged check | native 是否保持 |
| inferred share old/new | inferred 占比变化 |
| inferred time-infeasible share old/new | 30 km/h + dwell 规则下不可行比例 |
| discontinuous adjacent pairs old/new | stop 不连续率 |
| deadhead injectable/skipped counts | P2 stats 汇总 |
| infeasible block share old/new if available | P4 后的可行性比例 |

---

## 9. P6 — Notebook 01 Stage I：fleet 可行性与 deadhead 审计

### 9.1 Stage A/A.5 文案更新

把原先“nearest LSOA centroid”描述更新为：

```text
LSOA/Data Zone assignment now uses polygon-with-centroid-fallback:
points inside EW LSOA21 / Scotland DZ2022 / NI DZ2021 polygons use polygon match;
offshore or unmatched points fall back to the legacy nearest-centroid method.
```

中文叙事可写：LSOA 归属已从最近质心升级为行政边界 polygon 优先，少量离岸/边界失败点回退到旧 centroid 方案。

### 9.2 Stage I 内容

在 Stage F sensitivity 之后、Stage H identity card 之前插入 Stage I。

推荐把 full fleet audit 放到 script：

```bash
python scripts/run_bus_feasibility_audit.py \
  --blocks outputs/all_blocks.parquet \
  --out outputs/bus_feasibility_audit.parquet
```

Notebook Stage I：

1. 如果 `outputs/bus_feasibility_audit.parquet` 存在：读取全量 audit。
2. 如果不存在：跑 deterministic stratified sample，并在 markdown cell 明确写 “sample audit, not full fleet”；不要在 notebook 写 parquet/csv。
3. 展示：
   - `infeasible_count / total_blocks`。
   - 4 类 `infeasibility_reason` 占比柱图。
   - `first_floor_hit_h` histogram，weekday vs weekend 分组。
   - `infeasibility_reason × agency_id` heatmap。
   - top 5 routes/operators by infeasible count/share。
   - `deadhead_total_km / total_km` 分布。
   - long deadhead frequency vs `block_source`。
   - 三种 EV specs 对比：默认抽样 / BYD EBUS 345 kWh / 小电池 180 kWh。

### 9.3 Notebook 约束

- 不写 parquet/csv。
- 不下载数据。
- 不 pip install。
- 图中 short deadhead 可以视觉隐藏，但所有能耗和 summary 必须包含 short + long deadhead。
- 若 full fleet audit 超出 notebook runtime，使用 precomputed audit script，而不是把 notebook 改成长期任务。

---

## 10. 验证命令

```bash
pytest tests/mobility/core/test_point_in_polygon.py -v
pytest tests/mobility/bus/test_block_inference_time_feasibility.py -v
pytest tests/mobility/bus/test_deadhead_injection.py -v
pytest tests/mobility/bus/test_attach_lsoa_polygon.py -v
pytest tests/mobility/bus/test_feasibility.py -v
pytest tests/mobility/bus/ tests/mobility/core/ -v
```

Parquet / notebook 验证：

```bash
cp outputs/all_blocks.parquet outputs/all_blocks.parquet.legacy.bak
python -m mobility.bus.build_all_blocks
python scripts/compare_legacy_blocks.py
python scripts/run_bus_feasibility_audit.py --blocks outputs/all_blocks.parquet --out outputs/bus_feasibility_audit.parquet
python notebooks/_build_01_bus_narrative.py
jupyter nbconvert --to notebook --execute --inplace notebooks/01_single_bus_simulation.ipynb --ExecutePreprocessor.timeout=180
```

如果 `python -m mobility.bus.build_all_blocks` 不存在，以 pre-flight 找到的 canonical command 替换；不要在 PR description 中写未实际执行的命令。

---

## 11. 验收指标

| 指标 | 期望 |
|---|---:|
| 新增测试数 | 至少 19 个 |
| `pytest tests/mobility/bus/ tests/mobility/core/` | 全绿，目标 < 90s |
| `attach_lsoa` full unique stop runtime | 目标 < 90s |
| `attrs["lsoa_join"]["polygon_pct"]` | > 95% |
| `attrs["lsoa_join"]["no_match_pct"]` | < 1% |
| inferred block 占比 | 旧 66.3%，新期望 50–55% |
| inferred 内时间不可行率 | 旧 15.7%，新目标 < 1% |
| deadhead 注入数 | 从 0 变为正数；short 为主，long 可审计 |
| `deadhead_skipped_time_km` | 应主要来自 native 长尾；必须报告，不得静默 |
| default fleet infeasible share | 研究输出，期望 5–25%，不要硬编码 |
| notebook restart-and-run-all | 目标 < 180s；若全量 audit 太慢，读取 script 输出 |

---

## 12. 不要做的事

- 不要改 `mobility/core/simulator._soc_walk`。
- 不要在 `Modelling` runtime/import path 引入 `geopandas / pyproj / pyshp / shapely / fiona`。
- 不要 import `mobility.coach.*` 或 `mobility.cars.*`。
- 不要让用户下载 LSOA/DZ boundary；数据已在 `../Data/Loads/`。
- 不要忘记 Scotland Shapefile 转 GeoJSON 前 `.to_crs("EPSG:4326")`。
- 不要用 centroid fallback 覆盖 polygon 命中结果；fallback 只处理 polygon no-match。
- 不要把 point-in-polygon 跑在每条 trip row 上；必须 dedup unique coordinates。
- 不要把 holes 当普通 outer rings flatten。
- 不要静默跳过时间不够的 deadhead pair；必须统计 skipped。
- 不要把 SOC 触底作为 fatal raise；fleet audit 必须继续跑。
- 不要在 notebook 写 parquet/csv。
- 不要在 `infer_blocks` 改逻辑后保留旧 inferred block_id 兼容层；重生 parquet。
- 不要改 `DEADHEAD_SPEED_KMH=30.0`、`DEADHEAD_NOISE_KM=0.5`、`DEADHEAD_SHORT_KM=5.0`。
- 不要新增 road detour factor；本 PR 保持 haversine × 1.0，后续 PR 再做 OSRM/Valhalla/road factor。

---

## 13. PR description 模板

```markdown
## Summary
Bus duty cycle 完整重做：
- Rewrote `infer_blocks` with 30 km/h deadhead time-feasibility hard filter,
  minimum dwell, max inferred deadhead distance, max shift guard, and deterministic
  deadhead-aware scoring.
- Injected distance-tiered deadhead `Trip` segments before parking attachment:
  noise <0.5 km ignored, short 0.5–5 km, long >=5 km, plus skipped-time audit.
- Replaced nearest-centroid-first LSOA assignment with polygon-first matching over
  EW LSOA21 + Scotland DZ2022 + NI DZ2021 GeoJSON, with centroid fallback only for
  no-match/offshore points.
- Added `mobility.bus.feasibility` with shadow SOC walk, 4 infeasibility reasons,
  shortfall_kwh, first floor hit audit, and fleet-level result columns.
- Added notebook Stage I for fleet feasibility/deadhead audit and updated Gantt
  display for long deadhead segments.

## Verification
- `pytest tests/mobility/bus/ tests/mobility/core/ -v` -> paste output
- `python -m mobility.bus.build_all_blocks` or actual canonical build command -> regenerated `outputs/all_blocks.parquet`
- `python scripts/compare_legacy_blocks.py` -> wrote `outputs/inference_comparison.csv`; paste key deltas
- `python scripts/run_bus_feasibility_audit.py --blocks outputs/all_blocks.parquet --out outputs/bus_feasibility_audit.parquet` -> paste row count and infeasible share
- `jupyter nbconvert --to notebook --execute --inplace notebooks/01_single_bus_simulation.ipynb --ExecutePreprocessor.timeout=180` -> completed

## Data dependencies
- Existing: `Data/Loads/Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4_*.geojson`
- Existing: `Data/Loads/DZ2021.geojson`
- New derived local data: `Data/Loads/SG_DataZone_Bdry_2022.geojson`
  created once in `Data/Loads/loads.ipynb` via
  `gpd.read_file(...).to_crs("EPSG:4326").to_file(..., driver="GeoJSON")`.
- If Scotland GeoJSON is missing, polygon matching skips Scotland and those points
  fall back to centroid/no_match; no geospatial dependency is added to Modelling.

## Dependency changes
None.

## Deviations from AGENTS.md
None.

## Schema changes
- `Trip`: `+is_deadhead`, `+deadhead_class`.
- `simulate_block` / `simulate_fleet_blocks` per-block output:
  `+infeasible`, `+first_floor_hit_h`, `+first_floor_trip_id`, `+shortfall_kwh`,
  `+infeasibility_reason`, `+deadhead_short_count`, `+deadhead_long_count`,
  `+deadhead_total_km`, `+deadhead_total_kwh`, `+deadhead_skipped_time_count`,
  `+deadhead_skipped_time_km`.
- `attach_lsoa` output:
  `+start_lsoa_source`, `+start_lsoa_match_method`, `+end_lsoa_source`,
  `+end_lsoa_match_method`.
- `attrs["lsoa_join"]`:
  `+method`, `+polygon_pct`, `+centroid_fallback_pct`, `+no_match_pct`,
  `+max_centroid_fallback_km`, `+source_breakdown`; existing attrs preserved.
- `outputs/all_blocks.parquet`: regenerated; inferred block namespace/content changed.

## `_soc_walk` changes
None. Core simulator remains frozen; feasibility is post-simulation/shadow-walk audit only.
```

---

## 14. 后续 PR，不在本次 scope

- 使用真实路网距离或 `road_detour_factor` 替代 haversine × 1.0。
- 集成真实 charger database，例如 `outputs/UK_OCM_stations_labeled.csv`。
- 若 dedup + bbox chunking 仍慢，再实现纯 numpy coarse grid / R-tree-like index。
- Battery degradation / SOH across year。
- TxC `OperatingProfile` / GTFS calendar 更完整展开。
- 将 polygon spatial module 复用于 cars / coach。
- 将 Scotland shapefile conversion 从 `loads.ipynb` 抽成 `Data/Loads/convert_sg_shp.py`，但仍不作为 Modelling runtime dependency。
```
