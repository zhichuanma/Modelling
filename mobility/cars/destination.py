"""Runtime Layer-1 destination sampling backed by a frozen parquet table."""

from __future__ import annotations

from pathlib import Path
import warnings

import numpy as np
import pandas as pd

from mobility.core.spatial import load_lsoa_centroids, od_distance_km

DEFAULT_TABLE_PATH = (
    Path(__file__).resolve().parents[3]
    / "Data"
    / "Charging_stations"
    / "OSM_POI_Labeling"
    / "destination_choice_table.parquet"
)


class DestinationSampler:
    def __init__(
        self,
        table_path: Path | None = None,
        centroids: pd.DataFrame | None = None,
    ):
        """Load and index the frozen Layer-1 destination choice parquet."""
        self._table_path = Path(table_path) if table_path is not None else DEFAULT_TABLE_PATH
        table = pd.read_parquet(
            self._table_path,
            columns=["origin_lsoa", "purpose", "dest_lsoa", "prob"],
        )

        self._index: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
        for (origin_lsoa, purpose), group in table.groupby(
            ["origin_lsoa", "purpose"],
            sort=False,
        ):
            dest_lsoas = group["dest_lsoa"].to_numpy(dtype=object)
            probs = group["prob"].to_numpy(dtype=np.float64)
            prob_sum = probs.sum()
            if prob_sum <= 0.0:
                continue
            if not np.isclose(prob_sum, 1.0):
                probs = probs / prob_sum
            self._index[(str(origin_lsoa), str(purpose))] = (dest_lsoas, probs)

        centroid_frame = load_lsoa_centroids() if centroids is None else centroids.copy()
        if "lsoa_code" in centroid_frame.columns:
            centroid_frame = centroid_frame.set_index("lsoa_code", drop=True)
        self._centroids = centroid_frame.loc[:, ["easting_m", "northing_m"]]
        self._warned_missing_keys: set[tuple[str, str]] = set()

    def sample_destination_lsoa(
        self,
        origin_lsoa: str,
        purpose: str,
        rng: np.random.Generator,
        home_lsoa: str,
    ) -> str:
        """Sample a Layer-1 destination LSOA for one trip purpose."""
        if purpose == "home":
            return home_lsoa

        key = (origin_lsoa, purpose)
        hit = self._index.get(key)
        if hit is None:
            if key not in self._warned_missing_keys:
                warnings.warn(
                    f"Missing Layer-1 destination probabilities for origin={origin_lsoa!r}, "
                    f"purpose={purpose!r}; falling back to home_lsoa={home_lsoa!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self._warned_missing_keys.add(key)
            return home_lsoa

        dest_lsoas, probs = hit
        return str(rng.choice(dest_lsoas, p=probs))

    def distance_km(self, a: str, b: str) -> float:
        """Return centroid-based OD distance in kilometers with 0.5 km intra-LSOA."""
        return float(od_distance_km(a, b, self._centroids, intra_km=0.5))
