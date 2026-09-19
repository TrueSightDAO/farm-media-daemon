import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_locations as loc


def _plots():
    # one small square around (-3.2963, -52.5832) (Rancho Maranta P1)
    return {
        "features": [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-52.5832, -3.2963],
                            [-52.5827, -3.2963],
                            [-52.5827, -3.2957],
                            [-52.5832, -3.2957],
                            [-52.5832, -3.2963],
                        ]
                    ],
                },
                "properties": {
                    "plot_id": "RM-P1",
                    "farm_id": "rancho-maranta",
                    "name": "Rancho Maranta Plot 1 (house)",
                },
            }
        ]
    }


def _trees():
    return {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-52.5830, -3.2960]},
                "properties": {"tree_id": "T1", "farm_id": "rancho-maranta"},
            }
        ]
    }


def test_haversine_zero_and_known():
    assert loc.haversine_m(-3.0, -52.0, -3.0, -52.0) == 0.0
    # ~111 km per degree of latitude
    d = loc.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert abs(d - 111195) < 200


def test_index_builds_from_both_layers():
    idx = loc.LocationIndex.from_geojson(_plots(), _trees())
    assert len(idx) == 2
    types = {p["type"] for p in idx.points}
    assert types == {"plot", "tree"}


def test_nearest_matches_plot_centroid():
    idx = loc.LocationIndex.from_geojson(_plots(), _trees())
    near = idx.nearest(-3.2960, -52.5830)
    assert near["nearest_location_ok"] is True
    assert near["nearest_distance_m"] < 100
    # the tree point (exact) should win over the plot centroid
    assert near["nearest_location_id"] == "T1"


def test_nearest_flags_far_point_unmatched():
    idx = loc.LocationIndex.from_geojson(_plots(), _trees())
    near = idx.nearest(-3.63, -53.65)  # ~180 km away (the Bom Sucesso trap)
    assert near["nearest_location_ok"] is False
    assert near["nearest_distance_m"] > 100000


def test_empty_index_returns_none():
    assert loc.LocationIndex().nearest(-3.0, -52.0) is None
    idx = loc.LocationIndex.from_geojson(None, None)
    assert idx.nearest(-3.0, -52.0) is None


def test_cache_roundtrip(tmp_path):
    idx = loc.LocationIndex.from_geojson(_plots(), _trees())
    p = os.path.join(str(tmp_path), "cache.json")
    idx.to_cache(p)
    again = loc.LocationIndex.from_cache(p)
    assert len(again) == len(idx)
    assert again.nearest(-3.2960, -52.5830)["nearest_location_id"] == "T1"


def test_load_default_index_missing_is_none(tmp_path):
    assert loc.load_default_index(os.path.join(str(tmp_path), "nope.json")) is None
