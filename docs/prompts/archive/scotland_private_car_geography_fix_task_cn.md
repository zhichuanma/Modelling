# Scotland 私家车公共充电失败问题修复任务

## 任务标题

修复 Scotland 私家车公共充电失败问题：统一 small-area geography 编码体系，并增加 preflight 检查。

---

# 1. 背景

当前问题不是 Web 前端画图问题。

Web 前端只是把 modelling pipeline 生成的 station curves 可视化出来。现在北部苏格兰 active public charging stations 几乎消失，本质原因是后端私家车 modelling pipeline 没有正确把 Scottish EV 的停车位置匹配到 Scottish charging stations。

因此，请不要优先修改：

```text
Web plot
Active today filter
station sparkline
前端地图显示逻辑
```

应优先修复 modelling / backend pipeline 中的 geography consistency 问题。

---

# 2. 已确认的根因

当前 Scotland 私家车公共充电失败的根因是：

```text
Scotland EV home_lsoa 使用 Scotland Data Zone 2011；
Scotland charging station / destination / centroid 使用 Scotland Data Zone 2022；
station matching 使用 exact lsoa_code lookup；
因此 Scotland EV 无法匹配到 Scotland public charging stations。
```

具体表现为：

```text
EV Scotland home_lsoa codes:
S01006506 到 S01013481
```

这对应 Scotland Data Zone 2011。

而 charging station / destination / centroid 表中 Scotland station codes 类似：

```text
S01013495 到 S01020873
```

这对应 Scotland Data Zone 2022。

二者 exact overlap 为：

```text
0
```

也就是说，虽然两边都是 `S01...` 开头，但它们不是同一套 geography code system。

---

# 3. 当前代码机制

当前 station matching 是 exact lookup 逻辑。

大致机制如下：

```text
station_matcher.py:
    按 station lsoa_code 建立 by_lsoa index

match_stations_for_schedule:
    用 parking event 的 pe_lsoa 做 exact lookup

如果查不到：
    can_charge = False
```

因此，对于 Scotland EV：

```text
parking event lsoa = S01006506   # Data Zone 2011
station lsoa_code    = S01013495   # Data Zone 2022
```

exact match 永远失败。

结果就是：

```text
Scottish EV 进入仿真
    ↓
destination lookup 找不到匹配 rows
    ↓
fallback 回 home_lsoa，即 DZ2011 code
    ↓
station matcher 用 DZ2011 code 查 station table
    ↓
station table 是 DZ2022 code
    ↓
exact lookup 失败
    ↓
can_charge = False
    ↓
Scotland public charging curve 接近 0
```

---

# 4. 本任务目标

本任务目标是修复或至少显式阻止这种 geography mismatch 导致的无效输出。

优先级如下：

```text
1. 系统梳理所有使用 small-area code 的输入 artifact；
2. 增加 geography consistency preflight check；
3. 避免使用 head(n) 作为全国 sample；
4. 统一 Scotland small-area geography；
5. 修复后验证 Scotland station matching；
6. 如果无法安全修复，则 fail fast 并输出 blocker report。
```

---

# 5. 需要系统梳理的输入文件

请列出 private-car pipeline 中所有使用 LSOA / Data Zone / small-area code 的输入 artifact，包括但不限于：

```text
EV allocation
person_fleet
schedule_vehicle_profile
destination choice table
station metadata
LSOA / DataZone centroid table
neighbor table
POI attractiveness table
station matching index
中间 schedule / parking event 文件
任何包含 home_lsoa / origin_lsoa / destination_lsoa / lsoa_code 的文件
```

重点文件包括：

```text
data/EV_UK_LSOA_2025_with_energy.csv
data/UK_OCM_stations_labeled.csv
destination_choice_table.parquet
lsoa_scene_attractiveness.parquet
person_fleet.parquet
schedule_vehicle_profile_2025.csv
centroid lookup files
neighbor lookup files
```

对每个 artifact，请输出以下信息：

```text
file_path
small_area_code_column
country_or_prefix
Scotland geography version:
    - Data Zone 2011
    - Data Zone 2022
    - unknown
code prefix / range
unique code count
example codes
```

---

# 6. 增加 geography consistency preflight check

请在 private-car station-curve pipeline 正式运行前增加 preflight 检查。

该检查应按 prefix / country 输出统计：

```text
E: England
W: Wales
S: Scotland
N: Northern Ireland
```

至少检查以下 overlap：

```text
EV home_lsoa vs station lsoa_code
EV home_lsoa vs destination origin_lsoa
EV home_lsoa vs destination destination_lsoa
EV home_lsoa vs centroid codes
station lsoa_code vs centroid codes
station lsoa_code vs POI / attractiveness lsoa codes
parking event lsoa vs station lsoa_code
parking event lsoa vs centroid codes
```

