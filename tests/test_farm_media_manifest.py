import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_manifest as m


def test_parse_gps_decimal():
    assert m._parse_gps("-3.4146, -52.6285") == (-3.4146, -52.6285, "-3.4146, -52.6285")


def test_parse_gps_dms_string():
    """The bug: exiftool DMS output must resolve to decimal, not None."""
    lat, lon, raw = m._parse_gps(
        '3 deg 33\' 25.20" S, 51 deg 6\' 13.32" W, 108.156 m Above Sea Level'
    )
    assert raw is not None
    assert lat is not None and lon is not None
    assert abs(lat - (-3.5570)) < 0.001
    assert abs(lon - (-51.1037)) < 0.001


def test_parse_gps_matches_geo_parser():
    """Manifest parsing must agree with the daemon's own geo parser."""
    import farm_media_geo as g

    s = '3 deg 24\' 56.52" S, 52 deg 36\' 54.00" W'
    assert m._parse_gps(s) == g.parse_gps(s)


def test_parse_gps_none_and_junk():
    assert m._parse_gps(None) == (None, None, None)
    assert m._parse_gps("") == (None, None, None)
    lat, lon, raw = m._parse_gps("no gps here")
    assert lat is None and lon is None and raw == "no gps here"
