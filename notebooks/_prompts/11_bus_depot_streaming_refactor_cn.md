# 任务：把 annual depot-only 公交充电管线改成按 service_date 流式写出

> 给服务器端编码 agent 的执行说明。目标：在不改各阶段算法的前提下，把全年管线从
> "全量物化在内存" 改成 "按 `service_date` 分块流式写盘"，消除 OOM，并保证结果与
> 现有批处理逐行一致。

## 背景
仓库 `main` 分支已有一套年度 depot-only 公交充电管线，入口是
`scripts/run_bus_annual_depot_load.py` 的 `run_pipeline()`。它当前把全年所有
vehicle-day 的事件账本、SOC、15min 负荷、以及三张 observability 表
（trip_records / charging_events / ev_state_records）**一次性全部物化在内存里**
再写盘。全年规模约 194 万 vehicle-day、~3500 万事件行；其中
`mobility/bus/annual_depot_artifacts.py::build_bus_ev_state_records` 会对每个事件
按 15 分钟切片，产出 **~2.6 亿行**（每个 ~12h 的 overnight 停车单事件就 ~48 行），
它把 ~2.6 亿个 dict 堆进一个 Python list 再建 DataFrame → 峰值内存数百 GB，全年必 OOM。

## 目标
重构 `run_pipeline()` 的编排（**只改编排，不改各阶段算法/输出列**），改成按
`service_date` 分块流式处理并增量写盘，使全年运行内存峰值被压到"单日量级"。

## 关键正确性依据（必须保持）
- `mobility/bus/annual_vehicle_day_assignment.py::build_vehicle_day_assignments`
  已经是**按 service_date 独立分组**，用 `stable_daily_seed(seed, service_date)`
  做当日随机抽样，且 `vehicle_day_id` 含日期。→ 因此"逐日单独处理"与"整年一次性处理"
  的赋值结果**完全相同**。这是流式可行的根本原因，务必在实现中保持：逐日切片
  assignments，不要做任何跨日的全局重排/重抽样。
- `build_vehicle_day_events` / `apply_depot_only_soc` / `aggregate_depot_load_15min`
  / 三个 artifact builder 都是**逐 vehicle-day、逐 service_date 独立**的，可安全分日。
- 全年 `--max-vehicle-days` 用不到（默认 0=不封顶），实现里分日路径忽略该 cap 即可，
  但要保留 smoke 用的 `--max-days`。

## 流式编排设计
1. **只构建一次的全局表**（沿用现有函数，照旧写盘）：
   block_templates → block_templates_lsoa → block_instances_annual →
   depot_registry → ev_bus_specs → vehicle_day_assignments。
   这些相对小，可留在内存或读回。
2. 从 assignments 取**排序后的唯一 service_date 列表**。
3. **逐日循环**（或每 N 天一批，加 `--date-chunk-size`，默认 1）：
   a. 切片该日的 assignments 和 block_instances；
   b. `build_vehicle_day_events` → `apply_depot_only_soc`；
   c. `aggregate_depot_load_15min`（其内部已按 service_date 分块）→ 结果很小
      （depots×96 slot×当日），累加到一个列表，或写当日 shard；
   d. 三张 artifact 表逐日构建，**直接写成按 service_date 分区的 parquet 数据集**
      （Hive 风格：`bus_ev_state_records/service_date=YYYY-MM-DD/part.parquet`，
      trip / charging 同理），用 pyarrow `ParquetWriter` 或每日单文件；
   e. 每日跑一次 `depot_load_energy_matches_events` 自检，并累加
      Σcharge_kwh、Σenergy 等用于最终 run_summary 的统计量；
   f. `del` 当日的 events/soc/artifacts，显式释放内存（必要时 gc.collect()）。
4. **收尾合并**：把每日的 depot_load_15min / depot_daily_summary shard 合并成
   单文件（它们足够小）；vehicle_day_events / vehicle_day_soc_summary 也改成
   分区数据集（不要再单文件全量物化，events 全年 ~35M 行）。
5. 用累加的统计量生成 `run_summary.md`（保持现有 `build_run_summary_markdown` 的
   字段；若它依赖完整 DataFrame，改成接收预聚合统计或对分区数据集二次轻量聚合）。

## 输出与兼容
- **所有表的列名、dtype、语义、文件名保持不变**；大表从"单 parquet"变为"按
  service_date 分区的 parquet 目录"是允许的唯一结构变化（下游用
  `pd.read_parquet(dir)` 或 pyarrow dataset 仍可整体读）。
- 新增 CLI：`--stream`（默认 True）、`--date-chunk-size N`（默认 1）。
  保留原有一次性路径（`--stream false`）以便对拍。