每一项至少输出：

```text
left_unique_count
right_unique_count
exact_overlap_count
overlap_rate_left
overlap_rate_right
example_left_only_codes
example_right_only_codes
```

---

## 6.1 Scotland fail-fast 规则

如果 Scotland 的关键 overlap 接近 0，例如：

```text
EV home_lsoa vs station lsoa_code overlap = 0
```

或者：

```text
EV home_lsoa vs centroid codes overlap = 0
```

则 pipeline 应该 fail fast，不要继续生成看似正常但实际无效的 station curves。

错误信息应明确说明：

```text
Scotland EV home_lsoa appears to use Data Zone 2011,
while station / destination / centroid data appears to use Data Zone 2022.
Exact lsoa_code matching is invalid until Scotland geography is unified.
```

同时应输出或更新：

```text
data_quality_report.md
```

---

# 7. 不要使用 head(n) 判断全国 coverage

当前 `--max-vehicles` 如果只是：

```text
head(n)
```

会造成严重采样偏差。

原因是 EV / person fleet 排序大致是：

```text
England -> Northern Ireland -> Scotland -> Wales
```

Scotland 车辆在文件中位置很靠后，例如从约第 1,353,895 行才开始。

因此，如果使用：

```text
--max-vehicles 10000
--max-vehicles 100000
--max-vehicles 1200000
```

且内部逻辑只是 `head(n)`，则样本可能完全不包含 Scotland EV。

这样的小样本不能用于判断全国 coverage，也不能用于验证 Scotland station matching 是否正常。

---

## 7.1 推荐修复

新增 stratified sampling 模式，例如按 prefix / country 抽样：

```text
E / England
S / Scotland
W / Wales
N / Northern Ireland
```

确保测试样本中每个国家都有车辆。

示例目标：

```text
sample_n_per_country = 5000
```

或者：

```text
sample_fraction_by_country = fixed fraction
```

---

## 7.2 最低要求

如果暂时不修改 sampling 逻辑，也必须在日志和 preflight 报告中明确警告：

```text
--max-vehicles currently uses head(n).
This sample may exclude Scotland, Wales, or Northern Ireland depending on fleet ordering.
It is not valid for national coverage validation.
```

---

# 8. 主修复：统一 Scotland small-area geography

本任务的核心修复是统一 Scotland small-area geography。

当前问题不是字符串格式问题，而是：

```text
Data Zone 2011
Data Zone 2022
```

两套 Scotland geography 边界体系不一致。

它们之间可能存在：

```text
拆分
合并
边界调整
非一一对应
```

因此，不能做 naive string mapping。

---

## 8.1 首选方案

将 Scotland EV allocation 重建或映射到 Scotland Data Zone 2022，使 private-car pipeline 中所有 Scotland small-area code 使用同一套 DZ2022 code。

需要统一的字段包括：

```text
EV home_lsoa
parking event lsoa
destination origin_lsoa
destination destination_lsoa
station lsoa_code
centroid codes
POI / attractiveness lsoa codes
neighbor table codes
station matching index codes
```

也就是说，Scotland 应统一使用 Data Zone 2022 编码体系，例如：

```text
S01013482 - S01020873
```

---

## 8.2 如果需要 crosswalk

如果必须从 DZ2011 转到 DZ2022，请使用以下可靠方法之一：

```text
官方 lookup / best-fit lookup
spatial overlay
population-weighted overlay
area-weighted overlay
```

不要使用：

```text
简单字符串替换
code range 猜测
nearest code guess
手工硬编码 mapping
```

因为 DZ2011 和 DZ2022 不是可靠的一一对应关系。

---

## 8.3 如果 repo 中没有可用 crosswalk

如果当前仓库中没有官方或可验证的 DZ2011 -> DZ2022 crosswalk，请不要假装修好了 Scotland public charging。

应先完成以下工作：

```text
1. 添加 preflight failure；
2. 在 data_quality_report.md 中明确写出 blocker；
3. 标记 Scotland public charging outputs invalid / blocked；
4. 给出需要的外部数据或 notebook 重建步骤；
5. 防止 pipeline 继续静默生成误导性 Scotland station curves。
```

---

# 9. 不要为了修这个问题改其他模型逻辑

本任务不要修改以下行为逻辑，除非确实是修复 geography mismatch 所必须：

```text
public charging station scoring
neighboring LSOA fallback
holiday week logic
trip jitter / resorting logic
EV-person binding
Huff destination sampling
front-end Active today filter
front-end sparkline / map rendering
```

当前 bug 是 geography-code consistency 问题，不是：

```text
charging preference model 问题
holiday model 问题
front-end visualization 问题
```

---

# 10. 修复后的最小验证

修复或添加 preflight 后，请做最小验证。

