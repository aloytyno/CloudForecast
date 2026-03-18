"""
FMI cloud cover grid for the map overlay.

Downloads TotalCloudCover as NetCDF from the FMI edited scandinavia forecast
(pal_skandinavia) via the direct download API:
  • Single HTTP request, ~5 MB
  • Regular lat/lon grid: 180 × 182 points at ~0.067° ≈ 7 km resolution
  • Covers 0–120 h at 1-hourly steps

Public API
----------
fetch_cloud_grid(hours_ahead) → GridForecast
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import numpy as np
import requests
from scipy.io import netcdf_file

DOWNLOAD_URL = "https://opendata.fmi.fi/download"
FINLAND_BBOX = "20,59,32,71"   # lon_min,lat_min,lon_max,lat_max (WGS-84)


@dataclass
class GridForecast:
    lats: np.ndarray        # 1D ascending float64, shape (n_lats,)
    lons: np.ndarray        # 1D ascending float64, shape (n_lons,)
    times: list[datetime]   # UTC, sorted ascending
    oktas: np.ndarray       # int8, shape (n_times, n_lats, n_lons), -1 = missing


def fetch_cloud_grid(hours_ahead: int = 120) -> GridForecast:
    """
    Download TotalCloudCover grid from FMI pal_skandinavia edited forecast.

    Args:
        hours_ahead: Forecast horizon in hours (default 120 = 5 days).

    Returns:
        GridForecast with regular lat/lon grid and oktas time-series.

    Raises:
        requests.HTTPError: On a non-2xx response.
        ValueError:         If the response is not valid NetCDF.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    response = requests.get(
        DOWNLOAD_URL,
        params={
            "producer":   "pal_skandinavia",
            "param":      "TotalCloudCover",
            "bbox":       FINLAND_BBOX,
            "starttime":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endtime":    (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "format":     "netcdf",
            "timestep":   "60",
            "projection": "epsg:4326",
        },
        timeout=120,
    )
    response.raise_for_status()

    if response.content[:3] != b"CDF":
        raise ValueError(
            f"Expected NetCDF response, got: {response.content[:120]!r}"
        )

    return _parse_netcdf(response.content)


# ── Internal ──────────────────────────────────────────────────────────────────

def _parse_netcdf(data: bytes) -> GridForecast:
    nc = netcdf_file(io.BytesIO(data), mmap=False)
    try:
        times = _parse_nc_times(nc.variables["time"])

        lats = nc.variables["lat"].data.astype(np.float64).copy()
        lons = nc.variables["lon"].data.astype(np.float64).copy()

        cloud_key = next(
            k for k in nc.variables
            if "cloud" in k.lower() or "fraction" in k.lower()
        )
        cloud_var = nc.variables[cloud_key]
        cloud = cloud_var.data.astype(np.float32).copy()
        fill_val = getattr(cloud_var, "_FillValue", None)
    finally:
        nc.close()

    # Ensure lats sorted ascending (FMI can store N→S)
    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1].copy()
        cloud = cloud[:, ::-1, :].copy()

    # Convert fraction 0–1 → oktas 0–8; mark fill / out-of-range as -1
    valid = (cloud >= 0.0) & (cloud <= 1.1)
    if fill_val is not None:
        valid &= cloud != float(fill_val)

    oktas = np.full(cloud.shape, -1, dtype=np.int8)
    oktas[valid] = np.clip(np.round(cloud[valid] * 8).astype(np.int8), 0, 8)

    return GridForecast(lats=lats, lons=lons, times=times, oktas=oktas)


def _parse_nc_times(time_var) -> list[datetime]:
    """Parse a NetCDF 'hours since YYYY-MM-DD HH:MM:SS' time axis."""
    units = time_var.units
    if isinstance(units, bytes):
        units = units.decode("utf-8")
    # Expected format: "hours since 2026-03-17 20:00:00"
    ref_str = units.split("since", 1)[1].strip()
    ref_dt = datetime.strptime(ref_str, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    return [ref_dt + timedelta(hours=float(h)) for h in time_var.data]
