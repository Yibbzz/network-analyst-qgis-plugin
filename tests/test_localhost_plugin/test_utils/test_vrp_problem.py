import unittest
from datetime import datetime, time

from tests.utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

from qgis.core import NULL  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, QTime  # noqa: E402

from valhalla.processing.vehicle_routing import problem as vrp  # noqa: E402


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
            ("", None),
            ("  ", None),
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
        self.assertIsNone(vrp.to_amount(""))
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


class TestAttributes(unittest.TestCase):
    def test_to_profile(self):
        self.assertEqual(vrp.to_profile("Car"), "auto")
        self.assertEqual(vrp.to_profile(" truck "), "truck")
        self.assertIsNone(vrp.to_profile(""))
        with self.assertRaises(ValueError):
            vrp.to_profile("boat")

    def test_to_role(self):
        self.assertEqual(vrp.to_role("Pickup"), "pickup")
        self.assertIsNone(vrp.to_role(NULL))
        with self.assertRaises(ValueError):
            vrp.to_role("drop")


# locations 0 = depot, 1 = job a, 2 = job b; the truck can't get back from b
CAR = [[0, 10, 20], [10, 0, 10], [20, 10, 0]]
TRUCK = [[0, 20, 40], [20, 0, 20], [None, None, 0]]
MATRICES = {"auto": (CAR, CAR), "truck": (TRUCK, TRUCK)}


def make_problem(vehicles=None, jobs=None) -> vrp.Problem:
    return vrp.Problem(
        vehicles=vehicles or [vrp.Vehicle("v1", 0, "auto", capacity=[5], shift=[0, 100], max_tasks=4)],
        jobs=jobs or [vrp.Job(1, label="a"), vrp.Job(2, label="b")],
        coords=[(0, 0), (1, 1), (2, 2)],
    )


