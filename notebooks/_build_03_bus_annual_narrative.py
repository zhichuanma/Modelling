from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "Modelling" / "notebooks" / "03_bus_annual_walkthrough.ipynb"


def md(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


cells = []

cells.append(
    md(
        """
        # 03 Bus Annual Simulation

        ## 0. 这个 notebook 在做什么

        这一页解释年度 bus simulation 如何把 GTFS timetable 转成 feed-year charging demand，研究问题是：在连续 SOC 约束下，一个 bus block 在一年里何时需要 depot charging、负荷如何累积到 fleet 层。读者应把它看成 annual-layer modelling audit，而不是 full-fleet production report；当前展示使用小样本 smoke block 以便快速执行。

        方法骨架是 `GTFS feed -> service_id -> block_id -> trip -> DailySchedule -> feed-year expansion -> SOC/load profile`。`service_id` 给出服务日历，`block_id (车辆运营块)` 给出同一辆车一天内串联的 trip，年度层把 active dates 与 block template 相乘，并在 inactive days 保留 24h `depot_terminus` dwell 以保持 SOC 连续。

        关键 caveat 是 feed-year 覆盖 `2026-04-17` 到 `2027-04-16`，不是 2025 calendar year；notebook smoke 使用 `warm_up_days=0`，production annual runs 应使用 `WARMUP_DAYS=14`；后文 E.5 的 depot capacity 是从 simulation 自洽反推的 synthetic map，不是真实 depot inventory。
        """
    )
)

cells.append(
    code(
        """
        import importlib
        import inspect
        import sys
        import time
        from pathlib import Path

        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        from IPython.display import display

        NOTEBOOK_START = time.time()
        NOTEBOOK_DIR = Path.cwd()
        REPO_ROOT = NOTEBOOK_DIR.parent if (NOTEBOOK_DIR.parent / "mobility").exists() else NOTEBOOK_DIR
        if not (REPO_ROOT / "mobility").exists():
            REPO_ROOT = Path.cwd().parent
        sys.path.insert(0, str(REPO_ROOT))

        expected_mobility_dir = (REPO_ROOT / "mobility").resolve()
        loaded_mobility = sys.modules.get("mobility")
        if loaded_mobility is not None:
            loaded_file_raw = getattr(loaded_mobility, "__file__", None)
            loaded_file = Path(loaded_file_raw).resolve() if loaded_file_raw else None
            if loaded_file is None or loaded_file.parent != expected_mobility_dir:
                for module_name in list(sys.modules):
                    if module_name == "mobility" or module_name.startswith("mobility."):
                        del sys.modules[module_name]

        import mobility.bus as _bus_module
        import mobility.bus.annual_simulation as _annual_simulation

        importlib.reload(_annual_simulation)
        importlib.reload(_bus_module)

        from mobility.core.constants import WARMUP_DAYS
        from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY
        from mobility.bus import (
            FEED_YEAR_END,
            FEED_YEAR_START,
            attach_lsoa,
            build_service_date_index,
            load_all_blocks,
            load_bus_vehicle_params,
            load_service_calendar,
            sample_bus_vehicle_specs,
            simulate_block_year,
            simulate_fleet_year,
            write_annual_results,
        )

        if "warm_up_days" not in inspect.signature(simulate_block_year).parameters:
            raise RuntimeError(
                "simulate_block_year was imported without warm_up_days support. "
                "Restart the kernel and rerun the setup cell; loaded function: "
                f"{simulate_block_year}"
            )

        BLOCKS_PATH = REPO_ROOT / "outputs" / "all_blocks.parquet"
        VEHICLE_PARAMS_PATH = REPO_ROOT.parent / "Data" / "EV" / "EV_prepared" / "BEV_Bus_Coach_unique_with_params_with_AC.csv"
        BUS_ANNUAL_SMOKE_PER_BLOCK_PATH = REPO_ROOT / "outputs" / "bus_annual_smoke_per_block.parquet"
        BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH = REPO_ROOT / "outputs" / "bus_annual_smoke_load_profile.parquet"
        MAIN_BUS_ANNUAL_SEED = 20260505

        plt.rcParams["figure.figsize"] = (12, 4.5)
        plt.rcParams["figure.dpi"] = 110
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        """
    )
)

cells.append(
    md(
        """
        ## A. Feed-Year Calendar

        本段解释 service calendar 如何限定年度仿真的日期边界。`service_id` 表示同一组 active dates 的服务模式，`calendar_dates` 记录临时加班或取消，因此年度层必须按 feed-year 逐日展开，而不是假设每个 block 每天都运行。
        """
    )
)

cells.append(
    code(
        """
        all_blocks = attach_lsoa(load_all_blocks(BLOCKS_PATH))
        service_calendar = load_service_calendar()
        service_date_index = build_service_date_index(
            all_blocks["service_id"].astype(str).unique(),
            FEED_YEAR_START,
            FEED_YEAR_END,
            service_calendar,
        )
        active_days_by_service = pd.Series(
            {service_id: len(dates) for service_id, dates in service_date_index.items()},
            name="active_days",
        )
        display(
            pd.DataFrame(
                [
                    ("feed_year_start", FEED_YEAR_START.isoformat()),
                    ("feed_year_end", FEED_YEAR_END.isoformat()),
                    ("gtfs_calendar_services", service_calendar.calendar["service_id"].nunique()),
                    ("gtfs_exception_services", service_calendar.calendar_dates["service_id"].nunique()),
                    ("block_services", all_blocks["service_id"].nunique()),
                    ("services_active_in_feed_year", int((active_days_by_service > 0).sum())),
                    ("services_active_in_2025", 0),
                ],
                columns=["metric", "value"],
            )
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.hist(active_days_by_service, bins=40, color="tab:blue", alpha=0.78)
        ax.set(title="GTFS active days per service_id in the feed year", xlabel="active days", ylabel="service_ids")
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        ## A.5 出行模型逻辑

        本段解释原始 GTFS feed 如何变成 simulator 可以消费的 `DailySchedule`。概念阶梯是 `GTFS feed (calendar.txt + trips.txt + stop_times.txt) -> service_id -> block_id -> trip -> DailySchedule -> 年度展开`：`service_id` 定义哪些日历日属于同一服务模式，`block_id` 把同一辆车一天里的 trip 串起来，`DailySchedule` 再把 trips 与 parking events 放到同一个 24h 时间轴上。

        Inactive days 不会删除；它们被替换为 24h `depot_terminus` dwell，使上一天结束 SOC 能连续传到下一次 active service。下面只打印 protagonist block 的前三行 trip，并画一个 active service day timeline；这只是解释建模对象，不是 fleet-level 结论。
        """
    )
)

cells.append(
    code(
        """
        rng = np.random.default_rng(MAIN_BUS_ANNUAL_SEED)
        dates = pd.date_range(FEED_YEAR_START, FEED_YEAR_END, freq="D")
        block_stats = all_blocks.groupby("block_id", sort=False).agg(
            agency_id=("agency_id", "first"),
            service_id=("service_id", "first"),
            block_source=("block_source", "first"),
            n_trips=("trip_id", "count"),
            total_km=("distance_km", "sum"),
            start_h=("start_h", "min"),
            end_h=("end_h", "max"),
        )
        block_stats["active_days"] = block_stats["service_id"].astype(str).map(active_days_by_service).fillna(0).astype(int)
        candidates = block_stats[
            block_stats["active_days"].gt(0)
            & block_stats["block_source"].eq("native")
            & block_stats["total_km"].between(40.0, 250.0)
        ].sort_index()
        protagonist_id = str(candidates.index[int(rng.integers(0, len(candidates)))])
        protagonist_block = all_blocks[all_blocks["block_id"].astype(str).eq(protagonist_id)].copy()
        protagonist_service_id = str(protagonist_block["service_id"].iloc[0])
        protagonist_active_dates = service_date_index[protagonist_service_id]

        vehicle_params = load_bus_vehicle_params(VEHICLE_PARAMS_PATH)
        protagonist_vehicle = sample_bus_vehicle_specs(vehicle_params, rng, n=1).iloc[0]
        protagonist_result = simulate_block_year(
            protagonist_block,
            protagonist_active_dates,
            protagonist_vehicle,
            FEED_YEAR_START,
            FEED_YEAR_END,
            soc_init=1.0,
            warm_up_days=0,
        )

        trip_preview = protagonist_block.rename(
            columns={"start_stop": "start_stop_name", "end_stop": "end_stop_name"}
        )
        trip_columns = [
            "block_id",
            "service_id",
            "trip_id",
            "distance_km",
            "start_h",
            "end_h",
            "start_lsoa",
            "end_lsoa",
            "start_stop_name",
            "end_stop_name",
        ]
        display(trip_preview[[col for col in trip_columns if col in trip_preview.columns]].head(3))

        one_day = next(
            (schedule for schedule in protagonist_result["schedules"] if schedule.trips),
            protagonist_result["schedules"][0],
        )
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        for trip in one_day.trips:
            ax.barh(
                0,
                trip.arrival_time - trip.departure_time,
                left=trip.departure_time,
                height=0.38,
                color="tab:blue",
                alpha=0.85,
            )
        for park in one_day.parking_events:
            ax.barh(
                1,
                park.end_time - park.start_time,
                left=park.start_time,
                height=0.38,
                color="lightgray" if park.location_purpose == "depot_terminus" else "khaki",
            )
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["trip", "parking"])
        ax.set(
            title="Protagonist block: one active service day timeline",
            xlabel="hour of day",
            xlim=(0, 24),
        )
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        ## B. Pick a Protagonist Block

        本段说明 notebook 为什么使用一个 native 且日里程适中的 protagonist block。`native` 避免把推断 block 的不确定性混入教学例子，40-250 km 区间避开极短 shuttle 与极端长链条，使 SOC 曲线更适合作为 mechanism trace。
        """
    )
)

cells.append(
    code(
        """
        display(block_stats.loc[[protagonist_id]])
        display(pd.DataFrame([protagonist_vehicle]))
        """
    )
)

cells.append(
    md(
        """
        ## B.5 充电逻辑

        本段解释 charging model 的最小物理递推：$SOC_{t+1} = \\mathrm{clip}\\left[SOC_t - \\frac{E_{trip,t}}{B} + \\frac{P_{park,t} \\cdot \\Delta t}{B},\\ 0,\\ 1\\right]$。`depot_terminus` 在当前 simulator 中表示车辆回到可慢充位置，但不绑定真实经纬度；E.5 再用 home LSOA 做 post-hoc attribution。

        `warm_up_days=0` 是 smoke notebook 的速度选择，production annual runs 应使用 `WARMUP_DAYS=14`，让从 `soc_init=1.0` 起步造成的前期偏差先收敛再进入正式记录。不可行性标签来自 `mobility/bus/feasibility.py`，用于区分电池容量、首段出发 SOC、全天能量预算与中途时间错配四类风险。
        """
    )
)

cells.append(
    code(
        """
        parameter_table = pd.DataFrame(
            [
                ("battery_kwh", float(protagonist_vehicle["battery_kwh"]), "vehicle spec sample (BEV_Bus_Coach CSV)"),
                (
                    "consumption_kwh_per_km",
                    float(protagonist_vehicle["consumption_kwh_per_km"]),
                    "vehicle spec sample",
                ),
                ("depot_charge_kw", float(protagonist_vehicle["depot_charge_kw"]), "vehicle spec sample"),
                ("layover_charge_kw", 0.0, "notebook config (allow_layover_charging=False)"),
                ("warm_up_days", 0, f"notebook smoke; production WARMUP_DAYS={WARMUP_DAYS}"),
                ("soc_init", 1.0, "fully charged on day 0"),
            ],
            columns=["param", "value", "source"],
        )
        display(parameter_table)

        infeasibility_table = pd.DataFrame(
            [
                ("single_trip_exceeds_battery", "单 trip 能耗 > 电池可用容量"),
                ("starts_below_min_required", "第一段 trip 出发前 SOC 不足以完成该 trip"),
                ("depot_only_insufficient", "仅 depot 充电时全天总能耗超过 soc_init x battery + depot 充电潜力"),
                ("midday_depletion", "上述三条都不命中，但时间错配导致中途 SOC 触底"),
            ],
            columns=["reason", "meaning"],
        )
        display(infeasibility_table)
        """
    )
)

cells.append(
    md(
        """
        ## C. Single Block Annual SOC

        本段把 protagonist block 扩展到 feed-year，并检查 SOC continuity 是否真的跨 active 与 inactive days 保持。所有数字只代表这个 protagonist block，feed-year 日期不是 2025 calendar year。
        """
    )
)

cells.append(
    code(
        """
        display(
            pd.DataFrame(
                [
                    ("active_days", protagonist_result["active_days"]),
                    ("annual_distance_km", round(protagonist_result["annual_distance_km"], 2)),
                    ("annual_energy_kwh", round(protagonist_result["annual_energy_kwh"], 2)),
                    ("energy_charged_kwh", round(protagonist_result["energy_charged_kwh"], 2)),
                    ("soc_min", round(protagonist_result["soc_min"], 4)),
                    ("soc_end", round(protagonist_result["soc_end"], 4)),
                ],
                columns=["metric", "value"],
            )
        )

        soc_days = protagonist_result["soc"].reshape(len(dates), STEPS_PER_DAY)
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(dates, soc_days.min(axis=1), color="tab:red", lw=1.4, label="daily min SOC")
        ax.plot(dates, soc_days[:, -1], color="tab:green", lw=1.4, label="end-of-day SOC")
        ax.set(title="Protagonist block annual SOC continuity", ylabel="SOC")
        ax.legend()
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        ## D. Small Fleet Annual Load

        本段把相同年度逻辑应用到一个很小的 block sample，以展示 per-block SOC 结果如何聚合成 fleet charging load。样本规模是 smoke-only，不代表全英国 bus fleet 的年度总量。
        """
    )
)

cells.append(
    code(
        """
        sample_block_ids = candidates.sample(n=min(4, len(candidates)), random_state=MAIN_BUS_ANNUAL_SEED).index.astype(str)
        small_fleet_blocks = all_blocks[all_blocks["block_id"].astype(str).isin(sample_block_ids)].copy()
        fleet_per_block, fleet_load_kw = simulate_fleet_year(
            small_fleet_blocks,
            service_date_index,
            vehicle_params=vehicle_params,
            vehicle_rng=np.random.default_rng(MAIN_BUS_ANNUAL_SEED + 1),
            start_date=FEED_YEAR_START,
            end_date=FEED_YEAR_END,
            warm_up_days=0,
            progress_interval=4,
        )
        display(
            fleet_per_block[
                [
                    "agency_id",
                    "service_id",
                    "active_days",
                    "annual_distance_km",
                    "annual_energy_kwh",
                    "soc_min",
                    "vehicle_gen_model",
                ]
            ]
        )

        daily_energy = fleet_load_kw.sum(axis=1) * STEP_HOURS
        per_block_path, load_profile_path = write_annual_results(
            fleet_per_block,
            fleet_load_kw,
            start_date=FEED_YEAR_START,
            end_date=FEED_YEAR_END,
            per_block_path=BUS_ANNUAL_SMOKE_PER_BLOCK_PATH,
            load_profile_path=BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH,
        )
        display(
            pd.DataFrame(
                [
                    ("per_block_path", str(per_block_path)),
                    ("load_profile_path", str(load_profile_path)),
                    ("per_block_rows", len(fleet_per_block)),
                    ("load_profile_rows", int(fleet_load_kw.shape[0] * fleet_load_kw.shape[1])),
                    ("daily_energy_kwh_min", round(float(daily_energy.min()), 2)),
                    ("daily_energy_kwh_max", round(float(daily_energy.max()), 2)),
                ],
                columns=["metric", "value"],
            )
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(dates, daily_energy, color="tab:blue", lw=1.4)
        ax.set(title="Small bus-fleet annual charging energy", ylabel="kWh/day")
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        ## E. Annual Story Slices

        这张图检查 active service days 的 weekday/weekend 差异是否足够明显。所有结果来自 4-block smoke sample，因此只能说明机制和 plotting contract，不能外推为 fleet conclusion。
        """
    )
)

cells.append(
    code(
        """
        daily_energy_frame = pd.DataFrame(
            {
                "date": dates,
                "daily_total_kwh": daily_energy,
                "day_type": np.where(dates.dayofweek < 5, "weekday", "weekend"),
            }
        )
        active_daily_energy = daily_energy_frame[daily_energy_frame["daily_total_kwh"].gt(0.0)].copy()
        weekday_weekend_summary = (
            active_daily_energy.groupby("day_type")["daily_total_kwh"]
            .agg(["count", "mean", "median", "min", "max"])
            .reset_index()
        )
        if {"weekday", "weekend"}.issubset(set(weekday_weekend_summary["day_type"])):
            weekday_mean = float(weekday_weekend_summary.loc[weekday_weekend_summary["day_type"].eq("weekday"), "mean"].iloc[0])
            weekend_mean = float(weekday_weekend_summary.loc[weekday_weekend_summary["day_type"].eq("weekend"), "mean"].iloc[0])
            delta_pct = 100.0 * (weekend_mean - weekday_mean) / weekday_mean if weekday_mean else 0.0
            observation = (
                f"Weekend active-day charging is {abs(delta_pct):.1f}% lower than weekday charging."
                if delta_pct < -5.0
                else "The representative service sample does not show a strong weekday/weekend split."
            )
        else:
            observation = "The smoke sample does not contain both active weekday and active weekend service days."
        display(weekday_weekend_summary)
        display(pd.DataFrame([{"slice": "weekday_vs_weekend", "observation": observation}]))

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        if not active_daily_energy.empty:
            active_daily_energy.boxplot(column="daily_total_kwh", by="day_type", ax=ax, grid=False)
            ax.set_title("Active-day fleet charging: weekday vs weekend")
            ax.set_xlabel("")
            ax.set_ylabel("kWh/day")
            fig.suptitle("")
            ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        本图把 Christmas 与 Easter 附近窗口直接标到 annual load trace 上，目的是检查 GTFS feed-year service pattern 是否在假期周改变。smoke block 的服务日历不等于真实全 fleet 调度。
        """
    )
)

cells.append(
    code(
        """
        holiday_windows = [
            ("Christmas week", pd.Timestamp("2026-12-22"), pd.Timestamp("2026-12-28")),
            ("Easter week", pd.Timestamp("2027-03-22"), pd.Timestamp("2027-03-28")),
        ]
        baseline_mean = float(daily_energy_frame["daily_total_kwh"].mean())
        holiday_rows = []
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(daily_energy_frame["date"], daily_energy_frame["daily_total_kwh"], color="tab:blue", lw=1.2)
        for label, start, end in holiday_windows:
            mask = daily_energy_frame["date"].between(start, end)
            window_mean = float(daily_energy_frame.loc[mask, "daily_total_kwh"].mean())
            change_pct = 100.0 * (window_mean - baseline_mean) / baseline_mean if baseline_mean else 0.0
            holiday_rows.append(
                {
                    "window": label,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "mean_kwh": round(window_mean, 2),
                    "vs_feed_mean_pct": round(change_pct, 1),
                    "interpretation": (
                        "visible holiday-week reduction"
                        if change_pct < -5.0
                        else "no clear holiday reduction in the GTFS representative service assumption"
                    ),
                }
            )
            ax.axvspan(start, end, alpha=0.16, label=label)
        ax.set(title="Holiday-week call-outs on annual fleet charging", ylabel="kWh/day")
        ax.legend()
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        display(pd.DataFrame(holiday_rows))
        """
    )
)

cells.append(
    md(
        """
        本图把 15-minute load matrix 汇总成 month-by-hour heatmap，用来观察 depot charging 是否集中在某些夜间小时。这里展示的是 smoke sample 的 load shape，不是 production load magnitude。
        """
    )
)

cells.append(
    code(
        """
        hourly_load_kw = fleet_load_kw.reshape(len(dates), 24, 4).mean(axis=2)
        monthly_hourly_load = (
            pd.DataFrame(hourly_load_kw, index=dates, columns=np.arange(24))
            .groupby(lambda value: value.month)
            .mean()
            .reindex(range(1, 13))
        )
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        image = ax.imshow(monthly_hourly_load, aspect="auto", cmap="viridis")
        ax.set(
            title="Mean fleet load by month and hour of day",
            xlabel="hour of day",
            ylabel="month",
        )
        ax.set_xticks(np.arange(0, 24, 2))
        ax.set_yticks(np.arange(12))
        ax.set_yticklabels(range(1, 13))
        fig.colorbar(image, ax=ax, label="kW")
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        本图对比 active-heavy 与 sparse-service blocks 的 daily-min SOC（每天 96 个 step 里最低的那一刻），检验 service frequency 对每日"最累时刻"的影响。end-of-day SOC 已被夜间充电拉高，掩盖了真实差异；daily-min 直接显示电池"最深下沉"。为了让对比反映"使用频率"而不是"恰好抽到哪辆车"，两条线共用同一辆 vehicle spec；每类从分位数子集里取 total_km 中位 block 而不是极值。它仍是机制切片，单条曲线代表一条 block，不代表 fleet 分布。
        """
    )
)

cells.append(
    code(
        """
        active_days_positive = block_stats["active_days"][block_stats["active_days"].gt(0)]
        q10, q90 = active_days_positive.quantile([0.10, 0.90])
        contrast_specs = [
            ("active-heavy", block_stats[block_stats["active_days"].ge(q90)].sort_values("total_km")),
            ("sparse-service", block_stats[block_stats["active_days"].between(1, q10)].sort_values("total_km")),
        ]
        contrast_vehicle = sample_bus_vehicle_specs(
            vehicle_params,
            np.random.default_rng(MAIN_BUS_ANNUAL_SEED + 99),
            n=1,
        ).iloc[0]
        contrast_rows = []
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        for label, options in contrast_specs:
            if options.empty:
                contrast_rows.append({"slice": label, "block_id": None, "active_days": 0, "note": "no matching block in this feed"})
                continue
            median_pos = len(options) // 2
            block_id = str(options.index[median_pos])
            block = all_blocks[all_blocks["block_id"].astype(str).eq(block_id)].copy()
            service_id = str(block["service_id"].iloc[0])
            result = simulate_block_year(
                block,
                service_date_index.get(service_id, ()),
                contrast_vehicle,
                FEED_YEAR_START,
                FEED_YEAR_END,
                warm_up_days=0,
            )
            soc_by_day = result["soc"].reshape(len(dates), STEPS_PER_DAY)
            ax.plot(dates, soc_by_day.min(axis=1), lw=1.3, label=f"{label}: {block_id}")
            contrast_rows.append(
                {
                    "slice": label,
                    "block_id": block_id,
                    "active_days": result["n_active_dates"],
                    "daily_km": round(float(options.loc[block_id, "total_km"]), 1),
                    "soc_min": round(result["soc_min"], 4),
                    "soc_end": round(result["soc_end"], 4),
                    "vehicle_gen_model": contrast_vehicle["gen_model"],
                    "note": "simulated",
                }
            )
        ax.set(title="Active-heavy vs sparse-service block SOC", ylabel="daily min SOC")
        ax.legend()
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        display(pd.DataFrame(contrast_rows))
        """
    )
)

cells.append(
    md(
        """
        ## E.5 LSOA -> Synthetic Depot Map

        本研究当前阶段把每条 bus block 的 "home LSOA" 定义为其 feed-year 内 `end_lsoa` 的众数；该 block 的 `depot_charge_kw`（来自 vehicle spec）即视为该 LSOA 内一个 synthetic depot 的额定功率。这是 depot 在本仿真中的 operational definition："block 末班停靠的地方就是充电的地方"。

        每个 LSOA 的 depot 总容量 = 所有 home 在该 LSOA 的 block 的 `depot_charge_kw` 之和；该容量地图完全从 simulation 反推，不依赖外部 depot inventory。公共充电桩（OCM）当前未纳入 bus 充电基础设施，因为它们并非为 bus 这类大功率长停留场景设计；两类放宽（公共桩 eligibility、utilization & queueing、real depot inventory）见 [`docs/bus_charging_next_steps.md`](../../docs/bus_charging_next_steps.md)。

        归因规则是 `home_lsoa = mode(end_lsoa)` per block，且每个 block 的 `energy_charged_kwh` 与 `depot_charge_kw` 同时归到该 home LSOA。这个规则只用于 post-hoc 可视化，不替换 simulation 输入，也不对真实 infrastructure 充足性下结论。
        """
    )
)

cells.append(
    code(
        """
        def _mode_or_unknown(values: pd.Series) -> str:
            cleaned = values.dropna().astype(str)
            cleaned = cleaned[cleaned.str.strip().ne("")]
            mode_values = cleaned.mode()
            return str(mode_values.iloc[0]) if not mode_values.empty else "unknown"


        home_lsoa_by_block = all_blocks.groupby("block_id")["end_lsoa"].agg(_mode_or_unknown)
        demand_df = fleet_per_block.reset_index().merge(
            home_lsoa_by_block.rename("home_lsoa"),
            left_on="block_id",
            right_index=True,
            how="left",
        )
        demand_df["home_lsoa"] = demand_df["home_lsoa"].fillna("unknown").replace("", "unknown")
        lsoa_view = (
            demand_df.groupby("home_lsoa")
            .agg(
                n_home_blocks=("block_id", "nunique"),
                sim_kwh_year=("energy_charged_kwh", "sum"),
                depot_total_kw=("depot_charge_kw", "sum"),
            )
            .sort_values("sim_kwh_year", ascending=False)
        )
        lsoa_view["ceiling_kwh_year"] = lsoa_view["depot_total_kw"] * 8760
        lsoa_view["gap_ratio"] = np.where(
            lsoa_view["ceiling_kwh_year"].gt(0.0),
            lsoa_view["sim_kwh_year"] / lsoa_view["ceiling_kwh_year"],
            np.nan,
        )

        if lsoa_view.empty:
            display(pd.DataFrame({"note": ["No LSOA attribution rows available for the smoke sample."]}))
        else:
            top_n = 50
            top = lsoa_view.head(top_n).copy()
            others_kwh = lsoa_view["sim_kwh_year"].iloc[top_n:].sum()
            plot_df = pd.concat(
                [
                    top[["sim_kwh_year"]],
                    pd.DataFrame({"sim_kwh_year": [others_kwh]}, index=["others"]),
                ]
            )
            fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
            ax.bar(range(len(plot_df)), plot_df["sim_kwh_year"] / 1e6, color="tab:blue", alpha=0.85)
            ax.set_xticks(range(len(plot_df)))
            ax.set_xticklabels(plot_df.index.astype(str), rotation=90, fontsize=6)
            ax.set(
                title=f"Top-{top_n} home LSOAs by simulated annual bus charging energy",
                xlabel="LSOA code",
                ylabel="GWh/year",
            )
            ax.grid(alpha=0.25, axis="y")
            plt.tight_layout()
            plt.show()

            fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
            marker_size = np.maximum(lsoa_view["depot_total_kw"] / 5.0, 12.0)
            ax.scatter(
                lsoa_view["n_home_blocks"],
                lsoa_view["sim_kwh_year"],
                s=marker_size,
                alpha=0.5,
                color="tab:purple",
            )
            ax.set(
                title="LSOA service density: blocks vs annual demand (marker proportional to depot kW)",
                xlabel="n_home_blocks per LSOA",
                ylabel="sim_kwh_year",
            )
            ax.grid(alpha=0.25)
            plt.tight_layout()
            plt.show()

            fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
            red_mask = lsoa_view["gap_ratio"].gt(0.5).fillna(False)
            ax.scatter(
                lsoa_view.loc[~red_mask, "depot_total_kw"],
                lsoa_view.loc[~red_mask, "sim_kwh_year"],
                alpha=0.5,
                color="tab:gray",
                label="gap_ratio <= 0.5",
            )
            ax.scatter(
                lsoa_view.loc[red_mask, "depot_total_kw"],
                lsoa_view.loc[red_mask, "sim_kwh_year"],
                alpha=0.8,
                color="tab:red",
                label="gap_ratio > 0.5",
            )
            max_kw = float(lsoa_view["depot_total_kw"].max())
            kw_range = np.linspace(0.0, max_kw, 50)
            ax.plot(
                kw_range,
                kw_range * 8760,
                color="k",
                lw=1.0,
                linestyle="--",
                label="ceiling = kW x 8760",
            )
            ax.set(
                title="LSOA depot saturation: annual demand vs theoretical ceiling",
                xlabel="depot_total_kw",
                ylabel="sim_kwh_year",
            )
            ax.legend()
            ax.grid(alpha=0.25)
            plt.tight_layout()
            plt.show()

        gap_top_n = (
            lsoa_view.reset_index()
            .rename(columns={"home_lsoa": "lsoa_code"})
            [["lsoa_code", "n_home_blocks", "sim_kwh_year", "depot_total_kw", "ceiling_kwh_year", "gap_ratio"]]
            .sort_values("sim_kwh_year", ascending=False)
            .head(10)
            .round(2)
        )
        display(gap_top_n)
        """
    )
)

cells.append(
    md(
        """
        ## E.6 M1 chain-mode diagnostics (transparency)

        本节展示的是 operator-administrative 视角的 depot inventory（由 `scripts/run_bus_pipeline.py` 产生的 M1 outputs），其中 `depot_confidence` 大部分是 `low`，因为多数 agency 没有 TxC garage 数据，只能用 operator centroid 合成。E.5 的主分析不依赖这些数据，原因正是这一数据现状；E.6 在此呈现仅为对外透明度（"如果用现有 inventory 会怎样"）。

        下列图只描述当前 inventory 数据现状，不参与 E.5 的 `lsoa_view`、`fleet_per_block` 或缺口表；完整 follow-up 路径见 `docs/bus_charging_next_steps.md` §4。若 M1 outputs 不存在，cell 会显示 missing-file table 并优雅跳过。
        """
    )
)

cells.append(
    code(
        """
        M1_OUTPUT_DIR = REPO_ROOT / "outputs"
        if (
            not (M1_OUTPUT_DIR / "resolution_summary.parquet").exists()
            and (M1_OUTPUT_DIR / "m1_smoke" / "resolution_summary.parquet").exists()
        ):
            M1_OUTPUT_DIR = M1_OUTPUT_DIR / "m1_smoke"

        m1_paths = {
            "depot_registry": M1_OUTPUT_DIR / "depot_registry.parquet",
            "vehicles": M1_OUTPUT_DIR / "vehicles.parquet",
            "vehicle_assignments": M1_OUTPUT_DIR / "vehicle_assignments.parquet",
            "vehicle_day_events": M1_OUTPUT_DIR / "vehicle_day_events.parquet",
            "resolution_summary": M1_OUTPUT_DIR / "resolution_summary.parquet",
        }
        missing_m1 = [name for name, path in m1_paths.items() if not path.exists()]

        if missing_m1:
            display(pd.DataFrame({"missing_m1_output": missing_m1, "expected_dir": str(M1_OUTPUT_DIR)}))
        else:
            depot_registry_m1 = pd.read_parquet(m1_paths["depot_registry"])
            vehicles_m1 = pd.read_parquet(m1_paths["vehicles"])
            assignments_m1 = pd.read_parquet(m1_paths["vehicle_assignments"])
            events_m1 = pd.read_parquet(m1_paths["vehicle_day_events"])
            resolution_m1 = pd.read_parquet(m1_paths["resolution_summary"])

            depot_counts = depot_registry_m1["depot_confidence"].value_counts().reindex(["high", "medium", "low"]).fillna(0)
            fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=120)
            bars = ax.bar(depot_counts.index, depot_counts.values, color=["#3c7d5a", "#d39c3f", "#8b8f97"])
            ax.set_title("Depots by confidence")
            ax.set_xlabel("")
            ax.set_ylabel("depots")
            ax.grid(alpha=0.25, axis="y")
            for bar in bars:
                height = bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2, height, f"{height:,.0f}", ha="center", va="bottom", fontsize=9)
            plt.tight_layout()
            plt.show()

            real_fleet = vehicles_m1.dropna(subset=["depot_id"]).groupby("depot_id").size().rename("real")
            overflow_fleet = (
                assignments_m1[assignments_m1["vehicle_provenance"].eq("synthetic_overflow")]
                .groupby("depot_id")["vehicle_id"]
                .nunique()
                .rename("synthetic_overflow")
            )
            fleet_by_depot = pd.concat([real_fleet, overflow_fleet], axis=1).fillna(0)
            fleet_by_depot["total"] = fleet_by_depot["real"] + fleet_by_depot["synthetic_overflow"]
            fleet_plot = fleet_by_depot.sort_values("total", ascending=False).head(25).sort_values("total", ascending=True)
            fig_height = max(5.5, 0.30 * len(fleet_plot) + 1.4)
            fig, ax = plt.subplots(figsize=(12, fig_height), dpi=120)
            fleet_plot[["real", "synthetic_overflow"]].plot(kind="barh", stacked=True, ax=ax, color=["#4f83cc", "#c75c5c"])
            ax.set_title("Fleet by depot (top 25)")
            ax.set_xlabel("vehicles")
            ax.set_ylabel("")
            ax.grid(alpha=0.25, axis="x")
            ax.legend(title="")
            plt.tight_layout()
            plt.show()

            daily_chains = (
                assignments_m1[["service_date", "depot_id", "chain_id"]]
                .drop_duplicates()
                .groupby(["service_date", "depot_id"])
                .size()
                .rename("chains")
                .reset_index()
            )
            daily_chains["service_date"] = pd.to_datetime(daily_chains["service_date"])
            top_chain_depots = daily_chains.groupby("depot_id")["chains"].sum().nlargest(8).index
            daily_total = daily_chains.groupby("service_date")["chains"].sum().sort_index()
            fig, ax = plt.subplots(figsize=(12, 5.2), dpi=120)
            ax.plot(daily_total.index, daily_total.values, color="#222222", linewidth=2.0, label="all depots")
            for depot_id in top_chain_depots:
                group = daily_chains[daily_chains["depot_id"].eq(depot_id)].sort_values("service_date")
                ax.plot(
                    group["service_date"],
                    group["chains"],
                    linewidth=1.2,
                    alpha=0.75,
                    label=str(depot_id)[:24],
                )
            ax.set_title("Daily chains by depot (top 8 shown individually)")
            ax.set_xlabel("")
            ax.set_ylabel("chains")
            ax.grid(alpha=0.25)
            ax.legend(fontsize=8, ncol=3, loc="upper left")
            plt.tight_layout()
            plt.show()

            resolution_mix_all = (
                resolution_m1.groupby(["depot_id", "resolution_level"])
                .size()
                .unstack(fill_value=0)
                .sort_index()
            )
            resolution_totals = resolution_mix_all.sum(axis=1).sort_values(ascending=False)
            top_resolution_depots = resolution_totals.head(25).index
            resolution_plot = resolution_mix_all.loc[top_resolution_depots].copy()
            if len(resolution_mix_all) > len(resolution_plot):
                resolution_plot.loc["other depots"] = resolution_mix_all.drop(index=top_resolution_depots).sum(axis=0)
            resolution_plot = resolution_plot.loc[resolution_plot.sum(axis=1).sort_values(ascending=True).index]
            fig_height = max(6.5, 0.34 * len(resolution_plot) + 1.8)
            fig, ax = plt.subplots(figsize=(12, fig_height), dpi=120)
            level_palette = {
                0: "#4f83cc",
                1: "#6f9f5f",
                2: "#d6a34a",
                3: "#c75c5c",
                4: "#7b65a7",
            }
            colors = [level_palette.get(level, "#8b8f97") for level in resolution_plot.columns]
            resolution_plot.plot(kind="barh", stacked=True, ax=ax, color=colors)
            ax.set_title("Resolution levels by depot (top 25 + other depots)")
            ax.set_xlabel("chains")
            ax.set_ylabel("")
            ax.grid(alpha=0.25, axis="x")
            ax.legend(title="resolution_level", ncol=min(len(resolution_plot.columns), 5), fontsize=8)
            plt.tight_layout()
            plt.show()
        """
    )
)

cells.append(
    code(
        """
        if not missing_m1:
            station_rows = []
            for row in resolution_m1.itertuples(index=False):
                stations = [
                    station
                    for station in str(row.station_ids_used_csv).split(",")
                    if station and not station.startswith("depot_")
                ]
                if not stations:
                    continue
                share = float(row.opportunity_charge_kwh) / len(stations) if stations else 0.0
                station_rows.extend({"station_id": station, "charge_kwh": share} for station in stations)
            station_charge = pd.DataFrame(station_rows)
            if station_charge.empty:
                display(pd.DataFrame({"public_charger_station": [], "charge_kwh": []}))
            else:
                top_public = station_charge.groupby("station_id", as_index=False)["charge_kwh"].sum().nlargest(10, "charge_kwh")
                fig, ax = plt.subplots(figsize=(12, 5.2), dpi=120)
                ax.barh(top_public["station_id"], top_public["charge_kwh"], color="#6f7f3f")
                ax.invert_yaxis()
                ax.set_xlabel("charge_kwh")
                ax.set_ylabel("")
                ax.set_title("Top public charger stations")
                ax.grid(alpha=0.25, axis="x")
                plt.tight_layout()
                plt.show()

            import json
            from matplotlib.collections import PatchCollection
            from matplotlib.patches import Polygon as MplPolygon

            def _outer_geojson_rings(geometry):
                if not geometry:
                    return
                if geometry.get("type") == "Polygon":
                    for ring in geometry.get("coordinates", [])[:1]:
                        yield np.asarray(ring, dtype=float)
                elif geometry.get("type") == "MultiPolygon":
                    for polygon in geometry.get("coordinates", []):
                        if polygon:
                            yield np.asarray(polygon[0], dtype=float)

            def _plot_uk_basemap(ax, basemap_path):
                with basemap_path.open() as handle:
                    geojson = json.load(handle)
                patches = []
                for feature in geojson.get("features", []):
                    props = feature.get("properties", {})
                    country = props.get("CNTR_CODE")
                    if country and country != "UK":
                        continue
                    for ring in _outer_geojson_rings(feature.get("geometry")):
                        if ring.ndim == 2 and len(ring) >= 3:
                            patches.append(MplPolygon(ring[:, :2], closed=True))
                if not patches:
                    return 0
                collection = PatchCollection(
                    patches,
                    facecolor="#f3f1ec",
                    edgecolor="#cfd5d8",
                    linewidths=0.35,
                    zorder=0,
                )
                ax.add_collection(collection)
                return len(patches)

            fig, ax = plt.subplots(figsize=(9.2, 9.4), dpi=120)
            basemap_candidates = [
                REPO_ROOT.parent / "Data" / "Network" / "NUTS_RG_01M_2021_4326_LEVL_3.geojson",
                REPO_ROOT.parent / "Data" / "Charging_stations" / "NUTS_RG_01M_2021_4326_LEVL_3.geojson",
                REPO_ROOT.parent / "Data" / "Geometry_UK" / "LAD_May_2024_UK_BFE.geojson",
            ]
            basemap_note = "no UK basemap file found"
            for basemap_path in basemap_candidates:
                if not basemap_path.exists():
                    continue
                try:
                    n_basemap_polygons = _plot_uk_basemap(ax, basemap_path)
                except Exception as exc:
                    basemap_note = f"basemap unavailable: {exc}"
                    continue
                if n_basemap_polygons:
                    basemap_note = f"UK NUTS3 basemap: {basemap_path.relative_to(REPO_ROOT.parent)}"
                    break

            depot_map = depot_registry_m1.dropna(subset=["lon", "lat"]).copy()
            depot_map = depot_map[
                depot_map["lon"].between(-10.5, 3.0) & depot_map["lat"].between(49.0, 62.0)
            ]
            marker_by_confidence = {"high": "o", "medium": "s", "low": "^"}
            color_by_confidence = {"high": "#2f6f4e", "medium": "#c88a2a", "low": "#6f737b"}
            for confidence in ["high", "medium", "low"]:
                group = depot_map[depot_map["depot_confidence"].eq(confidence)]
                if group.empty:
                    continue
                marker_size = np.clip(28 + np.sqrt(group["n_candidate_vehicles"].fillna(0).clip(lower=0)) * 10, 32, 240)
                ax.scatter(
                    group["lon"],
                    group["lat"],
                    s=marker_size,
                    marker=marker_by_confidence.get(confidence, "x"),
                    color=color_by_confidence.get(confidence, "#8b8f97"),
                    edgecolor="black",
                    linewidth=0.5,
                    alpha=0.86,
                    label=f"{confidence} ({len(group)})",
                    zorder=3,
                )
            ax.set_xlabel("lon")
            ax.set_ylabel("lat")
            ax.set_title("M1 depots on UK basemap")
            ax.set_xlim(-8.9, 2.2)
            ax.set_ylim(49.7, 61.2)
            ax.set_aspect("equal", adjustable="box")
            ax.grid(alpha=0.18, zorder=1)
            ax.legend(title="confidence")
            ax.text(
                0.0,
                -0.11,
                (
                    f"{basemap_note}. Marker size scales with n_candidate_vehicles.\\n"
                    "Virtual operator-centroid depots are illustrative only; LSOA attribution for these depots is low-confidence."
                ),
                transform=ax.transAxes,
                fontsize=9,
                va="top",
            )
            fig.subplots_adjust(left=0.08, right=0.98, top=0.93, bottom=0.16)
            plt.show()
        """
    )
)

cells.append(
    md(
        """
        ## F. Honest Labels

        本表汇总 notebook 中最容易被误读的 modelling labels。新增 LSOA/depot 行把 E.5 的 post-hoc attribution、synthetic depot capacity、public charger exclusion 与 next-step scope 明确写出。
        """
    )
)

cells.append(
    code(
        """
        honest_labels = pd.DataFrame(
            [
                ("Calendar window", f"{FEED_YEAR_START} to {FEED_YEAR_END}", "This follows the current GTFS feed-year, not 2025."),
                ("SOC policy", "continuous", "SOC carries across active and inactive days."),
                ("Inactive days", "all-day depot_terminus", "They allow charging recovery between service days."),
                ("Fleet scale", f"{len(sample_block_ids)} sampled blocks", "The notebook is intentionally small; full fleet uses the same simulate_fleet_year API."),
                ("Warm-up", "warm_up_days=0", f"Smoke runs stay fast; production annual runs should use WARMUP_DAYS={WARMUP_DAYS} for a steadier first reported day."),
                ("Smoke outputs", f"{BUS_ANNUAL_SMOKE_PER_BLOCK_PATH.name}; {BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH.name}", "These files are smoke artifacts, not full-fleet production outputs."),
                ("Depot model", "depot_terminus abstraction", "No real depot assignment has been added yet."),
                ("Vehicle assignment", "one sampled EV spec per block", "The sampled bus model remains fixed for that block's feed-year."),
            ],
            columns=["label", "value", "treatment"],
        )
        extra_rows = [
            (
                "LSOA attribution rule",
                "home LSOA = mode of end_lsoa per block",
                "first-stop and last-stop equivalent given SOC continuity",
            ),
            (
                "Depot inventory",
                "synthesized from simulation",
                "one synthetic depot per block in its home LSOA; capacity = vehicle's depot_charge_kw",
            ),
            (
                "Charging-supply scope",
                "depot only",
                "public OCM stations not yet integrated (see next-steps doc section 1)",
            ),
            (
                "Utilization",
                "not modelled",
                "ceiling = depot_total_kw x 8760; queueing and utilization see next-steps doc section 2",
            ),
            (
                "Next steps",
                "docs/bus_charging_next_steps.md",
                "follow-up PRs scoped there",
            ),
        ]
        honest_labels = pd.concat(
            [honest_labels, pd.DataFrame(extra_rows, columns=honest_labels.columns)],
            ignore_index=True,
        )
        display(honest_labels)
        print(f"Notebook wall-clock seconds: {time.time() - NOTEBOOK_START:.2f}")
        """
    )
)


nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "pygments_lexer": "ipython3"},
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
nbf.write(nb, OUT_PATH)
print(f"Wrote {OUT_PATH}")
