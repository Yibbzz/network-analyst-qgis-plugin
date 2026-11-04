from typing import Dict, List

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingOutputLayerDefinition,
    QgsProcessingUtils,
    QgsVectorLayer,
)

from valhalla.core import pypi
from valhalla.processing.vehicle_routing.waste import ValhallaWasteTruck

from .... import LocalhostDockerTestCase
from ....utilities import get_qgis_app
from .test_vrp import DEPOT, JOBS, jobs_layer, vehicles_layer

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

LANDFILL = (1.5135, 42.4985)  # on the CG-1, 2 minutes south of the depot


def landfills_layer(*points) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "landfills", "memory")
    for coords in points:
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*coords)))
        layer.dataProvider().addFeature(feat)
    return layer


class TestWaste(LocalhostDockerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def run_waste(self, params: dict) -> Dict[str, List[QgsFeature]]:
        alg = ValhallaWasteTruck()
        alg.initAlgorithm({})
        params = {
            "INPUT_PROVIDER": 1,  # localhost
            "INPUT_VEHICLE_ID": "vid",
            "INPUT_LANDFILLS": landfills_layer(LANDFILL),
            **params,
        }
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

    def test_trips(self):
        """10 bins of 1, a truck holding 3: four trips to the landfill, then home."""
        out = self.run_waste(
            {
                "INPUT_LAYER_1": jobs_layer(waste=[1] * len(JOBS)),
                "INPUT_JOB_PICKUP": "waste",
                "INPUT_VEHICLES": vehicles_layer(["truck"], cap=[3]),
                "INPUT_VEHICLE_CAPACITY": "cap",
                "INPUT_DUMP_TIME": 600,
            }
        )
        self.assertEqual(out["unassigned"], [])
        stops = sorted(out["stops"], key=lambda s: s["sequence"])
        types = [s["type"] for s in stops]
        self.assertEqual(types[0], "start")
        self.assertEqual(types[-1], "end")
        self.assertEqual(types.count("job"), len(JOBS))
        self.assertEqual(types.count("landfill"), 4)
        self.assertEqual(types[-2], "landfill")
        # never more than 3 on board, empty after the landfill
        for s in stops:
            self.assertLessEqual(int(s["load"]), 3)
            if s["type"] == "landfill":
                self.assertEqual(s["load"], "0")
                self.assertLess(s.geometry().asPoint().distance(QgsPointXY(*LANDFILL)), 0.01)
                self.assertEqual(s["service"], 600)
        arrivals = [s["arrival"] for s in stops]
        self.assertEqual(arrivals, sorted(arrivals))
        (route,) = out["routes"]
        self.assertEqual(route["stops"], len(JOBS))
        line = route.geometry().asPolyline()
        self.assertLess(line[-1].distance(QgsPointXY(*DEPOT)), 0.01)

    def test_shift_limits_trips(self):
        out = self.run_waste(
            {
                "INPUT_LAYER_1": jobs_layer(waste=[1] * len(JOBS)),
                "INPUT_JOB_PICKUP": "waste",
                "INPUT_VEHICLES": vehicles_layer(["truck"], cap=[3], start=["08:00"], end=["09:00"]),
                "INPUT_VEHICLE_CAPACITY": "cap",
                "INPUT_VEHICLE_SHIFT_START": "start",
                "INPUT_VEHICLE_SHIFT_END": "end",
                "INPUT_DUMP_TIME": 900,
            }
        )
        self.assertGreater(len(out["unassigned"]), 0)
        end = max(s["arrival"] for s in out["stops"])
        self.assertLessEqual(end, 9 * 3600)

    def test_needs_landfills(self):
        with self.assertRaisesRegex(QgsProcessingException, "landfills"):
            self.run_waste(
                {
                    "INPUT_LAYER_1": jobs_layer(),
                    "INPUT_VEHICLES": vehicles_layer(["truck"]),
                    "INPUT_LANDFILLS": landfills_layer(),
                }
            )
