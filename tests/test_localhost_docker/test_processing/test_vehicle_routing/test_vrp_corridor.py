"""
VRP features with exact numbers, on a made-up network (tests/data/vrp_corridor, served on port
8003 by its own Valhalla): Main Road runs 20 km west -> east at 70.588 s/km, Side Road 5 km north
from Main Road's km 10 at 225 s/km. ``m(x)`` is the point x km along Main Road, ``s(y)`` y km up
Side Road. Travel times and distances are known to the second and the meter, so the limits and
costs can be tested right at their thresholds.
"""

import json
import socket
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingOutputLayerDefinition,
    QgsProcessingUtils,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

from valhalla.core import pypi
from valhalla.core.results_factory import ResultsFactory
from valhalla.global_definitions import RouterMethod, RouterProfile, RouterType
from valhalla.processing.vehicle_routing.vrp import ValhallaVrpCar
from valhalla.processing.vehicle_routing.waste import ValhallaWasteTruck

from .... import LocalhostDockerTestCase
from ....utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

URL_CORRIDOR = "http://localhost:8003"
KM = 1 / 111.319490793  # degrees per km at the equator
MAIN = 70.588  # seconds per km on Main Road
SIDE = 225.0  # seconds per km on Side Road


def m(x: float):
    return 10 + x * KM, 0.0


def s(y: float):
    return 10 + 10 * KM, y * KM


def on_corridor(alg_cls):
    """The algorithm, requesting the corridor's Valhalla with its default costing options."""

    class OnCorridor(alg_cls):
        def get_base_params(self, parameters, context):
            jobs, id_field, _, _ = super().get_base_params(parameters, context)
            factory = ResultsFactory(
                RouterType.VALHALLA, RouterMethod.REMOTE, self.profile, URL_CORRIDOR
            )
            return jobs, id_field, {}, factory

    return OnCorridor


