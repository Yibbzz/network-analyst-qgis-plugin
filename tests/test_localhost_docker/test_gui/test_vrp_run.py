from typing import List, Optional

from qgis.core import QgsCategorizedSymbolRenderer, QgsMapLayer
from qgis.PyQt.QtCore import QEventLoop, QTimer

from valhalla.core import pypi
from valhalla.global_definitions import RouterProfile
from valhalla.gui.dock_routing import RoutingDockWidget
from valhalla.gui.vrp_run import VrpRun, jobs_layer, split_waypoints, vehicles_layer
from valhalla.gui.widgets.waypoint_model import VRP, Waypoint

from ... import URL, LocalhostDockerTestCase, clear_project_points
from ...utilities import get_qgis_app
from ..test_processing.test_vehicle_routing.test_vrp import DEPOT, JOBS

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()


def table(*vehicles: dict, **job_attrs) -> List[Waypoint]:
    """The dock table: the JOBS with ``job_attrs``, then one vehicle at the DEPOT per dict."""
    jobs = [Waypoint(*c, {"role": "job", "name": f"job {i}", **job_attrs}) for i, c in enumerate(JOBS)]
    return jobs + [Waypoint(*DEPOT, {"role": "vehicle", **v}) for v in vehicles]


class TestVrpRun(LocalhostDockerTestCase):
    """The dock's background VRP run against localhost."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def setUp(self):
        clear_project_points()

    def run_vrp(self, waypoints: List[Waypoint], **kwargs):
        jobs, vehicles = split_waypoints(waypoints)
        result = dict()

        def on_done(layers: List[QgsMapLayer], error: Optional[str]):
            result.update(layers=layers, error=error)
            loop.quit()

        run = VrpRun(
            jobs,
            vehicles,
            **{
                "provider_url": URL,
                "profile": RouterProfile.CAR,
                "request_params": {},
                "return_to_start": True,
                "exploration_level": 5,
                "timeout": 0,
                "on_done": on_done,
                **kwargs,
            },
        )
        loop = QEventLoop()
        QTimer.singleShot(120_000, loop.quit)
        run.start()
        loop.exec()

        self.assertIn("layers", result, "timed out")
        return result["layers"], result["error"], run

    def test_layers(self):
        jobs, vehicles = split_waypoints(
            table({"name": "van", "amount": 0, "mode": "default"}, service=1.5)
        )
        self.assertEqual((len(jobs), len(vehicles)), (len(JOBS), 1))

        # no job has an amount: capacities are left out, even a vehicle's
        job = next(jobs_layer(jobs, with_amounts=False).getFeatures())
        # minutes -> seconds; the advanced columns' 0/empty = not set
        self.assertEqual(job.attributes(), ["job 0", None, 90] + [None] * 8)
        van = next(vehicles_layer(vehicles, with_amounts=False).getFeatures())
        self.assertEqual(van.attributes(), ["van"] + [None] * 9)  # "default" mode

        # unnamed vehicles get a name, a capacity of 0 counts as none
        unnamed = split_waypoints(table({"amount": 0, "mode": "truck"}))[1]
        vehicle = next(vehicles_layer(unnamed, with_amounts=True).getFeatures())
        expected = ["vehicle 1", None, None, None, None, "truck"] + [None] * 4
        self.assertEqual(vehicle.attributes(), expected)

        # the advanced columns
        advanced = table(
            {
                "name": "van",
                "max_jobs": 3,
                "break_start": "12:00",
                "break_end": "13:00",
                "break_minutes": 0.5,
            },
            priority=10,
            vehicle="van",
            order=2,
            shipment="s1",
            shipment_role="pickup",
        )
        jobs, vehicles = split_waypoints(advanced)
        job = next(jobs_layer(jobs, with_amounts=False).getFeatures())
        self.assertEqual(job.attributes()[-5:], [10, "van", 2, "s1", "pickup"])
        van = next(vehicles_layer(vehicles, with_amounts=False).getFeatures())
        self.assertEqual(van.attributes()[-4:], [3, "12:00", "13:00", 30])  # minutes -> seconds

    def test_solve(self):
        waypoints = table(
            {"name": "van", "amount": 4, "start": "08:00"},
            {"name": "bike", "amount": 4, "start": "08:00", "mode": "bicycle"},
            amount=1,
            start="09:00",
        )
        (unassigned, routes, stops), error, run = self.run_vrp(waypoints)
        self.assertIsNone(error)
        self.assertEqual(
            [layer.name() for layer in (unassigned, routes, stops)],
            ["VRP Unassigned", "VRP Routes", "VRP Stops"],
        )
        # 8 of the 10 jobs fit into the two vehicles
        self.assertEqual(unassigned.featureCount(), 2)
        self.assertEqual(sum(r["stops"] for r in routes.getFeatures()), 8)
        self.assertEqual({r["profile"] for r in routes.getFeatures()} <= {"auto", "bicycle"}, True)
        # one color per vehicle
        for layer in (routes, stops):
            self.assertIsInstance(layer.renderer(), QgsCategorizedSymbolRenderer)
        self.assertEqual(run.warnings, [])

    @staticmethod
    def jobs_by_vehicle(stops) -> dict:
        jobs = dict()
        for s in sorted(stops.getFeatures(), key=lambda s: (s["vehicle_id"], s["sequence"])):
            if s["type"] in ("job", "pickup", "delivery"):
                jobs.setdefault(s["vehicle_id"], []).append((s["name"], s["type"]))
        return jobs

    def test_advanced(self):
        """Locks, shipments, max jobs and breaks from the advanced columns."""
        waypoints = table(
            {"name": "van1", "start": "08:00"},
            {
                "name": "van2",
                "start": "08:00",
                "max_jobs": 4,
                "break_start": "09:00",
                "break_end": "17:00",
                "break_minutes": 30,
            },
        )
        attrs = {wp.attrs["name"]: wp.attrs for wp in waypoints}
        attrs["job 0"]["vehicle"] = "van2"
        attrs["job 6"].update(shipment="s1", shipment_role="pickup")  # Soldeu
        attrs["job 2"].update(shipment="s1", shipment_role="delivery")  # Sant Julià
        (unassigned, routes, stops), error, _ = self.run_vrp(waypoints)
        self.assertIsNone(error)

        by_vehicle = self.jobs_by_vehicle(stops)
        self.assertIn(("job 0", "job"), by_vehicle["van2"])
        self.assertLessEqual(len(by_vehicle.get("van2", [])), 4)
        shipment = ("job 6", "job 2")
        carriers = {vid for vid, steps in by_vehicle.items() for job, _ in steps if job in shipment}
        self.assertEqual(len(carriers), 1)  # one vehicle
        steps = by_vehicle[carriers.pop()]
        self.assertLess(steps.index(("job 6", "pickup")), steps.index(("job 2", "delivery")))
        breaks = [s for s in stops.getFeatures() if s["type"] == "break"]
        self.assertEqual([(b["vehicle_id"], b["service"]) for b in breaks], [("van2", 1800)])

    def test_keep_order(self):
        """The table's vehicle & order are kept; vehicles without jobs get no route."""
        waypoints = table({"name": "van"}, {"name": "bike", "mode": "bicycle"})
        for order, i in enumerate((6, 2, 0), 1):  # Soldeu, Sant Julià, Escaldes: a zig-zag
            waypoints[i].attrs.update(vehicle="van", order=order)
        (unassigned, routes, stops), error, run = self.run_vrp(waypoints, keep_order=True)
        self.assertIsNone(error)
        self.assertEqual(
            self.jobs_by_vehicle(stops), {"van": [("job 6", "job"), ("job 2", "job"), ("job 0", "job")]}
        )
        self.assertEqual([r["name"] for r in routes.getFeatures()], ["van"])
        self.assertEqual({u["reason"] for u in unassigned.getFeatures()}, {"no vehicle or sequence"})
        self.assertEqual(unassigned.featureCount(), len(JOBS) - 3)

    def test_error(self):
        layers, error, _ = self.run_vrp(table({"name": "van"}, {"name": "van"}))
        self.assertEqual(layers, [])
        self.assertIn("twice", error)

    def test_bad_time(self):
        layers, error, _ = self.run_vrp(table({"name": "van"}, start="noon"))
        self.assertEqual(layers, [])
        self.assertIn("not a time", error)


