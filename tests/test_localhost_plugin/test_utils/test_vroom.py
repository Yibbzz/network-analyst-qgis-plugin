import json
import os
import unittest
from dataclasses import asdict

from tests.utilities import get_qgis_app

QGIS_APP, CANVAS, IFACE, PARENT = get_qgis_app()

from valhalla.core import pypi, vroom  # noqa: E402
from valhalla.core.vroom import SolveRequest  # noqa: E402
from valhalla.exceptions import VroomError  # noqa: E402

# 0 = depot, 1..3 = jobs on a line: 0 - 1 - 2 - 3
MATRIX = [
    [0, 10, 20, 30],
    [10, 0, 10, 20],
    [20, 10, 0, 10],
    [30, 20, 10, 0],
]


def problem(**vehicle) -> dict:
    return {
        "vehicles": [{"id": 0, "start_index": 0, "end_index": 0, **vehicle}],
        "jobs": [{"id": i, "location_index": i, "delivery": [1]} for i in (1, 2, 3)],
        "matrices": {"car": {"durations": MATRIX}},
    }


class TestVroom(unittest.TestCase):
    """Runs the real runner subprocess, needs no valhalla, installs pyvroom if missing."""

    @classmethod
    def setUpClass(cls):
        if not pypi.is_installed(pypi.VROOM_PKG):
            pypi.install(pypi.VROOM_PKG, pypi.PyPiState.NOT_INSTALLED)

    def test_solve(self):
        solution = vroom.solve(problem(capacity=[3]))
        self.assertEqual(solution["summary"]["unassigned"], 0)
        steps = solution["routes"][0]["steps"]
        self.assertEqual([s["type"] for s in steps], ["start", "job", "job", "job", "end"])
        # out and back along the line
        self.assertEqual(solution["routes"][0]["duration"], 60)

    def test_capacity_leaves_jobs_unassigned(self):
        solution = vroom.solve(problem(capacity=[2]))
        self.assertEqual(len(solution["unassigned"]), 1)

    def test_bad_problem(self):
        with self.assertRaises(VroomError) as cm:
            vroom.solve({"vehicles": [], "jobs": [], "matrices": {}})
        self.assertIn("vehicle", str(cm.exception).lower())

    def test_canceled(self):
        self.assertIsNone(vroom.solve(problem(capacity=[3]), is_canceled=lambda: True))

    def test_runner_protocol(self):
        """Unknown keys are refused, never silently ignored."""
        request = {**asdict(SolveRequest(problem=problem())), "unknown": 1}
        env = {**os.environ, "PYTHONPATH": str(pypi.vroom_root_dir())}
        proc = pypi.run_python([vroom.RUNNER], stdin=json.dumps(request), env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown", json.loads(proc.stdout)["error"])

    def test_not_installed(self):
        real_root = pypi.vroom_root_dir
        pypi.vroom_root_dir = lambda: real_root().with_name("does_not_exist")
        try:
            with self.assertRaises(VroomError):
                vroom.solve(problem())
        finally:
            pypi.vroom_root_dir = real_root