def points(name: str, rows: List[dict]) -> QgsVectorLayer:
    """A point layer: each row is {"xy": (lon, lat), field: value, ...}; fields from the first."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    keys = [k for k in rows[0] if k != "xy"]
    types = {k: QVariant.String if isinstance(rows[0][k], str) else QVariant.Int for k in keys}
    layer.dataProvider().addAttributes([QgsField(k, types[k]) for k in keys])
    layer.updateFields()
    for row in rows:
        feat = QgsFeature(layer.fields())
        feat.setAttributes([row.get(k) for k in keys])
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*row["xy"])))
        layer.dataProvider().addFeature(feat)
    return layer


def jobs(*xys, **fields) -> QgsVectorLayer:
    return points("jobs", [{"xy": xy, "name": f"j{i}", **fields} for i, xy in enumerate(xys)])


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.warnings: List[str] = list()

    def reportError(self, error, fatalError=False):
        self.warnings.append(error)


class TestVrpCorridor(LocalhostDockerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        host = urlparse(URL_CORRIDOR)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((host.hostname, host.port)) != 0:
                raise ConnectionRefusedError(
                    f"The test network's Valhalla must run on {URL_CORRIDOR}, see tests/data/vrp_corridor"
                )
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def run_alg(self, params: dict, alg_cls=ValhallaVrpCar) -> Dict[str, List[QgsFeature]]:
        alg = on_corridor(alg_cls)()
        alg.initAlgorithm({})
        params = {"INPUT_FIELD_1": "name", "INPUT_VEHICLE_ID": "name", **params}
        for out in (alg.OUT, alg.OUT_STOPS, alg.OUT_UNASSIGNED):
            params[out] = QgsProcessingOutputLayerDefinition("TEMPORARY_OUTPUT")
        ctx, feedback = QgsProcessingContext(), _Feedback()
        self.assertTrue(alg.prepareAlgorithm(params, ctx, feedback))
        result = alg.processAlgorithm(params, ctx, feedback)
        layers = {
            key: list(QgsProcessingUtils.mapLayerFromString(result[out], ctx).getFeatures())
            for key, out in (
                ("routes", alg.OUT),
                ("stops", alg.OUT_STOPS),
                ("unassigned", alg.OUT_UNASSIGNED),
            )
        }
        layers["warnings"] = feedback.warnings
        return layers

    @staticmethod
    def served(out) -> Dict[str, Optional[str]]:
        """job name -> its vehicle (None = unassigned)"""
        result = {f["name"]: None for f in out["unassigned"]}
        result.update({f["name"]: f["vehicle_id"] for f in out["stops"] if f["type"] == "job"})
        return result

    @staticmethod
    def route(out, vehicle: str) -> QgsFeature:
        return next(r for r in out["routes"] if r["vehicle_id"] == vehicle)

    def test_network(self):
        """The network these tests count on: if Valhalla's speeds change, start here."""

        def trip(a, b):
            body = {
                "locations": [{"lon": a[0], "lat": a[1]}, {"lon": b[0], "lat": b[1]}],
                "costing": "auto",
            }
            req = urllib.request.Request(
                f"{URL_CORRIDOR}/route", json.dumps(body).encode(), {"Content-Type": "application/json"}
            )
            summary = json.load(urllib.request.urlopen(req))["trip"]["summary"]
            return summary["time"], summary["length"]

        for a, b, seconds, km in (
            (m(0), m(10), 10 * MAIN, 10),
            (m(3), m(17), 14 * MAIN, 14),
            (m(10), s(5), 5 * SIDE, 5),
        ):
            time, length = trip(a, b)
            self.assertAlmostEqual(time, seconds, delta=1)
            self.assertAlmostEqual(length, km, delta=0.002)

    # ------------------------------------------------------------------ end points
    def test_end_point(self):
        ends = points("ends", [{"xy": m(20), "vehicle": "v"}])
        for return_to_start in (True, False):  # an end point wins either way
            out = self.run_alg(
                {
                    "INPUT_LAYER_1": jobs(m(15)),
                    "INPUT_VEHICLES": points("vehicles", [{"xy": m(0), "name": "v"}]),
                    "INPUT_VEHICLE_ENDS": ends,
                    "INPUT_VEHICLE_END_ID": "vehicle",
                    "INPUT_RETURN_TO_START": return_to_start,
                }
            )
            steps = sorted(out["stops"], key=lambda f: f["sequence"])
            self.assertEqual([f["type"] for f in steps], ["start", "job", "end"])
            end = steps[-1]
            self.assertAlmostEqual(end.geometry().asPoint().x(), m(20)[0], places=6)
            self.assertAlmostEqual(end["arrival"], 20 * MAIN, delta=2)
            line = self.route(out, "v").geometry()
            self.assertAlmostEqual(
                line.vertexAt(line.constGet().nCoordinates() - 1).x(), m(20)[0], places=4
            )

    def test_end_point_choice(self):
        """km 15 is on v's way to its end at km 20 (20 km); w would go there and back (30 km)."""
        out = self.run_alg(
            {
                "INPUT_LAYER_1": jobs(m(15)),
                "INPUT_VEHICLES": points(
                    "vehicles", [{"xy": m(0), "name": "v"}, {"xy": m(0), "name": "w"}]
                ),
                "INPUT_VEHICLE_ENDS": points("ends", [{"xy": m(20), "vehicle": "v"}]),
                "INPUT_VEHICLE_END_ID": "vehicle",
            }
        )
        self.assertEqual(self.served(out), {"j0": "v"})

    def test_end_point_errors(self):
        vehicles = points("vehicles", [{"xy": m(0), "name": "v"}])
        for ends, field, message in (
            (points("ends", [{"xy": m(20), "vehicle": "x"}]), "vehicle", "no such vehicle"),
            (
                points("ends", [{"xy": m(20), "vehicle": "v"}, {"xy": m(5), "vehicle": "v"}]),
                "vehicle",
                "more than one",
            ),
            (points("ends", [{"xy": m(20), "vehicle": "v"}]), None, "vehicle ID"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(QgsProcessingException, message):
                self.run_alg(
                    {
                        "INPUT_LAYER_1": jobs(m(10)),
                        "INPUT_VEHICLES": vehicles,
                        "INPUT_VEHICLE_ENDS": ends,
                        "INPUT_VEHICLE_END_ID": field,
                    }
                )

    # ------------------------------------------------------------------ costs
    def two_vans(self, a, b, job, **costs) -> dict:
        """Van a and van b, one job; costs: field -> (a's value, b's value)."""
        rows = [{"xy": a, "name": "a"}, {"xy": b, "name": "b"}]
        params = {}
        for param, (field, values) in costs.items():
            for row, value in zip(rows, values):
                row[field] = value
            params[param] = field
        return self.run_alg(
            {"INPUT_LAYER_1": jobs(job), "INPUT_VEHICLES": points("vehicles", rows), **params}
        )

    def test_fixed_cost(self):
        # b is right next to the job...
        self.assertEqual(self.served(self.two_vans(m(0), m(10), m(11)))["j0"], "b")
        # ...but costs 100000 to use: a drives 22 km instead
        out = self.two_vans(m(0), m(10), m(11), INPUT_VEHICLE_FIXED_COST=("fixed", (0, 100_000)))
        self.assertEqual(self.served(out)["j0"], "a")
        route = self.route(out, "a")
        self.assertAlmostEqual(route["cost"], route["duration"], delta=1)  # = travel seconds

    def test_cost_per_km(self):
        self.assertEqual(self.served(self.two_vans(m(0), m(20), m(8)))["j0"], "a")  # 16 km vs 24 km
        out = self.two_vans(m(0), m(20), m(8), INPUT_VEHICLE_COST_PER_KM=("per_km", (1000, 0)))
        self.assertEqual(self.served(out)["j0"], "b")
        # one van alone: its cost is the travel seconds + 1000 per km
        alone = self.run_alg(
            {
                "INPUT_LAYER_1": jobs(m(8)),
                "INPUT_VEHICLES": points("vehicles", [{"xy": m(0), "name": "a", "per_km": 1000}]),
                "INPUT_VEHICLE_COST_PER_KM": "per_km",
            }
        )
        route = self.route(alone, "a")
        self.assertAlmostEqual(route["cost"], route["duration"] + 16 * 1000, delta=2)

    def test_cost_per_hour(self):
        out = self.two_vans(m(0), m(20), m(8), INPUT_VEHICLE_COST_PER_HOUR=("per_hour", (36_000, 3600)))
        self.assertEqual(self.served(out)["j0"], "b")  # a's time costs 10x
        alone = self.run_alg(
            {
                "INPUT_LAYER_1": jobs(m(8)),
                "INPUT_VEHICLES": points("vehicles", [{"xy": m(0), "name": "a", "per_hour": 7200}]),
                "INPUT_VEHICLE_COST_PER_HOUR": "per_hour",
            }
        )
        route = self.route(alone, "a")
        self.assertAlmostEqual(route["cost"], 2 * route["duration"], delta=2)

    def test_cost_error(self):
        with self.assertRaisesRegex(QgsProcessingException, "negative"):
            self.two_vans(m(0), m(20), m(8), INPUT_VEHICLE_FIXED_COST=("fixed", (-1, 0)))

    # ------------------------------------------------------------------ driving limits
    def one_van(self, job_points, **limit) -> dict:
        ((param, value),) = limit.items()
        return self.run_alg(
            {
                "INPUT_LAYER_1": jobs(*job_points),
                "INPUT_VEHICLES": points("vehicles", [{"xy": m(0), "name": "v", "limit": value}]),
                param: "limit",
            }
        )

    def test_max_travel_time(self):
        # Valhalla's matrix has whole seconds (km 0-5: 352, 5-10: 352, 10-14: 282, 14-0: 988), so
        # the shortest tour to all three jobs, 0 -> 14 -> 10 -> 5 -> 0, takes 1974 s
        tour = 988 + 282 + 352 + 352
        out = self.one_van([m(5), m(10), m(14)], INPUT_VEHICLE_MAX_TRAVEL_TIME=tour - 1)
        self.assertEqual(self.served(out), {"j0": "v", "j1": "v", "j2": None})
        out = self.one_van([m(5), m(10), m(14)], INPUT_VEHICLE_MAX_TRAVEL_TIME=tour)
        self.assertEqual(self.served(out), {"j0": "v", "j1": "v", "j2": "v"})
        self.assertEqual(self.route(out, "v")["duration"], tour)

    def test_max_distance(self):
        out = self.one_van([m(5), m(10), m(14)], INPUT_VEHICLE_MAX_DISTANCE=27_900)
        self.assertEqual(self.served(out), {"j0": "v", "j1": "v", "j2": None})
        out = self.one_van([m(5), m(10), m(14)], INPUT_VEHICLE_MAX_DISTANCE=28_100)
        self.assertEqual(set(self.served(out).values()), {"v"})

    def test_time_and_distance_differ(self):
        """Side Road's km 2: 24 km out and back but slow; Main Road's km 14: 28 km but fast."""
        side, main = [s(2)], [m(14)]  # ~2316 s / 24 km, 1976 s / 28 km
        self.assertEqual(self.served(self.one_van(side, INPUT_VEHICLE_MAX_DISTANCE=26_000)), {"j0": "v"})
        self.assertEqual(
            self.served(self.one_van(main, INPUT_VEHICLE_MAX_DISTANCE=26_000)), {"j0": None}
        )
        self.assertEqual(
            self.served(self.one_van(side, INPUT_VEHICLE_MAX_TRAVEL_TIME=2100)), {"j0": None}
        )
        self.assertEqual(
            self.served(self.one_van(main, INPUT_VEHICLE_MAX_TRAVEL_TIME=2100)), {"j0": "v"}
        )

    # ------------------------------------------------------------------ waste collection
    def run_waste(self, vehicle: dict, params: dict) -> dict:
        """5 bins of 1 at km 5, a truck of capacity 1 at km 0, the landfill at km 0."""
        return self.run_alg(
            {
                "INPUT_LAYER_1": jobs(*[m(5)] * 5, waste=1),
                "INPUT_JOB_PICKUP": "waste",
                "INPUT_VEHICLES": points(
                    "vehicles", [{"xy": m(0), "name": "t", "capacity": 1, **vehicle}]
                ),
                "INPUT_VEHICLE_CAPACITY": "capacity",
                "INPUT_LANDFILLS": points("landfills", [{"xy": m(0), "name": "dump"}]),
                "INPUT_DUMP_TIME": 0,
                **params,
            },
            alg_cls=ValhallaWasteTruck,
        )

    def trips(self, out) -> int:
        return sum(1 for f in out["stops"] if f["type"] == "landfill")

    def test_waste_max_travel_time(self):
        # each trip is km 0 -> 5 -> 0: 706 s; 3 trips fit into 2200 s, a 4th doesn't
        out = self.run_waste({"limit": 2200}, {"INPUT_VEHICLE_MAX_TRAVEL_TIME": "limit"})
        self.assertEqual((self.trips(out), len(out["unassigned"])), (3, 2))

    def test_waste_max_distance(self):
        # 10 km a trip: 2 fit into 25 km
        out = self.run_waste({"limit": 25_000}, {"INPUT_VEHICLE_MAX_DISTANCE": "limit"})
        self.assertEqual((self.trips(out), len(out["unassigned"])), (2, 3))

    def test_waste_end_point_and_budget(self):
        """Home is the end point at km 20; the 1412 s drive there counts against the limit."""
        out = self.run_waste(
            {"limit": 3600},
            {
                "INPUT_VEHICLE_MAX_TRAVEL_TIME": "limit",
                "INPUT_VEHICLE_ENDS": points("ends", [{"xy": m(20), "vehicle": "t"}]),
                "INPUT_VEHICLE_END_ID": "vehicle",
            },
        )
        # 3 x 706 + 1412 = 3530 <= 3600, a 4th trip would be 4236
        self.assertEqual(self.trips(out), 3)
        steps = sorted(out["stops"], key=lambda f: f["sequence"])
        self.assertEqual([f["type"] for f in steps[-2:]], ["landfill", "end"])
        self.assertAlmostEqual(steps[-1].geometry().asPoint().x(), m(20)[0], places=6)
        self.assertLessEqual(self.route(out, "t")["duration"], 3600)

    # ------------------------------------------------------------------ points far from roads
    def far_warnings(self, out) -> List[str]:
        return [w for w in out["warnings"] if "from a road" in w]

    def test_far_points(self):
        """A job 2 km south of Main Road and a depot ~800 m off it are named; 100 m off isn't."""
        out = self.run_alg(
            {
                "INPUT_LAYER_1": jobs(m(5), (m(8)[0], -2 * KM), (m(12)[0], -0.1 * KM)),
                "INPUT_VEHICLES": points("vehicles", [{"xy": (m(0)[0], -0.8 * KM), "name": "v"}]),
            }
        )
        (warning,) = self.far_warnings(out)
        self.assertIn("2 points are more than 500 m from a road", warning)
        self.assertRegex(warning, r"Job j1 \(2\.0 km\), Vehicle v \(79\d m\)")
        self.assertNotIn("j2", warning)
        # served anyway, from the road
        self.assertEqual(set(self.served(out).values()), {"v"})

    def test_far_points_threshold_and_off(self):
        job = jobs((m(8)[0], -0.3 * KM))
        vehicles = points("vehicles", [{"xy": m(0), "name": "v"}])
        for limit, warned in ((200, True), (500, False), (0, False)):
            with self.subTest(limit=limit):
                out = self.run_alg(
                    {"INPUT_LAYER_1": job, "INPUT_VEHICLES": vehicles, "INPUT_FAR_POINT_WARNING": limit}
                )
                self.assertEqual(bool(self.far_warnings(out)), warned)

    def test_far_landfill(self):
        # 1 km off in KM degrees, measured north-south: 993 m, a degree of latitude is shorter
        out = self.run_waste(
            {}, {"INPUT_LANDFILLS": points("landfills", [{"xy": (m(0)[0], -1 * KM), "name": "dump"}])}
        )
        (warning,) = self.far_warnings(out)
        self.assertIn("Landfill 1 (993 m)", warning)
