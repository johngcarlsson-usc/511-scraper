import math

from freetraffic.export import osrm, valhalla


def test_osrm_segment_speed_file():
    rows = [(10, 11, 48.4), (11, 12, 0)]
    text = osrm.format_segment_speed_file(rows)
    assert text == "10,11,48\n11,12,0\n"


def test_osrm_with_weight_and_closures():
    text = osrm.format_segment_speed_file([(1, 2, 30.0, 99.5)])
    assert text == "1,2,30,99.5\n"
    closed = list(osrm.closure_rows([(5, 6), (6, 7)]))
    assert closed == [(5, 6, 0), (6, 7, 0)]


def test_valhalla_predicted_csv_minimal():
    text = valhalla.format_predicted_csv([(123, 105.0, 80.0)])
    assert text == "123,105,80,\n"


def test_valhalla_predicted_csv_with_string_historical():
    text = valhalla.format_predicted_csv([(7, 100, 70, "AAAB")])
    assert text == "7,100,70,AAAB\n"


def test_valhalla_live_speed_csv():
    assert valhalla.format_live_speed_csv([(42, 55.6)]) == "42,56\n"


def test_historical_codec_roundtrips():
    # a synthetic weekly profile: faster at night, slower at rush hour
    speeds = []
    for bucket in range(valhalla.BUCKETS_PER_WEEK):
        minute_of_day = (bucket % 288) * 5
        rush = 25 if (420 <= minute_of_day <= 540 or 990 <= minute_of_day <= 1110) else 0
        speeds.append(100.0 - rush)
    encoded = valhalla.encode_historical_speeds(speeds)
    decoded = valhalla.decode_historical_speeds(encoded)
    assert len(decoded) == valhalla.BUCKETS_PER_WEEK
    # average reconstruction error should be small
    err = sum(abs(a - b) for a, b in zip(speeds, decoded)) / len(speeds)
    assert err < 5.0


def test_historical_codec_validates_length():
    try:
        valhalla.encode_historical_speeds([1, 2, 3])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for wrong bucket count")
