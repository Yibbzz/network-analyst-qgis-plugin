"""
ArcGIS-style cost barriers on top of Valhalla's ``linear_cost_factors``.

Valhalla can factor the cost of roads given as lines that follow its graph (it edge-walks
them). These build such lines:

- **added-cost point barriers**: crossing the point costs ``added`` more (in cost units, which
  are about seconds). The road from the point to the next node, in each direction of travel,
  gets a factor sized so that driving that piece costs exactly ``added`` more.
- **scaled-cost polygon barriers**: every road inside the polygon costs ``factor`` times as
  much (> 1 avoids it; < 1 only works if the server's min_linear_cost_factor allows it).

The roads come from Valhalla's /expansion, whose lines are exact edge shapes. Each edge is sent
as its own line: joined edges don't always edge-walk (the expansion may step between hierarchy
levels), and Valhalla 3.9.1 fails the whole request on a line that doesn't. Network calls are injected (``expand``, ``cost``), so the geometry logic is testable
without a server.

Valhalla uses the factors to choose the path but reports cost and time without them: a barrier
that's not worth a detour changes nothing you can see.
"""

from typing import Callable, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
)

from ..utils.geom_utils import WGS84

# Valhalla's pred_edge_id for the edges the expansion starts on
INVALID_EDGE_ID = 2**46 - 1
# Valhalla drops matched pieces shorter than 1 m
MIN_PIECE_M = 1.0
# a factor so large it's a closure in all but name
MAX_FACTOR = 1e6

Coords = List[Tuple[float, float]]
# expand(lon/lat, meters) -> Valhalla /expansion features (GeoJSON dicts)
Expand = Callable[[Tuple[float, float], float], List[dict]]
# cost(from lon/lat, to lon/lat) -> the cost of the route between them, None if there's none
Cost = Callable[[Tuple[float, float], Tuple[float, float]], Optional[float]]


def _distance() -> QgsDistanceArea:
    da = QgsDistanceArea()
    da.setEllipsoid("WGS84")
    da.setSourceCrs(WGS84, QgsCoordinateTransformContext())
    return da


def line_length(coords: Sequence[Sequence[float]]) -> float:
    """Meters along lon/lat coordinates."""
    if len(coords) < 2:
        return 0.0
    return _distance().measureLine([QgsPointXY(c[0], c[1]) for c in coords])


def encode_polyline6(coords: Sequence[Sequence[float]]) -> str:
    """lon/lat coordinates as Valhalla's polyline6 (any further dimensions are ignored)."""
    encoded = []
    prev_lat, prev_lng = 0, 0
    for lng, lat, *_ in coords:
        lat, lng = round(lat * 1e6), round(lng * 1e6)
        for delta in (lat - prev_lat, lng - prev_lng):
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                encoded.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            encoded.append(chr(delta + 63))
        prev_lat, prev_lng = lat, lng

    return "".join(encoded)


def _coords(feature: dict) -> Coords:
    return [(c[0], c[1]) for c in feature["geometry"]["coordinates"]]


def point_barrier_lines(
    point: Tuple[float, float], added: float, expand: Expand, cost: Cost
) -> List[dict]:
    """
    ``linear_cost_factors`` entries making a crossing of ``point`` cost ``added`` more.

    The expansion from the point starts on the edge(s) it snaps to, one per direction of travel
    away from it, each from the point to the edge's end. Whoever passes the point in that
    direction drives all of that piece, so the factor is ``added`` over its cost, plus 1.
    """
    if added <= 0:
        return []
    lines = list()
    for feature in expand(point, 100.0):
        if feature["properties"]["pred_edge_id"] != INVALID_EDGE_ID:
            continue
        coords = _coords(feature)
        if line_length(coords) < MIN_PIECE_M:
            continue  # Valhalla would drop it; the point sits on a node
        piece_cost = cost(point, coords[-1])
        if not piece_cost or piece_cost <= 0:
            continue
        lines.append(
            {"shape": encode_polyline6(coords), "factor": min(1 + added / piece_cost, MAX_FACTOR)}
        )

    return lines


def _parts(geometry: QgsGeometry) -> List[Coords]:
    if geometry.isEmpty():
        return []
    if geometry.isMultipart():
        return [[(p.x(), p.y()) for p in part] for part in geometry.asMultiPolyline()]
    # a line; points (a road just touching the polygon) are dropped
    if geometry.type() == QgsWkbTypes.GeometryType.LineGeometry:
        return [[(p.x(), p.y()) for p in geometry.asPolyline()]]
    return []


def polygon_barrier_lines(polygon: QgsGeometry, factor: float, expand: Expand) -> List[dict]:
    """
    ``linear_cost_factors`` entries scaling the cost of every road inside ``polygon`` (WGS84)
    by ``factor``. The roads are an expansion from a point in the polygon reaching past its
    farthest vertex, clipped to the polygon; roads that can't be reached from that point (e.g.
    an island network) are missed.
    """
    if factor == 1 or polygon.isEmpty():
        return []
    seed = polygon.pointOnSurface().asPoint()
    da = _distance()
    reach = max(da.measureLine(seed, QgsPointXY(v.x(), v.y())) for v in polygon.vertices())
    features = expand((seed.x(), seed.y()), 1.2 * reach + 100.0)

    lines, seen = list(), set()
    for feature in features:
        coords = _coords(feature)
        if len(coords) < 2:
            continue
        line = QgsGeometry.fromPolylineXY([QgsPointXY(*c) for c in coords])
        if not line.intersects(polygon):
            continue
        for part in _parts(line.intersection(polygon)):
            if len(part) < 2 or line_length(part) < MIN_PIECE_M:
                continue
            # the clip may come back reversed; a one-way road only edge-walks the right way
            start = line.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(*part[0])))
            end = line.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(*part[-1])))
            if start > end:
                part = part[::-1]
            key = tuple((round(x, 7), round(y, 7)) for x, y in part)
            if key in seen:
                continue
            seen.add(key)
            lines.append({"shape": encode_polyline6(part), "factor": float(factor)})

    return lines


def drop_unmatched(lines: List[dict], check: Callable[[List[dict]], bool]) -> Tuple[List[dict], int]:
    """
    ``lines`` without the ones Valhalla can't edge-walk, and how many were dropped.

    Valhalla 3.9.1 fails the whole request on one such line (newer versions only warn), and
    which ones fail isn't predictable from their shape (e.g. a parking aisle that map matches
    but doesn't edge-walk here). ``check(lines)`` asks Valhalla itself; on a failure the lines
    are halved until the bad ones are found, so all-good lines cost one request.
    """
    if not lines or check(lines):
        return lines, 0
    if len(lines) == 1:
        return [], 1
    half = len(lines) // 2
    first, dropped_first = drop_unmatched(lines[:half], check)
    second, dropped_second = drop_unmatched(lines[half:], check)
    return first + second, dropped_first + dropped_second
