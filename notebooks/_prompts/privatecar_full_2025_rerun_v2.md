# 任务：重跑 private-car 充电模拟，验证 Scotland fix，并产出 trip-level + event-level 全量结果

## 背景

由于旧版 private-car full run 存在 Scotland geography bug，现在需要在 git pull 后的修复版代码上重新跑 2025 全量 16 shard，并产出可验证的 station-level、trip-level、event-level 全量结果。

这次只允许运行、验证、报告；不要修改 modelling 代码。

## 环境（已验证）

- 服务器：251 GB RAM，`ulimit -v unlimited`，无 cgroup 内存限制。
- Python：必须使用 `/opt/conda/bin/python`。
  - 该环境自带 `pandas 3.0.2`、`pyarrow 23.0.1`、`numpy`。
  - 系统 `/usr/bin/python3` 没有 pandas，绝不能用。
- 工作目录：
  - `/home/mazhichuan/Projects/Modelling`
- 旧输出，仅作对照，禁止覆盖或删除：
  - `/home/mazhichuan/Work/Modelling/outputs/privatecar_full_2025_shards_16/`
- 新输出根目录：
  - `/home/mazhichuan/Work/Modelling/outputs/privatecar_full_2025_shards_16_v2/`
- 管理员指定的个人大文件目录：
  - 逻辑路径：`~/Work`
  - 本用户实际路径：`/home/mazhichuan/Work`
  - 物理位置：`/mnt/data/work/mazhichuan`
  - 用途：数据集、项目输出、大文件、日志和临时文件都应放在这里。
- SSH 长连接可能会被网络断开，所以任何重活必须用 `nohup` 加日志文件，绝不能挂在 interactive foreground 上。
- 这是 CPU 任务，不要使用 GPU。
- 不要 `sudo`。

## 必须先确认的修复内容

开始主跑前，必须确认以下 import 成功：

```bash
/opt/conda/bin/python - <<'PY'
from mobility.cars.scotland_geography import unify_scotland_ev_home_lsoa_to_dz2022
from mobility.cars import geography_preflight
print("imports ok")
PY
```

需要确认脚本支持以下 CLI flag：

- `--vehicle-shard-index`
- `--vehicle-shard-count`
- `--max-vehicles`
- `--sample-n-per-country`
- `--skip-web-json`
- `--resume`

用脚本真实 help 为准：

```bash
/opt/conda/bin/python scripts/run_privatecar_charging_curves.py --help
/opt/conda/bin/python scripts/merge_privatecar_station_curve_shards.py --help
```

## 预期 shard 输出

每个 shard 目录应包含至少：

- `station_charging_curve_15min_2025.parquet`
- `station_counts_2025.parquet`
- `station_day_counts_2025.parquet`
- `private_car_trip_records.parquet`
- `private_car_charging_events.parquet`
- `private_car_home_charging_events.parquet`
- `private_car_failed_charging_events.parquet`
- `profiling_log_2025.csv`
- `data_quality_report.md`

合并后根目录应包含聚合后的同名 parquet/csv/json/markdown，以及：

- `merge_manifest_2025.json`
- `profiling_log_merged_2025.csv`

## 严格约束

### 路径约束

必须使用以下路径变量：

```bash
REPO=/home/mazhichuan/Projects/Modelling
WORK=/home/mazhichuan/Work
# WORK is the admin-designated large-file area.
# Physical backing path: /mnt/data/work/mazhichuan
ROOT="$WORK/Modelling/outputs/privatecar_full_2025_shards_16_v2"
LOG_DIR="$ROOT/logs"
TMPDIR="$ROOT/tmp"
SMOKE_DIR="$ROOT/_smoke_scotland_stratified"
```

必须执行：

```bash
mkdir -p "$ROOT" "$LOG_DIR" "$TMPDIR"
export TMPDIR="$TMPDIR"
```

不要让日志、临时文件或默认 outputs 落到：

- `/home/mazhichuan/Projects/Modelling/outputs`
- `/tmp`
- 旧输出目录 `/home/mazhichuan/Work/Modelling/outputs/privatecar_full_2025_shards_16/`
- `/mnt/data/work/` 下其他用户目录

