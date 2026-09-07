"""Reverse-geocode farm media GPS -> a human place name for descriptions.

Reads GOOGLE_MAPS_API_KEY from the environment (or .env next to the config /
in CWD via a tiny dotenv shim). Parses both GPS formats seen in farm-media
sidecars:

  * decimal:      "-3.4894, -52.9667"
  * DMS string:   "3 deg 24' 56.52\" S, 52 deg 36' 54.00\" W"

Reverse-geocodes with the Google Geocoding API, dedupes by rounded
coordinates (~farms share points, so 312 videos -> ~10-60 lookups), and
returns a compact place label.  Never raises when the key is missing or the
API fails: callers get None and fall back to a no-place description.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# tiny dotenv shim (no external dep): load KEY=VAL lines from .env files
# ---------------------------------------------------------------------------


def _load_dotenv_files() -> None:
    for p in (Path.cwd() / ".env", Path("/opt/truesight_autopilot/.env")):
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except OSError:
            continue


_load_dotenv_files()


def api_key() -> str | None:
    k = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_PLACES_API_KEY")
    return k if k and k.startswith("AIza") else None


# ---------------------------------------------------------------------------
# GPS parsing
# ---------------------------------------------------------------------------

_DMS_RE = re.compile(
    r"(?P<deg>\d+(?:\.\d+)?)\s*(?:deg|°|d)\s*"
    r"(?:'|′|min|m)?\s*"
    r"(?P<min>\d+(?:\.\d+)?)?\s*(?:'|′|″|\")?\s*"
    r"(?P<sec>\d+(?:\.\d+)?)?\s*(?:'|′|″|\")?\s*"
    r"(?P<hem>[NSEWnsew])",
)


def parse_gps(gps: object) -> tuple[float | None, float | None, str | None]:
    """Return (lat, lon, raw) for a sidecar gps value.

    Supports '-3.4894, -52.9667', '3 deg 24' 56.52" S, ...' and similar.
    Returns (None, None, raw) when unparseable so callers can still record
    the raw string in the manifest.
    """
    if gps is None:
        return None, None, None
    raw = str(gps).strip()
    if not raw:
        return None, None, None

    # decimal form: "lat, lon"
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$", raw)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, raw

    # DMS form: "<deg> <min>' <sec>\" H, <deg> <min>' <sec>\" H"
    parts = [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]
    if len(parts) >= 2:
        vals = []
        ok = True
        for part in parts[:2]:
            mm = _DMS_RE.search(part)
            if not mm:
                ok = False
                break
            deg = float(mm.group("deg"))
            minute = float(mm.group("min") or 0)
            sec = float(mm.group("sec") or 0)
            val = deg + minute / 60.0 + sec / 3600.0
            if mm.group("hem").upper() in ("S", "W"):
                val = -val
            vals.append(val)
        if ok and len(vals) == 2:
            lat, lon = vals
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon, raw

    return None, None, raw


# ---------------------------------------------------------------------------
# Geocoding (deduped)
# ---------------------------------------------------------------------------


class GeoCache:
    """Dedupes reverse-geocode calls by rounded coordinates + keeps results
    on disk (json) so re-runs cost nothing."""

    def __init__(self, path: str | Path | None = None, key: str | None = None):
        self.path = Path(path) if path else None
        self.key = key or api_key()
        self._mem: dict[tuple, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    lat_s, lon_s = k.split(",")
                    self._mem[(float(lat_s), float(lon_s))] = v
            except (OSError, ValueError):
                self._mem = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {f"{lat},{lon}": v for (lat, lon), v in self._mem.items()}
            self.path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _round(lat: float, lon: float) -> tuple[float, float]:
        # ~1.1 km at the equator per 0.01 deg; plenty for a "which town" label
        return round(lat, 2), round(lon, 2)

    def reverse(self, lat: float, lon: float) -> dict | None:
        """Return cached/fetched geocode result or None.  Never raises."""
        key = self._round(lat, lon)
        if key in self._mem:
            return self._mem[key]
        if not self.key:
            return None
        try:
            url = (
                "https://maps.googleapis.com/maps/api/geocode/json?"
                + urllib.parse.urlencode({"latlng": f"{lat},{lon}", "key": self.key})
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "farm-media-geo/0.1"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") != "OK":
                return None
            res = data.get("results") or []
            if not res:
                return None
            out = self._pick(res[0])
            self._mem[key] = out
            self._save()
            time.sleep(0.05)  # gentle pacing
            return out
        except Exception:
            return None

    @staticmethod
    def _pick(r: dict) -> dict:
        comps = {c["types"][0]: c["long_name"] for c in r.get("address_components", [])}
        locality = (
            comps.get("locality")
            or comps.get("sublocality_level_1")
            or comps.get("sublocality")
            or comps.get("administrative_area_level_2")
        )
        region = comps.get("administrative_area_level_1")
        country = comps.get("country")
        return {
            "place_name": locality,
            "region": region,
            "country": country,
            "formatted_address": r.get("formatted_address"),
            "place_id": r.get("place_id"),
        }


def place_label(lat: float, lon: float, cache: GeoCache | None = None) -> str | None:
    """Return a 'Place, Region' label (or just 'Place') or None."""
    cache = cache or GeoCache()
    res = cache.reverse(lat, lon)
    if not res or not res.get("place_name"):
        return None
    if res.get("region") and res["region"] != res["place_name"]:
        return f"{res['place_name']}, {res['region']}"
    return res["place_name"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--cache", default=None, help="json cache path")
    args = p.parse_args()
    cache = GeoCache(path=args.cache)
    res = cache.reverse(args.lat, args.lon)
    if not res:
        print("no result (key missing or lookup failed)")
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
