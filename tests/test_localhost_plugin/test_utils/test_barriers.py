import unittest

from tests.utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

from qgis.core import QgsGeometry  # noqa: E402

from valhalla.core import barriers  # noqa: E402
from valhalla.core.barriers import INVALID_EDGE_ID  # noqa: E402

# ~11 m per 0.0001 degrees of latitude
STEP = 0.0001


def feature(edge_id, pred, *coords):
    return {
        "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
        "properties": {"edge_id": edge_id, "pred_edge_id": pred},
    }


def decode(shape):
    idx = lat = lng = 0
    out = []
    while idx < len(shape):
        vals = []
        for _ in (0, 1):
            shift = res = 0
            while True:
                b = ord(shape[idx]) - 63
                idx += 1
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lng += vals[1]
        out.append((round(lng / 1e6, 6), round(lat / 1e6, 6)))
    return out


class TestPointBarriers(unittest.TestCase):
    """A road along the meridian through the barrier at (0, 0), no valhalla."""

    def expand(self, point, meters):
        return [
            feature(1, INVALID_EDGE_ID, (0, 0), (0, STEP)),  # 11 m north to a node
            feature(2, 1, (0, STEP), (0, 4 * STEP)),  # beyond it: not the barrier's road
            feature(5, INVALID_EDGE_ID, (0, 0), (0, -5 * STEP)),  # 55 m south
            feature(6, INVALID_EDGE_ID, (0, 0), (0, 0.000001)),  # 11 cm: Valhalla would drop it
        ]

    def test_lines(self):
        priced = []

        def cost(a, b):
            priced.append(b)
            return 4.0

        lines = barriers.point_barrier_lines((0, 0), 300, self.expand, cost)
        # one line per direction, each just the piece from the point to the next node
        self.assertEqual(
            [decode(line["shape"]) for line in lines], [[(0, 0), (0, STEP)], [(0, 0), (0, -5 * STEP)]]
        )
        # driving a piece that costs 4 must cost 300 more: 4 * 76 = 304
        self.assertEqual([line["factor"] for line in lines], [76.0, 76.0])
        self.assertEqual(priced, [(0, STEP), (0, -5 * STEP)])

    def test_nothing_to_add(self):
        self.assertEqual(barriers.point_barrier_lines((0, 0), 0, self.expand, lambda a, b: 4.0), [])
        # a piece that can't be priced is left out
        self.assertEqual(barriers.point_barrier_lines((0, 0), 300, self.expand, lambda a, b: None), [])


class TestPolygonBarriers(unittest.TestCase):
    SQUARE = QgsGeometry.fromWkt("POLYGON((0 0, 0.001 0, 0.001 0.001, 0 0.001, 0 0))")

    def test_clipped(self):
        seeds = []

        def expand(point, meters):
            seeds.append((point, meters))
            return [
                feature(1, 0, (-0.001, 0.0005), (0.002, 0.0005)),  # crosses west to east
                feature(2, 0, (0.0005, 0.002), (0.0005, -0.001)),  # crosses north to south
                feature(3, 0, (0.003, 0.003), (0.004, 0.003)),  # outside
                feature(4, 0, (-0.001, 0.0005), (0.002, 0.0005)),  # the same road again
            ]

        lines = barriers.polygon_barrier_lines(self.SQUARE, 5, expand)
        self.assertEqual(len(lines), 2)
        shapes = [decode(line["shape"]) for line in lines]
        self.assertEqual(shapes[0], [(0, 0.0005), (0.001, 0.0005)])
        # still north to south: a one-way road only edge-walks in its own direction
        self.assertEqual(shapes[1], [(0.0005, 0.001), (0.0005, 0)])
        self.assertEqual({line["factor"] for line in lines}, {5.0})
        # one expansion, from inside the polygon, reaching past its corners (~79 m away)
        ((seed, meters),) = seeds
        self.assertTrue(self.SQUARE.contains(QgsGeometry.fromWkt(f"POINT({seed[0]} {seed[1]})")))
        self.assertGreater(meters, 79)

    def test_factor_one(self):
        self.assertEqual(barriers.polygon_barrier_lines(self.SQUARE, 1, lambda p, m: []), [])


class TestDropUnmatched(unittest.TestCase):
    def test_bisects(self):
        lines = [{"shape": str(i)} for i in range(10)]
        bad = {"3", "7"}
        checks = []

        def check(ls):
            checks.append(len(ls))
            return not any(line["shape"] in bad for line in ls)

        kept, dropped = barriers.drop_unmatched(lines, check)
        self.assertEqual(
            [line["shape"] for line in kept], [str(i) for i in range(10) if str(i) not in bad]
        )
        self.assertEqual(dropped, 2)
        self.assertLess(len(checks), 20)

    def test_all_good_is_one_check(self):
        checks = []
        kept, dropped = barriers.drop_unmatched(
            [{"shape": "a"}, {"shape": "b"}], lambda ls: checks.append(1) or True
        )
        self.assertEqual((len(kept), dropped, len(checks)), (2, 0, 1))
        self.assertEqual(barriers.drop_unmatched([], lambda ls: 1 / 0), ([], 0))
