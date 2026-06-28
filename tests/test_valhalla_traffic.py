import io
import struct
import tarfile

from freetraffic.export import valhalla_traffic as vt
from freetraffic.models import LinkSpeed


def test_graphid_decode():
    # level=0, tileid=5, index=2  ->  value = (2<<25) | (5<<3)
    edge_id = (2 << 25) | (5 << 3)
    assert vt.decode_graphid(edge_id) == (0, 5, 2)
    assert vt.tile_key(edge_id) == (5 << 3)


def test_speed_pack_roundtrip():
    rec = vt.uniform_speed_record(60)
    assert len(rec) == 8
    d = vt.unpack_traffic_speed(rec)
    assert d["overall_encoded_speed"] == 30 and d["encoded_speed1"] == 30  # 60>>1
    assert d["breakpoint1"] == 255
    # encode clamps to known range
    assert vt.encode_speed(1000) == vt.MAX_ENCODED
    assert vt.encode_speed(-5) == vt.UNKNOWN_SPEED_RAW


def _tile_blob(tile_id: int, edge_count: int, version: int = 3) -> bytes:
    header = struct.pack("<QQIIII", tile_id, 0, edge_count, version, 0, 0)
    # a fresh Valhalla extract initializes every record to "unknown"
    return header + vt.unknown_speed_record() * edge_count


def _make_traffic_tar(path):
    tiles = {
        (5 << 3): ("0/000/005.gph", 3),   # tile A: 3 edges
        (6 << 3): ("0/000/006.gph", 2),   # tile B: 2 edges
    }
    with tarfile.open(path, "w") as tar:
        for tile_id, (name, count) in tiles.items():
            blob = _tile_blob(tile_id, count)
            info = tarfile.TarInfo(name=name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))


def test_traffic_tar_updater_roundtrip(tmp_path):
    tar_path = str(tmp_path / "traffic.tar")
    _make_traffic_tar(tar_path)

    edge_a2 = (2 << 25) | (5 << 3)   # tile A, index 2
    edge_b0 = (0 << 25) | (6 << 3)   # tile B, index 0
    edge_oob = (5 << 25) | (5 << 3)  # tile A, index 5 -> out of range
    edge_unknown_tile = (0 << 25) | (99 << 3)  # tile 99 not in extract

    with vt.TrafficTarUpdater(tar_path) as up:
        assert up.tile_count == 2
        written = up.set_speeds({
            edge_a2: 50.0,
            edge_b0: 100.0,
            edge_oob: 30.0,
            edge_unknown_tile: 30.0,
        })
        assert written == 2 and up.skipped == 2

    # reopen and verify persistence + that untouched edges remain unknown
    with vt.TrafficTarUpdater(tar_path) as up:
        assert up.read_speed_kph(edge_a2) == 50
        assert up.read_speed_kph(edge_b0) == 100
        untouched = (0 << 25) | (5 << 3)  # tile A index 0, never written
        assert up.read_speed_kph(untouched) is None


def test_apply_link_speeds(tmp_path):
    tar_path = str(tmp_path / "traffic.tar")
    _make_traffic_tar(tar_path)
    edge_b1 = (1 << 25) | (6 << 3)
    with vt.TrafficTarUpdater(tar_path) as up:
        n = up.apply_link_speeds([
            LinkSpeed(source_id="gtfs", speed_kph=40, link_id=str(edge_b1)),
            LinkSpeed(source_id="x", speed_kph=10, link_id="not-an-edge"),
        ])
        assert n == 1
        assert up.read_speed_kph(edge_b1) == 40
