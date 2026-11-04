import unittest

from tests.utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

from valhalla.core import pypi, vroom  # noqa: E402
from valhalla.processing.vehicle_routing.multi_trip import LANDFILL, solve_multi_trip  # noqa: E402
from valhalla.processing.vehicle_routing.problem import Job, Problem, Vehicle  # noqa: E402

# locations on a line, 60 s apart: 0 = depot, 1 = landfill, 2..7 = bins
N = 8
LINE = [[abs(a - b) * 60 for b in range(N)] for a in range(N)]
MATRICES = {"truck": (LINE, LINE)}
DUMP = 300


def problem(**vehicle) -> Problem:
    return Problem(
        vehicles=[Vehicle("t1", 0, "truck", capacity=[2], **vehicle)],
        jobs=[Job(loc, pickup=[1], service=30, label=f"bin {loc}") for loc in range(2, N)],
        coords=[(0, 0)] * N,
    )


def solve(p: dict):
    return vroom.solve(p)


class TestMultiTrip(unittest.TestCase):
    """The rounds with the real VROOM runner, no valhalla."""

    @classmethod
    def setUpClass(cls):
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def types(self, route):
        return [s["type"] for s in route["steps"]]

    def test_trips_until_done(self):
        solution = solve_multi_trip(problem(), MATRICES, [1], DUMP, True, solve)
        self.assertEqual(solution["unassigned"], [])
        (route,) = solution["routes"]
        # 6 bins, 2 per load: three trips, each ending at the landfill, then home
        self.assertEqual(route["trips"], 3)
        self.assertEqual(self.types(route), ["start"] + ["job", "job", LANDFILL] * 3 + ["end"])
        steps = route["steps"]
        self.assertEqual([s["location_index"] for s in steps if s["type"] == LANDFILL], [1, 1, 1])
        self.assertEqual(steps[-1]["location_index"], 0)
        # time only moves forward, and every trip starts after unloading
        arrivals = [s["arrival"] for s in steps]
        self.assertEqual(arrivals, sorted(arrivals))
        for n, s in enumerate(steps[:-1]):
            if s["type"] == LANDFILL:
                self.assertEqual(s["load"], [0])
                self.assertGreaterEqual(steps[n + 1]["arrival"], s["arrival"] + DUMP)
        self.assertEqual(sorted(s["id"] for s in steps if s["type"] == "job"), list(range(6)))

    def test_shift_end(self):
        """No trip may end too late to unload and get home."""
        solution = solve_multi_trip(problem(shift=[0, 1500]), MATRICES, [1], DUMP, True, solve)
        (route,) = solution["routes"]
        self.assertLessEqual(route["steps"][-1]["arrival"], 1500)
        self.assertGreater(len(solution["unassigned"]), 0)
        self.assertEqual(
            len(solution["unassigned"]) + sum(1 for s in route["steps"] if s["type"] == "job"), 6
        )

    def test_one_break(self):
        solution = solve_multi_trip(problem(break_duration=600), MATRICES, [1], DUMP, True, solve)
        (route,) = solution["routes"]
        self.assertEqual(self.types(route).count("break"), 1)

    def test_nearest_landfill_and_no_return(self):
        """Two landfills: each trip ends at the one nearest to where the truck is."""
        p = problem()
        p.jobs = [Job(loc, pickup=[1], label=f"bin {loc}") for loc in (2, 3, 5, 6)]
        solution = solve_multi_trip(p, MATRICES, [1, 7], DUMP, False, solve)
        (route,) = solution["routes"]
        self.assertEqual(self.types(route)[-1], LANDFILL)  # no way home asked for
        self.assertEqual(route["steps"][0]["type"], "start")

    def test_two_trucks_share(self):
        p = problem()
        p.vehicles.append(Vehicle("t2", 0, "truck", capacity=[2]))
        solution = solve_multi_trip(p, MATRICES, [1], DUMP, True, solve)
        self.assertEqual(solution["unassigned"], [])
        self.assertEqual(sum(r["trips"] for r in solution["routes"]), 3)

    def test_validation(self):
        with self.assertRaisesRegex(ValueError, "landfill"):
            solve_multi_trip(problem(), MATRICES, [], DUMP, True, solve)
        p = problem()
        p.jobs[0].shipment_id, p.jobs[0].role = 1, "pickup"
        with self.assertRaisesRegex(ValueError, "pickup and delivery"):
            solve_multi_trip(p, MATRICES, [1], DUMP, True, solve)

    def test_canceled(self):
        self.assertIsNone(solve_multi_trip(problem(), MATRICES, [1], DUMP, True, lambda p: None))
