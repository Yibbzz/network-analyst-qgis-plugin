from typing import Dict, List, Tuple

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsPointXY,
    QgsGeometry,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingOutputLayerDefinition,
    QgsProcessingUtils,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

from valhalla.core import pypi
from valhalla.processing.vehicle_routing.vrp import ValhallaVrpCar

from .... import LocalhostDockerTestCase
from ....utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

DEPOT = (1.5218, 42.5063)  # Andorra la Vella
JOBS = [
    (1.5345, 42.5096),  # Escaldes
    (1.5800, 42.5340),  # Encamp
    (1.4910, 42.4636),  # Sant Julià
    (1.5148, 42.5455),  # La Massana
    (1.5333, 42.5560),  # Ordino
    (1.5975, 42.5672),  # Canillo
    (1.6680, 42.5770),  # Soldeu
    (1.4830, 42.5710),  # Arinsal
    (1.5420, 42.5120),  # Engordany
    (1.5050, 42.4900),  # Santa Coloma
]


def jobs_layer(**fields: List) -> QgsVectorLayer:
    """A memory point layer of JOBS, "name" plus one column per keyword (a list per job)."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "jobs", "memory")
    attrs = [QgsField("name", QVariant.String)]
    for name, values in fields.items():
        attrs.append(QgsField(name, QVariant.Int if isinstance(values[0], int) else QVariant.String))
    layer.dataProvider().addAttributes(attrs)
    layer.updateFields()
    for i, coords in enumerate(JOBS):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*coords)))
        feat.setAttributes([f"job {i}"] + [values[i] for values in fields.values()])
        layer.dataProvider().addFeature(feat)
    return layer


def vehicles_layer(names: List[str], **fields: List) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "vehicles", "memory")
    attrs = [QgsField("vid", QVariant.String)]
    for name, values in fields.items():
        attrs.append(QgsField(name, QVariant.Int if isinstance(values[0], int) else QVariant.String))
    layer.dataProvider().addAttributes(attrs)
    layer.updateFields()
    for i, vid in enumerate(names):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*DEPOT)))
        feat.setAttributes([vid] + [values[i] for values in fields.values()])
        layer.dataProvider().addFeature(feat)
    return layer


class TestVrp(LocalhostDockerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def run_vrp(self, params: dict) -> Dict[str, List[QgsFeature]]:
        alg = ValhallaVrpCar()
        alg.initAlgorithm({})
        params = {"INPUT_PROVIDER": 1, "INPUT_VEHICLE_ID": "vid", **params}  # localhost
        for out in (alg.OUT, alg.OUT_STOPS, alg.OUT_UNASSIGNED):
            params[out] = QgsProcessingOutputLayerDefinition("TEMPORARY_OUTPUT")
        ctx, feedback = QgsProcessingContext(), QgsProcessingFeedback()
        self.assertTrue(alg.prepareAlgorithm(params, ctx, feedback))
        result = alg.processAlgorithm(params, ctx, feedback)
        return {
            key: list(QgsProcessingUtils.mapLayerFromString(result[out], ctx).getFeatures())
            for key, out in (
                ("routes", alg.OUT),
                ("stops", alg.OUT_STOPS),
                ("unassigned", alg.OUT_UNASSIGNED),
            )
        }

    @staticmethod
    def jobs_by_vehicle(stops: List[QgsFeature]) -> Dict[str, List[str]]:
        by_vehicle: Dict[str, List[Tuple[int, str]]] = dict()
        for s in stops:
            if s["type"] == "job":
                by_vehicle.setdefault(s["vehicle_id"], []).append((s["sequence"], s["name"]))
        return {v: [name for _, name in sorted(jobs)] for v, jobs in by_vehicle.items()}

    def test_basic(self):
        out = self.run_vrp({"INPUT_LAYER_1": jobs_layer(), "INPUT_VEHICLES": vehicles_layer(["a", "b"])})

        self.assertEqual(out["unassigned"], [])
        served = [name for names in self.jobs_by_vehicle(out["stops"]).values() for name in names]
        self.assertCountEqual(served, [f"job {i}" for i in range(len(JOBS))])
        for route in out["routes"]:
            self.assertGreater(route["distance"], 0)
            self.assertGreater(route["duration"], 0)
            self.assertFalse(route.geometry().isEmpty())
            line = route.geometry().asPolyline()
            # out and back from the depot
            for end in (line[0], line[-1]):
                self.assertLess(end.distance(QgsPointXY(*DEPOT)), 0.01)
        # every route has a start and an end besides its jobs
        self.assertEqual(len(out["stops"]), len(JOBS) + 2 * len(out["routes"]))

    def test_capacity(self):
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(demand=[1] * len(JOBS)),
                "INPUT_JOB_DELIVERY": "demand",
                "INPUT_VEHICLES": vehicles_layer(["a", "b"], cap=[3, 3]),
                "INPUT_VEHICLE_CAPACITY": "cap",
            }
        )
        self.assertEqual(len(out["unassigned"]), len(JOBS) - 6)
        self.assertEqual({f["reason"] for f in out["unassigned"]}, {"not assigned"})
        for names in self.jobs_by_vehicle(out["stops"]).values():
            self.assertLessEqual(len(names), 3)

    def test_skills(self):
        # only vehicle b can serve job 6 (Soldeu)
        skills = [""] * len(JOBS)
        skills[6] = "7"
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(skills=skills),
                "INPUT_JOB_SKILLS": "skills",
                "INPUT_VEHICLES": vehicles_layer(["a", "b"], skills=["1", "1,7"]),
                "INPUT_VEHICLE_SKILLS": "skills",
            }
        )
        self.assertIn("job 6", self.jobs_by_vehicle(out["stops"]).get("b", []))

    def test_time_windows_and_break(self):
        # all jobs between 09:00 and 12:00, 10 minutes each, a 30 min break between 10 and 11
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(
                    tw_start=["09:00"] * len(JOBS),
                    tw_end=["12:00"] * len(JOBS),
                    service=[600] * len(JOBS),
                ),
                "INPUT_JOB_TW_START": "tw_start",
                "INPUT_JOB_TW_END": "tw_end",
                "INPUT_JOB_SERVICE": "service",
                "INPUT_VEHICLES": vehicles_layer(
                    ["a", "b"],
                    start=["08:00"] * 2,
                    b_start=["10:00"] * 2,
                    b_end=["11:00"] * 2,
                    b_len=[1800] * 2,
                ),
                "INPUT_VEHICLE_SHIFT_START": "start",
                "INPUT_VEHICLE_BREAK_START": "b_start",
                "INPUT_VEHICLE_BREAK_END": "b_end",
                "INPUT_VEHICLE_BREAK_DURATION": "b_len",
            }
        )
        self.assertEqual(out["unassigned"], [])
        for stop in out["stops"]:
            start = stop["arrival"] + stop["waiting"]
            if stop["type"] == "job":
                self.assertGreaterEqual(start, 9 * 3600)
                self.assertLessEqual(start, 12 * 3600)
                self.assertEqual(stop["departure"], start + 600)
            elif stop["type"] == "break":
                self.assertGreaterEqual(start, 10 * 3600)
                self.assertLessEqual(start, 11 * 3600)
        self.assertEqual(sum(1 for s in out["stops"] if s["type"] == "break"), len(out["routes"]))
        self.assertTrue(all(s["arrival_hms"] for s in out["stops"]))

    def test_request_chunks(self):
        """Tiny server limits give the same solution, just in more requests."""
        params = {"INPUT_LAYER_1": jobs_layer(), "INPUT_VEHICLES": vehicles_layer(["a"])}
        whole = self.run_vrp(params)
        chunked = self.run_vrp({**params, "INPUT_MAX_MATRIX_PAIRS": 25, "INPUT_MAX_ROUTE_LOCATIONS": 3})

        self.assertEqual(self.jobs_by_vehicle(whole["stops"]), self.jobs_by_vehicle(chunked["stops"]))
        self.assertAlmostEqual(whole["routes"][0]["duration"], chunked["routes"][0]["duration"])
        # chunked route lines differ slightly where they're stitched
        self.assertAlmostEqual(
            whole["routes"][0]["distance"],
            chunked["routes"][0]["distance"],
            delta=0.02 * whole["routes"][0]["distance"],
        )

    def test_inputs_are_validated(self):
        with self.assertRaisesRegex(QgsProcessingException, "capacity"):
            self.run_vrp(
                {
                    "INPUT_LAYER_1": jobs_layer(demand=[1] * len(JOBS)),
                    "INPUT_JOB_DELIVERY": "demand",
                    "INPUT_VEHICLES": vehicles_layer(["a"]),
                }
            )
        with self.assertRaisesRegex(QgsProcessingException, "not a time"):
            self.run_vrp(
                {
                    "INPUT_LAYER_1": jobs_layer(tw=["noon"] * len(JOBS)),
                    "INPUT_JOB_TW_START": "tw",
                    "INPUT_VEHICLES": vehicles_layer(["a"]),
                }
            )
        with self.assertRaisesRegex(QgsProcessingException, "unique"):
            self.run_vrp({"INPUT_LAYER_1": jobs_layer(), "INPUT_VEHICLES": vehicles_layer(["a", "a"])})

    def test_mixed_fleet(self):
        # only the walker can serve job 0 (Escaldes, close by)
        skills = [""] * len(JOBS)
        skills[0] = "5"
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(skills=skills),
                "INPUT_JOB_SKILLS": "skills",
                "INPUT_VEHICLES": vehicles_layer(
                    ["walker", "van"], mode=["pedestrian", ""], skills=["5", ""]
                ),
                "INPUT_VEHICLE_PROFILE": "mode",
                "INPUT_VEHICLE_SKILLS": "skills",
            }
        )
        routes = {r["vehicle_id"]: r for r in out["routes"]}
        self.assertEqual(routes["walker"]["profile"], "pedestrian")
        self.assertEqual(routes["van"]["profile"], "auto")  # the algorithm's own
        self.assertEqual(self.jobs_by_vehicle(out["stops"])["walker"], ["job 0"])
        # seconds per meter: walking is several times slower than driving
        pace = {v: r["duration"] / r["distance"] for v, r in routes.items()}
        self.assertGreater(pace["walker"], 3 * pace["van"])

    def test_shipments(self):
        # job 2 -> job 1 and job 6 -> job 3 are pickups with their deliveries
        shipment = [""] * len(JOBS)
        role = [""] * len(JOBS)
        for pickup, delivery, sid in ((2, 1, "s1"), (6, 3, "s2")):
            shipment[pickup] = shipment[delivery] = sid
            role[pickup], role[delivery] = "pickup", "delivery"
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(shipment=shipment, role=role, amount=[1] * len(JOBS)),
                "INPUT_JOB_SHIPMENT_ID": "shipment",
                "INPUT_JOB_SHIPMENT_ROLE": "role",
                "INPUT_JOB_DELIVERY": "amount",
                "INPUT_VEHICLES": vehicles_layer(["a", "b"], cap=[10, 10]),
                "INPUT_VEHICLE_CAPACITY": "cap",
            }
        )
        self.assertEqual(out["unassigned"], [])
        steps = {s["name"]: s for s in out["stops"] if s["type"] in ("job", "pickup", "delivery")}
        for pickup, delivery in ((2, 1), (6, 3)):
            p, d = steps[f"job {pickup}"], steps[f"job {delivery}"]
            self.assertEqual((p["type"], d["type"]), ("pickup", "delivery"))
            self.assertEqual(p["vehicle_id"], d["vehicle_id"])
            self.assertLess(p["sequence"], d["sequence"])

    def test_locked_vehicles(self):
        assigned = [""] * len(JOBS)
        assigned[6], assigned[7] = "b", "a"  # Soldeu and Arinsal, opposite ends
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(assigned=assigned),
                "INPUT_JOB_VEHICLE": "assigned",
                "INPUT_VEHICLES": vehicles_layer(["a", "b"]),
            }
        )
        by_vehicle = self.jobs_by_vehicle(out["stops"])
        self.assertIn("job 6", by_vehicle["b"])
        self.assertIn("job 7", by_vehicle["a"])
        self.assertEqual(out["unassigned"], [])

    def test_keep_order(self):
        """Plan mode keeps a bad order, computes its times and reports the late job."""
        assigned = [""] * len(JOBS)
        sequence = [0] * len(JOBS)
        tw_end = [""] * len(JOBS)
        for seq, job in enumerate((6, 2, 0)):  # Soldeu, Sant Julià, Escaldes: zig-zag
            assigned[job], sequence[job] = "a", seq + 1
        tw_end[0] = "00:10"  # can't be in Escaldes within 10 minutes after Soldeu
        out = self.run_vrp(
            {
                "INPUT_LAYER_1": jobs_layer(assigned=assigned, seq=sequence, tw_end=tw_end),
                "INPUT_JOB_VEHICLE": "assigned",
                "INPUT_JOB_SEQUENCE": "seq",
                "INPUT_JOB_TW_END": "tw_end",
                "INPUT_KEEP_ORDER": True,
                "INPUT_VEHICLES": vehicles_layer(["a", "b"]),
            }
        )
        self.assertEqual(self.jobs_by_vehicle(out["stops"]), {"a": ["job 6", "job 2", "job 0"]})
        self.assertEqual(
            {f["name"]: f["reason"] for f in out["unassigned"]},
            {f"job {i}": "no vehicle or sequence" for i in range(len(JOBS)) if i not in (6, 2, 0)},
        )
        late = [s for s in out["stops"] if s["name"] == "job 0"][0]
        self.assertIn("delay", late["violations"])
        self.assertIn("delay", out["routes"][0]["violations"])

    def test_keep_order_needs_fields(self):
        with self.assertRaisesRegex(QgsProcessingException, "sequence"):
            self.run_vrp(
                {
                    "INPUT_LAYER_1": jobs_layer(),
                    "INPUT_KEEP_ORDER": True,
                    "INPUT_VEHICLES": vehicles_layer(["a"]),
                }
            )
