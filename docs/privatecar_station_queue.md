# Private Car Station Queue Model

This note documents the queue-aware public charging station layer for the
private-car charging pipeline.

## Pipeline Audit

The existing private-car station curve pipeline is:

1. `scripts/run_privatecar_charging_curves.py`
   calls `mobility.cars.station_curves.run_privatecar_station_curve_pipeline`.
2. `mobility/cars/station_curves.py`
   builds year schedules, matches public stations, simulates uncontrolled
   charging, and aggregates station curves.
3. `mobility/cars/station_matcher.py`
   assigns non-home parking events to a public station using LSOA candidates and
   Huff-style weights. It writes `ParkingEvent.matched_station_id` and
   `ParkingEvent.charge_power_kw`.
4. `mobility/core/simulator.py`
   simulates charging immediately whenever a `ParkingEvent` has
   `can_charge=True` and positive `charge_power_kw`.
5. `mobility/cars/station_curves.py::build_session_time_bins_for_ev`
   attributes the vehicle load profile back to public station sessions and
   `aggregate_station_curves_15min` writes the station-level 15-minute curve.

The always-available assumption is in `mobility/core/simulator.py`: each
chargeable parking event contributes `charge_power_kw * overlap` to the
vehicle's per-step charging power. There is no connector occupancy, waiting,
or station-capacity state in that SOC simulation. The queue layer added here is
a post-processing layer and does not change SOC equations, station matching,
destination choice, sampling, or vehicle assignment.

Current station input is `UK_OCM_stations_labeled.csv`, with station-level
`TotalCapacity_kW`. The queue layer now also looks for the local expanded
connector table `data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv`
when queueing is enabled. That file has one row per connector/device with
`StationID` and `Power_kW`; because it is already expanded, missing `Quantity`
means one connector per row. If the file is absent or an explicit connector
path is not supplied, the model falls back to documented synthetic connector
assumptions.

## Model

`mobility/cars/station_queue.py` implements a deterministic first-come,
first-served finite-connector queue per station.

Inputs:

- public charging events from `private_car_charging_events.parquet`;
- station metadata from `station_metadata_{year}.json` or the in-memory station
  metadata frame;
- optional connector table with `StationID`/`station_id`, `Power_kW`, and
  optional `Quantity`. The default local connector source is
  `data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv` when present.

Capacity rules:

- If a connector table is supplied, connector quantities and powers are summed
  by station. Expanded connector rows without `Quantity` are treated as one
  connector per row.
- Without connector rows, connector count is a configurable fallback. By
  default the model derives a synthetic count from station total capacity and
  `fallback_connector_power_kw=7.0`.
- Fallback capacity is explicitly labelled in `capacity_source`; it is an
  assumption, not observed connector inventory.

Scheduling rules:

- Sessions are ordered by `station_id`, arrival time, and session id.
- Each session requests the existing baseline public energy and power.
- The first available connector is assigned.
- Waiting time is the time between arrival and service start, or between
  arrival and parking-window closure for rejected sessions.
- By default service cannot continue beyond the original parking window. Energy
  that cannot be delivered before that window closes is reported as unmet.
- `queue_length_on_arrival` counts sessions waiting after this arrival joins the
  queue, so station `max_queue_length` captures the peak waiting queue.
- `station_utilization_rate` is connector occupied time divided by connector
  count times study period.

The current availability model is deterministic. Stochastic outages or
connector availability can be added later behind the same session/capacity
interfaces.

## New Outputs

When queueing is enabled, baseline files are left unchanged and additional
files are written:

| File | Purpose |
|---|---|
| `station_charging_curve_15min_queue_aware_{year}.parquet` / `.csv` | station-level 15-minute delivered load after queueing, with occupancy and waiting columns |
| `station_charging_curve_hourly_queue_aware_{year}.parquet` | hourly queue-aware delivered load |
| `private_car_public_charging_sessions_queue_aware.parquet` | session-level queue status, wait time, service time, unmet energy |
| `station_queue_capacity_{year}.parquet` | station connector count, connector power, station kW, and capacity source |
| `station_queue_summary_{year}.parquet` / `.csv` | station waiting, rejection, unmet-energy, max queue, and utilization metrics |
| `station_queue_comparison_{year}.parquet` / `.csv` | no-queue baseline versus queue-aware delivered energy and peak power |
| `queue_model_config_{year}.json` | queue model configuration and output manifest |
| `queue_model_report_{year}.md` | assumptions, provenance, and high-level metrics |

Important columns include:

- `waiting_time_min`
- `service_duration_min`
- `queue_length_on_arrival`
- `delivered_energy_after_queue_kwh`
- `unmet_energy_kwh`
- `rejected`
- `queued_session_count`
- `occupied_connector_count`
- `bin_utilization_rate`
- `station_utilization_rate`

## How To Run Small Validation

Unit tests use synthetic data only:

```bash
python -m pytest tests/mobility/cars/test_station_queue.py -q
```

To run the queue layer on an existing small private-car sample output directory:

```bash
python scripts/run_privatecar_station_queue.py \
  --input-dir outputs/privatecar_charging_curves_2025_sample \
  --output-dir outputs/privatecar_charging_curves_2025_sample \
  --year 2025

# By default this uses data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv
# when that file exists.
```

To generate baseline sample outputs and queue outputs in one pass, keep the
sample small and skip web JSON:

```bash
python scripts/run_privatecar_charging_curves.py \
  --max-vehicles 5 \
  --chunk-size 5 \
  --skip-web-json \
  --enable-queue-model \
  --output-dir outputs/privatecar_queue_smoke_2025
```

Do not use the sample outputs as national estimates.

## Limitations

- The queue layer does not feed delayed or unmet public charging back into SOC,
  later trip feasibility, destination choice, or charging rescheduling.
- Default connector counts are fallback assumptions unless a connector table is
  supplied.
- The model is deterministic FCFS; no random outages, broken connectors,
  payment/network access, plug-type compatibility, or driver abandonment model
  is active yet.
- Full national queue runs over all public charging events can be large. For
  production, prefer shard-aware or station-partitioned processing after review.

## Next Integration Steps

1. Review fallback connector assumptions against the available OCM connector
   tables.
2. Run the queue-only script on one small existing output directory and inspect
   `queue_model_report_{year}.md`.
3. For full private-car simulation, decide whether to queue each shard
   separately or merge public sessions by station first. True queueing across
   all vehicles at a station requires all sessions for that station in the same
   queue run.
4. Add infrastructure-gap experiments by comparing baseline demand,
   queue-aware unmet energy, p95 wait, and utilization under alternative
   connector-count or power scenarios.
