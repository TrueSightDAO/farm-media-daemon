#!/usr/bin/env python3
"""Nearest-known-location join for farm media GPS.

Answers "which parcel of land in our network was this clip filmed on?" for
every GPS-bearing media item in a farm manifest.

Location layers (all machine-generated, never hand-edited):

  * plots -- sunmint/plots/index.geojson  (one polygon per SunMint plot; the
             primary layer -- a named, farm-linked parcel)
  * trees -- sunmint/trees/index.geojson  (per-tree points; finer than plots
             and present for sites without a plot polygon yet)

The join is ANNOTATIVE and non-gating: it stamps nearest_location_* fields
onto an item and never blocks upload or publication. Distance is great-circle
(haversine) to the nearest plot centroid / tree point. With ~150 geometries a
brute-force scan is sub-millisecond, so no spatial index is needed.

The index is read from a local JSON cache (fast, offline). Refresh it from the
live geojson with ``python3 farm_media_locations.py refresh``. A missing or
unreachable index yields ``nearest() -> None`` -- callers never crash.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

EARTH_RADIUS_M = 6371000.0

DEFAULT_CACHE = os.environ.get(
    "FARM_MEDIA_LOCATIONS_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_cache.json"),
)

PLOTS_URL = (
    "https://raw.githubusercontent.com/TrueSightDAO/sunmint/main/plots/index.geojson"
)
TREES_URL = (
    "https://raw.githubusercontent.com/TrueSightDAO/sunmint/main/trees/index.geojson"
)

# A media point farther than this from every known location is "unmatched" --
# the signal that it may belong to a parcel we have not registered yet.
UNMATCHED_KM = 2.0


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two WGS84 points, in metres."""
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _vertices(coords):
    """Flatten any GeoJSON coordinate nesting to a list of [lon, lat] pairs."""
    out = []
    stack = [coords]
    while stack:
        c = stack.pop()
        if isinstance(c, list) and c and isinstance(c[0], (int, float)):
            out.append(c)
        elif isinstance(c, list):
            stack.extend(c)
    return out


def _centroid(coords):
    """Mean of a geometry's vertices -> (lat, lon), or None."""
    vs = _vertices(coords)
    if not vs:
        return None
    lon = sum(v[0] for v in vs) / len(vs)
    lat = sum(v[1] for v in vs) / len(vs)
    return lat, lon


class LocationIndex:
    """A flat list of candidate points (plot centroids + tree points)."""

    def __init__(self, points=None):
        # each point: {"lat","lon","id","name","type","farm_id"}
        self.points = list(points or [])

    def __len__(self):
        return len(self.points)

    @classmethod
    def from_geojson(cls, plots_doc=None, trees_doc=None):
        """Build candidates from the plots (polygon) + trees (point) layers."""
        pts = []
        for feat in (plots_doc or {}).get("features", []):
            geom = feat.get("geometry") or {}
            if geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            c = _centroid(geom.get("coordinates"))
            if not c:
                continue
            props = feat.get("properties") or {}
            pts.append(
                {
                    "lat": c[0],
                    "lon": c[1],
                    "id": props.get("plot_id"),
                    "name": props.get("name") or props.get("plot_id"),
                    "type": "plot",
                    "farm_id": props.get("farm_id"),
                }
            )
        for feat in (trees_doc or {}).get("features", []):
            geom = feat.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            props = feat.get("properties") or {}
            pts.append(
                {
                    "lat": coords[1],
                    "lon": coords[0],
                    "id": props.get("tree_id") or props.get("id"),
                    "name": props.get("tree_id") or props.get("id"),
                    "type": "tree",
                    "farm_id": props.get("farm_id"),
                }
            )
        return cls(pts)

    def nearest(self, lat, lon, flag_km=UNMATCHED_KM):
        """Nearest known location for (lat, lon), or None if the index is empty.

        Returns a flat dict of annotative fields: nearest_location_id / _name /
        _type / _farm_id / nearest_distance_m / nearest_location_ok.
        """
        if not self.points or lat is None or lon is None:
            return None
        best = None
        best_d = None
        for p in self.points:
            d = haversine_m(lat, lon, p["lat"], p["lon"])
            if best_d is None or d < best_d:
                best, best_d = p, d
        if best is None:
            return None
        return {
            "nearest_location_id": best["id"],
            "nearest_location_name": best["name"],
            "nearest_location_type": best["type"],
            "nearest_farm_id": best["farm_id"],
            "nearest_distance_m": round(best_d, 1),
            "nearest_location_ok": best_d <= flag_km * 1000.0,
        }

    @classmethod
    def from_cache(cls, path):
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh).get("points", []))

    def to_cache(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"points": self.points}, fh, indent=2, ensure_ascii=False)


def _fetch_json(url, timeout=30):
    req = urllib.request.Request(
        url, headers={"User-Agent": "farm-media-locations/0.1"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh(cache_path=DEFAULT_CACHE, plots_url=PLOTS_URL, trees_url=TREES_URL):
    """Fetch the live layers and (re)write the local cache. Returns the index."""
    plots_doc = trees_doc = None
    try:
        plots_doc = _fetch_json(plots_url)
    except Exception:
        pass
    try:
        trees_doc = _fetch_json(trees_url)
    except Exception:
        pass
    idx = LocationIndex.from_geojson(plots_doc, trees_doc)
    if len(idx):
        idx.to_cache(cache_path)
    return idx


def load_default_index(cache_path=DEFAULT_CACHE):
    """Load the cached index, or None if absent/unreadable. Never raises."""
    try:
        return LocationIndex.from_cache(cache_path)
    except Exception:
        return None


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("refresh", help="fetch live layers -> cache")
    r.add_argument("--cache", default=DEFAULT_CACHE)
    n = sub.add_parser("nearest", help="nearest known location for a point")
    n.add_argument("lat", type=float)
    n.add_argument("lon", type=float)
    n.add_argument("--cache", default=DEFAULT_CACHE)
    args = ap.parse_args(argv)

    if args.cmd == "refresh":
        idx = refresh(args.cache)
        print(f"wrote {args.cache}: {len(idx)} locations")
        return 0
    if args.cmd == "nearest":
        idx = load_default_index(args.cache)
        if idx is None:
            print("no locations cache -- run: refresh")
            return 1
        print(json.dumps(idx.nearest(args.lat, args.lon), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