### shard 输出目录约束

每个 shard 必须写入独立目录：

- `$ROOT/shard_0`
- `$ROOT/shard_1`
- ...
- `$ROOT/shard_15`

禁止让 16 个 shard 共同写同一个 `--output-dir "$ROOT"`，否则会互相覆盖最终 parquet、抢占 `chunks/` checkpoint，并污染结果。

### 内存约束

内存预算：峰值不超过 200 GB，保留至少约 50 GB headroom。

必须每分钟采样一次 `free -m`，写入：

- `$ROOT/monitor.log`

内存控制规则：

- `MemAvailable >= 100 GB`：可以维持当前并行度；若单 shard RSS 很低且运行稳定，可谨慎升并行度。
- `MemAvailable < 100 GB`：不要启动新 shard。
- `MemAvailable < 80 GB`：暂停启动新 shard，并考虑降低目标并行度。
- `MemAvailable < 60 GB`：主动降低并行度。
- `MemAvailable < 50 GB`：立即 kill 最年轻的 shard 进程，并记录到 `$ROOT/monitor.log` 和进度报告。
- `MemAvailable < 30 GB`：视为 emergency；必须立即停止新增任务并 kill 最年轻 shard，直到恢复到安全区间。

不要把 `<30 GB` 当作正常 kill 阈值；那已经超过了 200 GB 峰值预算。

### 并行度约束

建议不要直接开 6-8 并行。默认策略：

- 起步并行度：2。
- 观察至少 30 分钟或至少一个 shard 的多个 chunk 后，再决定是否升到 3 或 4。
- 只有当 `MemAvailable` 长期稳定在 100 GB 以上，且单 shard RSS 明显可控时，才允许升到 4。
- 默认不要超过 4 并行，除非监控数据明确证明安全。

### 2026-05-19 失败 shard 重跑补充要求

当前 v2 首轮主跑已经完成 smoke，且 `shard_2`、`shard_3` 产出了完整 shard 结果；不要重跑这两个成功 shard。

首轮失败诊断：实际失败批次不是稳定的 4 并行。`controller.log` 显示 controller 曾被重启为 `target=12 max=12`，一次性运行约 12 个 shard，随后触发低内存 kill；因此本次失败 shard rerun 需要显式锁定为 4 并行。

必须只重跑失败 shards：

- `shard_0`
- `shard_1`
- `shard_4`
- `shard_5`
- `shard_6`
- `shard_7`
- `shard_8`
- `shard_9`
- `shard_10`
- `shard_11`
- `shard_12`
- `shard_13`
- `shard_14`
- `shard_15`

失败 shard 重跑必须按 4 shard 并行启动和维持：

- `PARALLEL_TARGET=4`
- 不要降到 2 或 3 作为默认策略。
- 仍然必须使用每个 shard 的原独立目录 `$ROOT/shard_${i}`。
- 仍然必须加 `--resume`，复用已有 chunk checkpoints。
- 日志必须使用新文件名，避免覆盖首轮日志，例如 `$LOG_DIR/shard_${i}.rerun4.log`。
- 重跑完成后，再执行 Step 3 merge 和 Step 4 validation。

### nohup 约束

所有重活，包括 smoke、主跑 shard、merge、验证，都必须用 `nohup ... > logfile 2>&1 < /dev/null &` 启动。

不要 foreground 跑长任务。

可以创建一个 controller 脚本来管理并行、重试、内存监控和 summary，但该脚本必须放在 `$ROOT` 或 `/tmp`，不要修改 repo 代码。

## Step 0 - 准备

```bash
cd /home/mazhichuan/Projects/Modelling
git status --short
git log -1 --oneline
```

如果明确需要拉最新代码，且工作树没有未提交改动，再执行：

```bash
git pull --ff-only
git log -1 --oneline
```

如果工作树不干净，不要覆盖或回滚用户改动；暂停并报告。

然后：

