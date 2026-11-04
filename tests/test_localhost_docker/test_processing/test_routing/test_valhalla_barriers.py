from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

from valhalla.global_definitions import FieldNames
from valhalla.processing.routing.valhalla.directions import ValhallaDirectionsCar
from valhalla.processing.routing.valhalla.matrix import ValhallaMatrixCar

from ....utilities import get_qgis_app
from ...test_processing.processing_base import ProcessingBase

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

# a short route along the CG-2 (~83 cost), and a point on it; going round costs ~1218 more
A = (1.559617, 42.521943)
B = (1.571642, 42.529238)
ON_CG2 = (1.567536, 42.526785)


def points(*coords, crs="EPSG:4326") -> QgsVectorLayer:
    layer = QgsVectorLayer(f"Point?crs={crs}", "points", "memory")
    layer.dataProvider().addAttributes([QgsField("cost", QVariant.Double)])
    layer.updateFields()
    for c in coords:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*c)))
        layer.dataProvider().addFeature(feat)
    return layer


def square(center, half=0.0015, factor=None) -> QgsVectorLayer:
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "polygons", "memory")
    layer.dataProvider().addAttributes([QgsField("factor", QVariant.Double)])
    layer.updateFields()
    x, y = center
    feat = QgsFeature(layer.fields())
    feat.setGeometry(
        QgsGeometry.fromWkt(
            f"POLYGON(({x - half} {y - half}, {x + half} {y - half}, {x + half} {y + half}, "
            f"{x - half} {y + half}, {x - half} {y - half}))"
        )
    )
    feat.setAttributes([factor])
    layer.dataProvider().addFeature(feat)
    return layer


class TestBarriers(ProcessingBase):
    def route(self, **params):
        feats, _ = self.run_routing_algorithm(
            ValhallaDirectionsCar(), {"INPUT_LAYER_1": points(A, B), **params}
        )
        return feats[0]

    def test_point_barrier_cost(self):
        """Crossing the point costs the added cost: worth a detour only when that's more."""
        base = self.route()
        cheap = self.route(INPUT_POINT_BARRIERS=points(ON_CG2), INPUT_POINT_BARRIER_COST=600)
        dear = self.route(INPUT_POINT_BARRIERS=points(ON_CG2), INPUT_POINT_BARRIER_COST=2000)

        # Valhalla reports durations without the barrier's cost: the same route looks the same
        self.assertEqual(cheap[FieldNames.DURATION], base[FieldNames.DURATION])
        self.assertTrue(cheap.geometry().equals(base.geometry()))
        self.assertGreater(dear[FieldNames.DURATION], base[FieldNames.DURATION] + 600)
        self.assertFalse(
            dear.geometry().intersects(QgsGeometry.fromPointXY(QgsPointXY(*ON_CG2)).buffer(0.0002, 8))
        )

    def test_point_barrier_field_and_crs(self):
        """The cost comes from the field, the point may be in another CRS."""
        layer = points((174497.3, 5240218.8), crs="EPSG:3857")  # ON_CG2 in web mercator
        layer.dataProvider().changeAttributeValues({1: {0: 2000.0}})
        dear = self.route(
            INPUT_POINT_BARRIERS=layer,
            INPUT_POINT_BARRIER_FIELD="cost",
            INPUT_POINT_BARRIER_COST=0,
        )
        self.assertGreater(dear[FieldNames.DURATION], self.route()[FieldNames.DURATION] + 600)

    def test_polygon_barrier(self):
        base = self.route()
        avoided = self.route(INPUT_POLYGON_BARRIERS=square(ON_CG2), INPUT_POLYGON_BARRIER_FACTOR=1000)
        self.assertGreater(avoided[FieldNames.DURATION], base[FieldNames.DURATION] + 600)
        self.assertFalse(
            avoided.geometry().intersects(square(ON_CG2, half=0.0005).getFeature(1).geometry())
        )
        # a factor from the field, a mild one: not worth the detour
        mild = self.route(
            INPUT_POLYGON_BARRIERS=square(ON_CG2, factor=1.5), INPUT_POLYGON_BARRIER_FIELD="factor"
        )
        self.assertTrue(mild.geometry().equals(base.geometry()))

    def test_matrix(self):
        """Matrices (and so the VRP) choose their paths with the barriers too."""

        def duration(**params):
            alg = ValhallaMatrixCar()
            feats, _ = self.run_routing_algorithm(
                alg, {"INPUT_LAYER_1": points(A), "INPUT_LAYER_2": points(B), **params}
            )
            return feats[0][FieldNames.DURATION]

        base = duration()
        self.assertEqual(
            duration(INPUT_POINT_BARRIERS=points(ON_CG2), INPUT_POINT_BARRIER_COST=600), base
        )
        self.assertGreater(
            duration(INPUT_POINT_BARRIERS=points(ON_CG2), INPUT_POINT_BARRIER_COST=2000), base + 600
        )

    def test_dense_polygon(self):
        """A square kilometre of town: some road pieces don't edge-walk, they're dropped, not fatal."""
        center = (1.5218, 42.5063)  # Andorra la Vella
        base = self.route()
        routed = self.route(
            INPUT_POLYGON_BARRIERS=square(center, half=0.006), INPUT_POLYGON_BARRIER_FACTOR=3
        )
        self.assertGreater(routed[FieldNames.DISTANCE], 0)
        self.assertIsNotNone(base)