class TestProblem(unittest.TestCase):
    def test_unreachable(self):
        truck = [vrp.Vehicle("t", 0, "truck")]
        self.assertEqual(vrp.unreachable_jobs(make_problem(truck), MATRICES, True), [1])
        # without returning, getting there is enough
        self.assertEqual(vrp.unreachable_jobs(make_problem(truck), MATRICES, False), [])
        # a car in the fleet can do it
        mixed = truck + [vrp.Vehicle("c", 0, "auto")]
        self.assertEqual(vrp.unreachable_jobs(make_problem(mixed), MATRICES, True), [])

    def test_build(self):
        jobs = [vrp.Job(1, delivery=[2], window=[5, 50], service=30), vrp.Job(2)]
        problem, keep, unplanned = vrp.build_vroom_problem(make_problem(jobs=jobs), True, MATRICES)
        self.assertEqual((keep, unplanned), ([0, 1, 2], []))
        vehicle = problem["vehicles"][0]
        self.assertEqual(vehicle["capacity"], [5])
        self.assertEqual((vehicle["start_index"], vehicle["end_index"]), (0, 0))
        self.assertEqual(vehicle["time_window"], [0, 100])
        self.assertEqual(vehicle["max_tasks"], 4)
        self.assertNotIn("skills", vehicle)
        job_a, job_b = problem["jobs"]
        self.assertEqual((job_a["delivery"], job_a["pickup"]), ([2], [0]))
        self.assertEqual((job_b["delivery"], job_b["pickup"]), ([0], [0]))  # padded
        self.assertEqual(job_a["time_windows"], [[5, 50]])
        # only the fleet's profiles
        self.assertEqual(list(problem["matrices"]), ["auto"])

    def test_mixed_fleet_matrices(self):
        fleet = [vrp.Vehicle("t", 0, "truck"), vrp.Vehicle("c", 0, "auto")]
        problem, _, _ = vrp.build_vroom_problem(make_problem(fleet), False, MATRICES)
        self.assertEqual([v["profile"] for v in problem["vehicles"]], ["truck", "auto"])
        self.assertEqual(problem["matrices"]["truck"]["durations"][2][0], vrp.UNROUTABLE)
        self.assertEqual(problem["matrices"]["auto"]["durations"][2][0], 20)

    def test_build_skips_jobs(self):
        problem, keep, _ = vrp.build_vroom_problem(make_problem(), False, MATRICES, skip_jobs=[0])
        self.assertEqual(keep, [0, 2])
        self.assertEqual([j["id"] for j in problem["jobs"]], [1])
        self.assertEqual(problem["jobs"][0]["location_index"], 1)  # re-indexed
        self.assertNotIn("end_index", problem["vehicles"][0])
        self.assertEqual(len(problem["matrices"]["auto"]["durations"]), 2)

    def test_amounts_need_capacity(self):
        problem = make_problem(jobs=[vrp.Job(1, delivery=[1])])
        problem.vehicles[0].capacity = None
        with self.assertRaises(ValueError):
            vrp.build_vroom_problem(problem, True, MATRICES)

    def test_break(self):
        problem = make_problem()
        problem.vehicles[0].break_duration = 60
        vroom_problem, _, _ = vrp.build_vroom_problem(problem, True, MATRICES)
        # without a break window, the break may be anywhere in the shift
        self.assertEqual(
            vroom_problem["vehicles"][0]["breaks"],
            [{"id": 0, "time_windows": [[0, 100]], "service": 60}],
        )

    def test_shipments(self):
        jobs = [
            vrp.Job(1, shipment_id="s1", role="pickup", delivery=[3], service=60, label="a"),
            vrp.Job(2, shipment_id="s1", role="delivery", priority=10, label="b"),
            vrp.Job(2, delivery=[1], label="c"),
        ]
        problem, _, _ = vrp.build_vroom_problem(make_problem(jobs=jobs), True, MATRICES)
        self.assertEqual([j["id"] for j in problem["jobs"]], [2])
        (shipment,) = problem["shipments"]
        self.assertEqual(shipment["pickup"], {"id": 0, "location_index": 1, "service": 60})
        self.assertEqual(shipment["delivery"], {"id": 1, "location_index": 2})
        self.assertEqual((shipment["amount"], shipment["priority"]), ([3], 10))

    def test_shipments_are_validated(self):
        for jobs, message in (
            ([vrp.Job(1, shipment_id=1, role="pickup", label="a")], "one pickup and one delivery"),
            (
                [vrp.Job(1, shipment_id=1, role="pickup"), vrp.Job(2, shipment_id=1, role="pickup")],
                "one pickup and one delivery",
            ),
            ([vrp.Job(1, shipment_id=1, label="a"), vrp.Job(2)], "no role"),
            (
                [
                    vrp.Job(1, shipment_id=1, role="pickup", delivery=[1]),
                    vrp.Job(2, shipment_id=1, role="delivery", delivery=[2]),
                ],
                "different amounts",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                vrp.build_vroom_problem(make_problem(jobs=jobs), True, MATRICES)

    def test_shipment_unreachable_together(self):
        """The truck can reach both parts on their own, but not b and then back."""
        jobs = [vrp.Job(1, shipment_id=1, role="pickup"), vrp.Job(2, shipment_id=1, role="delivery")]
        problem = make_problem([vrp.Vehicle("t", 0, "truck")], jobs)
        self.assertEqual(vrp.unreachable_jobs(problem, MATRICES, True), [0, 1])

    def test_locked_vehicle(self):
        fleet = [vrp.Vehicle(7, 0, "auto"), vrp.Vehicle(8, 0, "auto", skills=[1])]
        # the job's field may be text while the vehicle IDs are numbers
        jobs = [vrp.Job(1, vehicle_id="8", skills=[1]), vrp.Job(2)]
        problem, _, _ = vrp.build_vroom_problem(make_problem(fleet, jobs), True, MATRICES)
        lock_7, lock_8 = vrp.LOCK_SKILL_BASE, vrp.LOCK_SKILL_BASE + 1
        self.assertEqual([v["skills"] for v in problem["vehicles"]], [[lock_7], [1, lock_8]])
        self.assertEqual(problem["jobs"][0]["skills"], [1, lock_8])
        self.assertNotIn("skills", problem["jobs"][1])

        with self.assertRaisesRegex(ValueError, "doesn't exist"):
            vrp.build_vroom_problem(make_problem(fleet, [vrp.Job(1, vehicle_id=9)]), True, MATRICES)

    def test_locked_shipment_parts_agree(self):
        fleet = [vrp.Vehicle(7, 0, "auto"), vrp.Vehicle(8, 0, "auto")]
        jobs = [
            vrp.Job(1, shipment_id=1, role="pickup", vehicle_id=7),
            vrp.Job(2, shipment_id=1, role="delivery"),
        ]
        problem, _, _ = vrp.build_vroom_problem(make_problem(fleet, jobs), True, MATRICES)
        shipment = problem["shipments"][0]
        self.assertEqual(shipment["skills"], [vrp.LOCK_SKILL_BASE])
        jobs[1].vehicle_id = 8
        with self.assertRaisesRegex(ValueError, "different vehicles"):
            vrp.build_vroom_problem(make_problem(fleet, jobs), True, MATRICES)

    def test_plan(self):
        fleet = [vrp.Vehicle("a", 0, "auto", break_duration=60), vrp.Vehicle("b", 0, "auto")]
        jobs = [
            vrp.Job(1, vehicle_id="a", sequence=2),
            vrp.Job(2, vehicle_id="a", sequence=1),
            vrp.Job(1, vehicle_id="b"),  # no sequence: left out
            vrp.Job(2, shipment_id=1, role="pickup", vehicle_id="b", sequence=1),
            vrp.Job(1, shipment_id=1, role="delivery", sequence=2),
        ]
        problem, _, unplanned = vrp.build_vroom_problem(
            make_problem(fleet, jobs), True, MATRICES, plan=True
        )
        self.assertEqual(unplanned, [2])
        steps = [[(s["type"], s.get("id")) for s in v["steps"]] for v in problem["vehicles"]]
        self.assertEqual(
            steps,
            [
                [("start", None), ("job", 1), ("job", 0), ("end", None)],
                [("start", None), ("pickup", 3), ("delivery", 4), ("end", None)],
            ],
        )
        self.assertNotIn("breaks", problem["vehicles"][0])
        self.assertEqual([j["id"] for j in problem["jobs"]], [0, 1])