class TestVrpDock(LocalhostDockerTestCase):
    def test_menu(self):
        dlg = RoutingDockWidget(IFACE)
        menu = dlg.menu_widget
        rows = [
            row
            for row in range(menu.count())
            if dlg._menu_entry(row) and dlg._menu_entry(row).kind == VRP
        ]
        self.assertEqual(len(rows), 1)
        menu.setCurrentRow(rows[0])
        self.assertIs(dlg.ui_params_stacked.currentWidget(), dlg.ui_vrp_params)
        self.assertEqual(dlg.waypoints_widget.table_model.table_kind, VRP)
        self.assertIn("Vehicle Routing", dlg.windowTitle())

        # without points it asks for them instead of running
        dlg._on_execute()
        self.assertIsNone(dlg.vrp_run)

        # the advanced columns are hidden until asked for; keeping the order shows them
        table_view, columns = dlg.waypoints_widget.ui_table, VRP.columns
        advanced = [i for i, c in enumerate(columns) if c.advanced]
        self.assertTrue(advanced)
        self.assertTrue(all(table_view.isColumnHidden(i) for i in advanced))
        basic = [i for i, c in enumerate(columns) if not c.advanced]
        self.assertFalse(any(table_view.isColumnHidden(i) for i in basic))
        dlg.ui_vrp_keep_order.setChecked(True)
        self.assertTrue(dlg.ui_vrp_advanced.isChecked())
        self.assertFalse(any(table_view.isColumnHidden(i) for i in advanced))
        dlg.ui_vrp_advanced.setChecked(False)
        self.assertTrue(all(table_view.isColumnHidden(i) for i in advanced))
        dlg.unload()
