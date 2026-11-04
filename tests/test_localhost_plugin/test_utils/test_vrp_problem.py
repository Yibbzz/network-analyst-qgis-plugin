import unittest
from datetime import datetime, time

from tests.utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

from qgis.core import NULL, QgsFeature  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, QTime  # noqa: E402

from valhalla.processing.vehicle_routing import vrp  # noqa: E402


class TestParsing(unittest.TestCase):
    def test_to_seconds(self):
        for value, expected in (
            (90, 90),
            (90.4, 90),
            ("08:30", 30600),
            ("8:30:15", 30615),
            ("120", 120),
            (QTime(8, 30), 30600),
            (time(8, 30), 30600),
            (QDateTime.fromSecsSinceEpoch(1_700_000_000), 1_700_000_000),
            (datetime.fromtimestamp(1_700_000_000), 1_700_000_000),
            (None, None),
            (NULL, None),
        ):
            with self.subTest(value=value):
                self.assertEqual(vrp.to_seconds(value), expected)
        for bad in ("noon", True, "8h30"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vrp.to_seconds(bad)

    def test_to_amount(self):
        self.assertEqual(vrp.to_amount(3), [3])
        self.assertEqual(vrp.to_amount("500, 2"), [500, 2])
        self.assertIsNone(vrp.to_amount(NULL))
        for bad in ("-1", "a,b", -2):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vrp.to_amount(bad)

    def test_to_skills(self):
        self.assertEqual(vrp.to_skills("3,1,3"), [1, 3])

    def test_time_window(self):
        self.assertIsNone(vrp.time_window(None, None))
        self.assertEqual(vrp.time_window(10, None), [10, vrp.OPEN_END])
        self.assertEqual(vrp.time_window(None, 10), [0, 10])
        with self.assertRaises(ValueError):
            vrp.time_window(20, 10)

    def test_seconds_to_hms(self):
        self.assertEqual(vrp.seconds_to_hms(30615), "08:30:15")
        self.assertEqual(vrp.seconds_to_hms(90000), "25:00:00")
        self.assertIsNone(vrp.seconds_to_hms(1_700_000_000))  # epoch, not a time of day


class TestChunks(unittest.TestCase):
    def test_matrix_row_chunks(self):
        self.assertEqual(vrp.matrix_row_chunks(5, 2500), [[0, 1, 2, 3, 4]])
        self.assertEqual(vrp.matrix_row_chunks(5, 10), [[0, 1], [2, 3], [4]])
        with self.assertRaises(ValueError):
            vrp.matrix_row_chunks(30, 20)

    def test_route_chunks(self):
        self.assertEqual(vrp.route_chunks([0, 1, 2], 20), [[0, 1, 2]])
        # each chunk starts where the previous one ended
        self.assertEqual(vrp.route_chunks([0, 1, 2, 3, 4, 5], 3), [[0, 1, 2], [2, 3, 4], [4, 5]])
        # two jobs at one place
        self.assertEqual(vrp.route_chunks([0, 1, 1, 0], 20), [[0, 1, 0]])
        self.assertEqual(vrp.route_chunks([0, 0], 20), [])


class TestProblem(unittest.TestCase):
    # locations 0 = depot, 1 = job a, 2 = job b (one-way trap: reachable, no way back)
    DURATIONS = [[0, 10, 20], [10, 0, 10], [20, None, 0]]

    def test_unreachable(self):
        self.assertEqual(vrp.unreachable_jobs(self.DURATIONS, [0], [0], [1, 2]), [])
        self.assertEqual(
            vrp.unreachable_jobs([[0, 10, 20], [10, 0, 10], [None, None, 0]], [0], [0], [1, 2]), [1]
        )
        # without returning, getting there is enough
        self.assertEqual(
            vrp.unreachable_jobs([[0, 10, 20], [10, 0, 10], [None, None, 0]], [0], [None], [1, 2]), []
        )

    def _problem(self, **job) -> vrp.Problem:
        return vrp.Problem(
            vehicles=[vrp.Vehicle(QgsFeature(), "v1", 0, capacity=[5], shift=[0, 100], max_tasks=4)],
            jobs=[vrp.Job(QgsFeature(), 1, **job), vrp.Job(QgsFeature(), 2)],
            coords=[(0, 0), (1, 1), (2, 2)],
        )

    def test_build(self):
        problem, keep = vrp.build_vroom_problem(
            self._problem(delivery=[2], window=[5, 50], service=30),
            "auto",
            True,
            self.DURATIONS,
            self.DURATIONS,
        )
        self.assertEqual(keep, [0, 1, 2])
        vehicle = problem["vehicles"][0]
        self.assertEqual(vehicle["capacity"], [5])
        self.assertEqual((vehicle["start_index"], vehicle["end_index"]), (0, 0))
        self.assertEqual(vehicle["time_window"], [0, 100])
        self.assertEqual(vehicle["max_tasks"], 4)
        job_a, job_b = problem["jobs"]
        self.assertEqual((job_a["delivery"], job_a["pickup"]), ([2], [0]))
        self.assertEqual((job_b["delivery"], job_b["pickup"]), ([0], [0]))  # padded
        self.assertEqual(job_a["time_windows"], [[5, 50]])
        self.assertEqual(problem["matrices"]["auto"]["durations"][2][1], vrp.UNROUTABLE)

    def test_build_skips_jobs(self):
        problem, keep = vrp.build_vroom_problem(
            self._problem(), "auto", False, self.DURATIONS, self.DURATIONS, skip_jobs=[0]
        )
        self.assertEqual(keep, [0, 2])
        self.assertEqual([j["id"] for j in problem["jobs"]], [1])
        self.assertEqual(problem["jobs"][0]["location_index"], 1)  # re-indexed
        self.assertNotIn("end_index", problem["vehicles"][0])
        self.assertEqual(len(problem["matrices"]["auto"]["durations"]), 2)

    def test_amounts_need_capacity(self):
        problem = self._problem(delivery=[1])
        problem.vehicles[0].capacity = None
        with self.assertRaises(ValueError):
            vrp.build_vroom_problem(problem, "auto", True, self.DURATIONS, self.DURATIONS)

    def test_break(self):
        problem = self._problem()
        problem.vehicles[0].break_duration = 60
        vroom_problem, _ = vrp.build_vroom_problem(problem, "auto", True, self.DURATIONS, self.DURATIONS)
        # without a break window, the break may be anywhere in the shift
        self.assertEqual(
            vroom_problem["vehicles"][0]["breaks"],
            [{"id": 0, "time_windows": [[0, 100]], "service": 60}],
        )
