"""
FMI WFS client for fetching TotalCloudCover forecasts.

Short-range  (0–48 h)  : HARMONIE surface point, 1 h timestep.
Extended     (48–120 h): ECMWF surface point, 3 h timestep.

The public API is get_cloud_cover_forecast(), which transparently blends
both sources and returns a single sorted list of (timestamp, cloud_octas).
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

WFS_URL = "https://opendata.fmi.fi/wfs"

_QUERIES = {
    # High-resolution NWP, ~48 h at 1 h steps.
    "harmonie":  "fmi::forecast::harmonie::surface::point::timevaluepair",
    # FMI edited/blended Scandinavia product, valid ~7 days at 3 h steps.
    # ecmwf::forecast::surface::point::timevaluepair also exists but its
    # TotalCloudCover field goes NaN after ~48 h, making it unusable here.
    "scandinavia": "fmi::forecast::edited::weather::scandinavia::point::timevaluepair",
}

NS_WML2 = "http://www.opengis.net/waterml/2.0"

# HARMONIE runs to ~54 h; hand off to ECMWF after this many hours.
_HARMONIE_HORIZON_H = 48


def get_cloud_cover_forecast(
    lat: float,
    lon: float,
    hours_ahead: int = 120,
    short_timestep_minutes: int = 60,
    long_timestep_minutes: int = 180,
) -> list[tuple[datetime, int]]:
    """
    Fetch TotalCloudCover forecast for a location in Finland.

    Blends HARMONIE (hours 0–48, 1 h steps) with ECMWF (hours 48–N, 3 h
    steps) so the result has high detail near-term and extended medium-range
    coverage beyond two days.

    Args:
        lat: Latitude (WGS84).
        lon: Longitude (WGS84).
        hours_ahead: Total forecast horizon in hours (default 120 = 5 days).
            Pass ≤48 to use HARMONIE only.
        short_timestep_minutes: Timestep for HARMONIE segment (default 60).
        long_timestep_minutes:  Timestep for ECMWF segment (default 180).

    Returns:
        List of (timestamp, cloud_octas) tuples, sorted ascending by time.
        cloud_octas is an integer 0–8 (oktas), or -1 for missing values.

    Raises:
        requests.HTTPError: On a non-2xx API response.
        ValueError: If the XML response cannot be parsed.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    points: dict[datetime, int] = {}

    # ── Short-range: HARMONIE ─────────────────────────────────────────────
    harmonie_end_h = min(hours_ahead, _HARMONIE_HORIZON_H)
    harmonie_data = _fetch(
        lat, lon,
        start=now,
        end=now + timedelta(hours=harmonie_end_h),
        timestep=short_timestep_minutes,
        model="harmonie",
    )
    for ts, oktas in harmonie_data:
        points[ts] = oktas

    # ── Extended-range: ECMWF ────────────────────────────────────────────
    # Fetch ECMWF from `now` rather than from the handoff boundary so that
    # the data used after hour 48 comes from the middle of the ECMWF run,
    # not its first timestep (which can have a spin-up artefact of 0 %).
    if hours_ahead > _HARMONIE_HORIZON_H:
        ext_data = _fetch(
            lat, lon,
            start=now,
            end=now + timedelta(hours=hours_ahead),
            timestep=long_timestep_minutes,
            model="scandinavia",
        )
        for ts, oktas in ext_data:
            if ts not in points:          # HARMONIE takes precedence for 0–48 h
                points[ts] = oktas

    return sorted(points.items())


# ── Internal helpers ──────────────────────────────────────────────────────

def _fetch(
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    timestep: int,
    model: str,
) -> list[tuple[datetime, int]]:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "storedquery_id": _QUERIES[model],
        "latlon": f"{lat},{lon}",
        "parameters": "TotalCloudCover",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endtime":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestep":  str(timestep),
    }
    response = requests.get(WFS_URL, params=params, timeout=30)
    response.raise_for_status()
    return _parse_timevaluepair(response.text)


def _parse_timevaluepair(xml_text: str) -> list[tuple[datetime, int]]:
    """Parse a WFS timevaluepair XML response into (datetime, oktas) tuples."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse WFS response XML: {exc}") from exc

    results: list[tuple[datetime, int]] = []

    for tvp in root.iter(f"{{{NS_WML2}}}MeasurementTVP"):
        time_el  = tvp.find(f"{{{NS_WML2}}}time")
        value_el = tvp.find(f"{{{NS_WML2}}}value")

        if time_el is None or value_el is None:
            continue

        timestamp = _parse_iso8601(time_el.text.strip())

        raw = value_el.text.strip() if value_el.text else None
        try:
            # FMI returns TotalCloudCover as percentage (0–100).
            # Convert to oktas: 1 okta = 12.5 %.
            pct = float(raw) if raw not in (None, "NaN", "") else None
            octas = round(pct / 12.5) if pct is not None else -1
        except ValueError:
            octas = -1

        results.append((timestamp, octas))

    return results


def _parse_iso8601(s: str) -> datetime:
    s = s.rstrip("Z")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