选择多个 Scotland DZ2022 区域，至少覆盖：

```text
Glasgow
Edinburgh
Aberdeen
Highlands
Scottish Borders
```

对每个区域验证：

```text
1. EV home_lsoa 与 station lsoa_code 使用同一套 Scotland geography；
2. home_lsoa 能在 centroid table 中找到；
3. 附近 Scottish charging stations 能被找到；
4. _build_lsoa_indices 后，该 Scottish code 能查到候选 stations；
5. match_stations_for_schedule 返回非空 Scottish station candidates；
6. 候选 station 的 distance 不是 NaN；
7. 候选 station 的 station_attractiveness 不是 NaN；
8. 候选 station 的 score 不是 NaN，也不是全 0；
9. 最终 selected station_id 能在 Web station metadata 中找到。
```

---

# 11. 修复后运行 stratified private-car sample

请运行一个包含 Scotland 的 stratified private-car sample，而不是 `head(n)` sample。

验证结果应包括：

```text
Scotland active station rate 不再异常接近 0；
Glasgow 附近出现 station curves；
Edinburgh 附近出现 station curves；
Aberdeen 附近出现 station curves；
Highlands 附近出现 station curves；
Scotland public charging events 不再大面积 failed due to no station；
England / Wales / Northern Ireland 结果没有被破坏；
orphan station_id 数量没有异常增加。
```

---

# 12. Northern Ireland 也需要复查

报告中还发现 Northern Ireland 可能也存在潜在编码问题：

```text
station file 是 N200...
destination table 中 N20000001 没有 rows；
destination table 中 N21000001 有 rows。
```

因此，NI “看起来正常”不一定代表 destination model 完全正确。

请在 preflight 中同样检查 NI：

```text
NI EV home_lsoa vs station lsoa_code
NI EV home_lsoa vs destination origin_lsoa
NI EV home_lsoa vs destination destination_lsoa
NI station lsoa_code vs centroid codes
```

如果 NI 也存在 geography mismatch，请单独汇报，不要把 Scotland 修复和 NI 问题混在一起。

---

# 13. 最终汇报要求

完成后请汇报：

```text
1. 根因确认；
2. 修改了哪些文件；
3. 新增了哪些 preflight checks；
4. Scotland geography 最终统一到了哪个版本；
5. 是否使用了 crosswalk；
6. 如果使用 crosswalk，来源和方法是什么；
7. 修复前后的 overlap 统计；
8. 修复前后的 Scotland station active rate；
9. 修复前后的 Scotland public charging failure rate；
10. stratified sample 测试结果；
11. orphan station_id 数量变化；
12. England / Wales / Northern Ireland 是否受到影响；
13. 现有测试是否仍然通过。
```

---

# 14. 如果不能安全修复

如果当前任务中不能安全完成 DZ2011 -> DZ2022 的转换，请不要输出“修复完成”。

请明确汇报：

```text
当前 Scotland EV 使用 DZ2011；
station / destination / centroid 使用 DZ2022；
exact overlap = 0；
需要官方或 spatial/population-weighted crosswalk；
在 crosswalk 可用前，Scotland private-car public charging outputs are invalid；
pipeline 已经添加 preflight fail-fast，防止继续生成误导性结果。
```

---

# 15. 验收标准

本任务完成后，至少应满足以下条件之一。

## 15.1 完整修复标准

```text
1. Scotland EV home_lsoa 已统一到 DZ2022；
2. Scotland station lsoa_code、destination table、centroid table 使用同一套 DZ2022；
3. Scotland EV home_lsoa vs station lsoa_code overlap 不再为 0；
4. Scotland EV home_lsoa vs centroid codes overlap 不再为 0；
5. Scotland station matching 能返回本地 candidates；
6. Scotland station curves 在 Glasgow / Edinburgh / Aberdeen / Highlands 等地出现；
7. Scotland public charging failure rate 不再异常；
8. 现有 England / Wales / NI 输出未被破坏；
9. 现有测试和新增测试通过。
```

## 15.2 安全阻断标准

如果无法完成 geography conversion，则至少应满足：

```text
1. pipeline 能检测到 Scotland DZ2011 vs DZ2022 mismatch；
2. pipeline 在关键 overlap 为 0 时 fail fast；
3. data_quality_report.md 明确说明 Scotland public charging outputs invalid；
4. 不再静默生成看似正常但实际错误的 Scotland station curves；
5. 提供所需 crosswalk 或重建 EV allocation 的下一步方案。
```

---

# 16. 一句话总结

这不是前端显示问题，而是后端 modelling pipeline 中 Scotland EV home_lsoa 和 station / destination / centroid geography version 不一致的问题。

请优先统一 Scotland small-area geography，并添加 preflight overlap 检查，避免继续生成表面正常但实际无效的 Scotland station curves。