## 输出位置（强制）
- 所有产物**必须写到个人工作目录**，不要写进仓库的 outputs/ 或 home 根目录：
    根目录 = ~/Work   （物理路径 /mnt/data/work/$USER，18T 盘，仅本人可访问）
    本任务输出目录 = ~/Work/Nature_EV_2025/outputs/bus_annual_depot_load/
- 运行时通过 --out-dir 显式指定，例如：
    OUT="$HOME/Work/Nature_EV_2025/outputs/bus_annual_depot_load"
    mkdir -p "$OUT"
    python scripts/run_bus_annual_depot_load.py --stream true --out-dir "$OUT"
- 注意：argparse 不会自动展开 ~。若在代码里处理路径，用
  Path(os.path.expanduser(str(args.out_dir))).resolve()，确保 ~ / $HOME 被展开成
  /mnt/data/work/$USER/...；并在写盘前 mkdir -p。
- 全年大表（尤其 bus_ev_state_records ~8GB、按 service_date 分区的数据集）一律落在
  这个 ~/Work 目录下，禁止写到仓库工作区（避免污染 git 和撑爆系统盘）。
- 仓库代码仍从 main 拉取；代码与数据分离：code 在 repo，data/outputs 在 ~/Work。
- 冒烟对拍的两份输出也放 ~/Work 下（如 .../outputs/_stream 与 .../outputs/_batch_ref）。
- 顺手确认仓库 .gitignore 已忽略 outputs/，防止误提交大文件。

## 验收标准（必须全部满足）
1. **逐行一致性对拍**：`--max-days 3 --max-vehicle-days 800` 下，`--stream true` 与
   `--stream false` 两条路径产出的 depot_load_15min / depot_daily_summary /
   bus_trip_records / bus_charging_events / bus_ev_state_records 在排序后
   **逐行相等**（数值用 atol=1e-9）。
2. 能量自检：每日 `depot_load_energy_matches_events` 为 True，且全年
   Σdepot_load.charge_kwh 与 Σevents.charge_kwh_added（depot 充电类型）相对误差 ≤1e-9。
3. 现有 `tests/mobility/bus/` 全部仍通过；为流式编排补 1~2 个单测
   （小型多日 fixture，断言 stream 与 batch 等价）。
4. 全年运行内存峰值与"单日处理量"同量级（不随天数线性增长）；ev_state 不再
   一次性进内存。
5. 不改动 `mobility/bus/annual_*.py` 里各阶段的核心算法函数签名/输出（只可新增
   可选的"逐日切片入参"或在 runner 层切片）。

## 涉及文件
- 主改：`scripts/run_bus_annual_depot_load.py`（编排层）
- 可能轻改：`mobility/bus/annual_depot_outputs.py`（run_summary 接预聚合统计）
- 新增：流式等价性单测
- 请勿改动 events/soc/aggregate/artifacts 的计算逻辑。

## 完成后运行
```bash
OUT="$HOME/Work/Nature_EV_2025/outputs/bus_annual_depot_load"
mkdir -p "$OUT"

# 对拍（小样本）
python scripts/run_bus_annual_depot_load.py --max-days 3 --max-vehicle-days 800 \
    --stream true  --out-dir "$HOME/Work/Nature_EV_2025/outputs/_stream"
python scripts/run_bus_annual_depot_load.py --max-days 3 --max-vehicle-days 800 \
    --stream false --out-dir "$HOME/Work/Nature_EV_2025/outputs/_batch_ref"
# 贴出两者逐行对拍结果 + 内存峰值

# 通过后开全年
python scripts/run_bus_annual_depot_load.py --stream true --out-dir "$OUT"
```

## 最终产物（验收时确认这些存在且口径正确）
- 核心：`depot_load_15min.parquet`（depot×15min×日 充电曲线）、
  `depot_daily_summary.parquet`（depot×日 汇总）
- 明细：`bus_trip_records` / `bus_charging_events` / `bus_ev_state_records`
  （后者按 service_date 分区）
- 中间/registry：block_templates(_lsoa)、block_instances_annual、depot_registry、
  ev_bus_specs、vehicle_day_assignments、vehicle_day_events、vehicle_day_soc_summary
- 诊断：run_summary.md、preflight_summary.{json,md}、各 *_diagnostics.parquet

## 模型口径提醒（不要在重构中改变）
1. 只建模 depot 充电（无公共/机会充电）。
2. 不结转跨日 SOC，每个 vehicle-day 从 usable_soc_max 满电起步——这是"当前 EV 存量
   规模下的代表性年度负荷"，不是全英公交全电动化。
