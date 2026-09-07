import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_geo as g


def test_parse_gps_decimal():
    assert g.parse_gps("-3.4894, -52.9667") == (-3.4894, -52.9667, "-3.4894, -52.9667")


def test_parse_gps_dms():
    lat, lon, raw = g.parse_gps("3 deg 24' 56.52\" S, 52 deg 36' 54.00\" W")
    assert raw is not None
    assert abs(lat - (-3.4157)) < 0.001
    assert abs(lon - (-52.615)) < 0.001


def test_parse_gps_none_and_junk():
    assert g.parse_gps(None) == (None, None, None)
    assert g.parse_gps("") == (None, None, None)
    lat, lon, raw = g.parse_gps("no gps here")
    assert lat is None and lon is None and raw == "no gps here"


def test_parse_gps_out_of_range():
    assert g.parse_gps("99.0, 12.0")[0] is None
    assert g.parse_gps("12.0, 199.0")[1] is None


class _FakeCache:
    """Dedupes by rounded coords, returns canned results, no network."""

    def __init__(self):
        self.calls = []

    def reverse(self, lat, lon):
        self.calls.append((lat, lon))
        r = round(lat, 2), round(lon, 2)
        if r == (-3.49, -52.97):
            return {
                "place_name": "Medicilândia",
                "region": "Pará",
                "country": "Brazil",
                "formatted_address": "G26M+88 - Medicilândia, PA",
                "place_id": "ChIJfake",
            }
        return None


def test_place_label_with_region():
    c = _FakeCache()
    label = g.place_label(-3.4894, -52.9667, cache=c)
    assert label == "Medicilândia, Pará"


def test_place_label_none_when_unresolved():
    c = _FakeCache()
    assert g.place_label(-10.0, -40.0, cache=c) is None


def test_round():
    assert g.GeoCache._round(-3.4894, -52.9667) == (-3.49, -52.97)