```bash
REPO=/home/mazhichuan/Projects/Modelling
WORK=/home/mazhichuan/Work
ROOT="$WORK/Modelling/outputs/privatecar_full_2025_shards_16_v2"
LOG_DIR="$ROOT/logs"
TMPDIR="$ROOT/tmp"
SMOKE_DIR="$ROOT/_smoke_scotland_stratified"

mkdir -p "$ROOT" "$LOG_DIR" "$TMPDIR"
export TMPDIR="$TMPDIR"
```

确认 Work 输出盘空间，而不是项目盘空间：

```bash
df -h "$ROOT" "$REPO" "$TMPDIR"
free -h
```

Step 0 完成后，给一行报告：当前 commit、输出目录、可用内存、输出盘可用空间。

## Step 1 - smoke test（必跑，不许跳过）

smoke test 必须验证 Scotland，不要只用 `--max-vehicles 2000`，因为该参数是 deterministic `head(n)`，可能排除 Scotland，导致假阴性。

使用 country-prefix stratified sample：

```bash
cd "$REPO"
nohup /opt/conda/bin/python scripts/run_privatecar_charging_curves.py \
  --vehicle-shard-count 16 \
  --vehicle-shard-index 0 \
  --sample-n-per-country 1000 \
  --output-dir "$SMOKE_DIR" \
  --skip-web-json \
  --resume \
  > "$LOG_DIR/smoke_scotland_stratified.log" 2>&1 < /dev/null &
```

等待 smoke 完成。若失败，立即报告日志尾部错误，不进入 Step 2。

smoke 完成后，从以下文件读 station activity：

- `$SMOKE_DIR/station_charging_curve_15min_2025.parquet`

加载 connector metadata：

- `/home/mazhichuan/Projects/Web/public/data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv`

字段约定：

- curve 里使用 `station_id`
- connector CSV 里使用 `StationID` 和 `region`
- join 时统一转成 string
- Scotland region 为 `SC`

Scotland 激活率定义：

```text
active_scotland_stations / total_scotland_stations
```

其中：

- `total_scotland_stations` = connector CSV 中 `region == "SC"` 的 unique `StationID`
- `active_scotland_stations` = smoke curve 中出现过且属于 Scotland 的 unique `station_id`

验收标准：

- smoke Scotland 激活率必须 >= 50%。
- 如果仍然 < 5%，立刻停下报告，不许进入 Step 2。
- 如果在 5%-50% 之间，报告并暂停，请用户决定是否继续；不要擅自进入全量主跑。

Step 1 完成后，报告 Scotland 激活率、active/total 数字、是否进入 Step 2。

## Step 2 - 主跑 16 shard

主跑目标：`shard_0` 到 `shard_15` 全部完成。

每个 shard 的基础命令必须是：

```bash
cd "$REPO"
nohup /opt/conda/bin/python scripts/run_privatecar_charging_curves.py \
  --vehicle-shard-count 16 \
  --vehicle-shard-index "$i" \
  --output-dir "$ROOT/shard_${i}" \
  --skip-web-json \
  --resume \
  > "$LOG_DIR/shard_${i}.log" 2>&1 < /dev/null &
```

注意：

- 每个 shard 的 `--output-dir` 必须是 `$ROOT/shard_${i}`。
- 日志必须是 `$LOG_DIR/shard_${i}.log`。
- 必须使用 `/opt/conda/bin/python`。
- 必须加 `--skip-web-json`，避免每个 shard 都生成大量 Web JSON。
- 推荐加 `--resume`，允许利用 chunk checkpoints。
- 不要用 `--no-checkpoint`。

### 重试规则

每个 shard 失败后自动重试一次，使用同样参数和同一个 output dir，并保留原日志：

- 首次日志：`$LOG_DIR/shard_${i}.log`
- 重试日志：`$LOG_DIR/shard_${i}.retry1.log`

第二次仍失败则跳过该 shard，并在最终 summary 里标为 `FAILED`，附日志尾部错误。

### 进度报告规则

Step 2 期间，每 10 分钟或每次 shard 完成，取较短者，报告一次：

- 已完成数 / 16
- 进行中数
- 已失败数
- 当前 `MemAvailable`
- 当前并行度
- 已用时长
- 粗略剩余预估时长
- 最近完成的 shard 及其车辆数、trip rows、charging event rows（可从日志或输出 parquet metadata/manifest 获取）

任何 shard 失败，立即报告：

- shard id
- exit code
- 日志尾部错误
- 是否准备 retry

## Step 3 - 合并

只有在至少确认所有可用 shard 都结束后再 merge。理想状态是 16 个 shard 全部成功。

merge 命令必须显式传入 shard root 和 output dir：

```bash
cd "$REPO"
nohup /opt/conda/bin/python scripts/merge_privatecar_station_curve_shards.py \
  --shard-root "$ROOT" \
  --shard-glob "shard_*" \
  --output-dir "$ROOT" \
  --year 2025 \
  > "$LOG_DIR/merge.log" 2>&1 < /dev/null &
```

不要依赖 merge 脚本默认路径；默认路径不是本次 v2 输出目录。

merge 完成后确认：

- `$ROOT/station_charging_curve_15min_2025.parquet`
- `$ROOT/private_car_trip_records.parquet`
- `$ROOT/private_car_charging_events.parquet`
- `$ROOT/private_car_home_charging_events.parquet`
- `$ROOT/private_car_failed_charging_events.parquet`
- `$ROOT/merge_manifest_2025.json`

## Step 4 - 最终验证

必须跑验证脚本并贴结果。验证内容：

1. 16 个 shard 是否都成功。
2. 每个成功 shard 是否存在必需 parquet。
3. `merge_manifest_2025.json` 的 `source_shard_count` 是否为 16。
4. 合并后 Scotland 站点激活率是否 >= 90%。
5. `private_car_trip_records.parquet` 行数是否为数千万级。
6. `private_car_charging_events.parquet` 行数是否为数百万级。
7. 合并后的 station curve 是否有正的 `energy_kwh`。
8. 合并后的 charging events 中，`home`、`public_current_lsoa`、`failed_public_charging` 三类数量。

Scotland 激活率口径：

```text
active_scotland_stations / total_scotland_stations
```

其中：

- `total_scotland_stations` = connector CSV 中 `region == "SC"` 的 unique `StationID`
- `active_scotland_stations` = merged station curve 中全年任意一天任意 15-min bin `energy_kwh > 0` 的 Scotland unique `station_id`

验证读取大 parquet 时要节制内存：

- 优先用 pyarrow metadata 获取行数。
- 对 station curve 可只读必要列：`station_id`, `energy_kwh`。
- 不要回头去读 `destination_choice_table.parquet` 内容；该文件巨大。若必须检查，只看 metadata。

## 最终报告格式

全部完成后，最终报告必须包含：

```text
commit: <git hash + subject>
output_root: /home/mazhichuan/Work/Modelling/outputs/privatecar_full_2025_shards_16_v2
shards_success: <n>/16
shards_failed: <list or none>
merge_status: SUCCESS/FAILED
scotland_activation_rate: <active>/<total> = <percent>
trip_record_rows: <n>
charging_event_rows: <n>
home_charging_event_rows: <n>
public_charging_event_rows: <n>
failed_public_charging_event_rows: <n>
station_curve_rows: <n>
public_station_energy_kwh: <n>
peak_mem_available_mb_observed: <min MemAvailable from monitor.log>
logs: $LOG_DIR
```

如果任一验收项失败，必须明确标为 `FAILED`，并说明下一步建议。

## 不要做的事

- 不要修改 modelling 代码。
- 不要删除旧目录 `/home/mazhichuan/Work/Modelling/outputs/privatecar_full_2025_shards_16/`。
- 不要让 16 个 shard 写同一个 output dir。
- 不要 foreground 跑长任务。
- 不要用 `/usr/bin/python3`。
- 不要 `sudo`。
- 不要在 GPU 上跑。
- 不要回头读取 `destination_choice_table.parquet` 的完整内容；它很巨大，用 metadata 即可。
- 不要默认把并行度升到 6-8；除非内存监控明确证明安全。
