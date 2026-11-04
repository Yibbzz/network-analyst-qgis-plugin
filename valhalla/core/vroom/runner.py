"""
Solves one vehicle routing problem with pyvroom. Runs as a subprocess of the plugin
(see core/vroom/__init__.py), under pypi.python_exe() with the pyvroom tree on PYTHONPATH.

Standalone on purpose: no plugin imports, stdlib + pyvroom only.

The protocol is defined once, here: stdin is a JSON SolveRequest, stdout a JSON
SolveResponse, exit 0 if solved, else 1. The problem and the solution are VROOM's own
documented JSON formats (https://github.com/VROOM-Project/vroom/blob/master/docs/API.md),
so nothing here needs to know about jobs, vehicles or matrices. The client imports this
module in-process for these types, so everything but the stdlib must only be imported
inside solve().
"""

import contextlib
import json
import sys
import tempfile
import traceback
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional


@dataclass
class SolveRequest:
    problem: dict  # VROOM input JSON, with "matrices" (no routing server)
    exploration_level: int = 5  # 1..5, VROOM's -x
    threads: int = 4  # VROOM's -t
    timeout: Optional[float] = None  # seconds, None = no limit
    # plan mode: keep the vehicles' "steps" as given and only compute the times (VROOM's -c)
    plan: bool = False


@dataclass
class SolveResponse:
    solution: Optional[dict] = None  # VROOM output JSON
    error: Optional[str] = None


class InputError(Exception):
    pass


def solve(req: SolveRequest) -> SolveResponse:
    try:
        import vroom
    except ImportError as e:
        raise InputError(f"pyvroom is not installed properly: {e}")

    if not 1 <= int(req.exploration_level) <= 5:
        raise InputError(f"The exploration level must be 1 to 5, not {req.exploration_level}")

    with tempfile.TemporaryDirectory() as tmp:
        # pyvroom only reads and writes VROOM's JSON formats from files
        problem_path = Path(tmp, "problem.json")
        solution_path = Path(tmp, "solution.json")
        problem_path.write_text(json.dumps(req.problem))
        try:
            problem = vroom.Input.from_json(problem_path)
            if req.plan:
                solution = problem.check(nb_threads=int(req.threads))
            else:
                solution = problem.solve(
                    exploration_level=int(req.exploration_level),
                    nb_threads=int(req.threads),
                    timeout=timedelta(seconds=req.timeout) if req.timeout else None,
                )
        except Exception as e:  # noqa: BLE001 - VroomInputException & co, all bad input
            raise InputError(str(e) or type(e).__name__)

        solution.to_json(solution_path)
        return SolveResponse(solution=json.loads(solution_path.read_text()))


def main() -> int:
    stdout = sys.stdout
    try:
        # a missing or unknown key is a TypeError
        req = SolveRequest(**json.load(sys.stdin))
        # stdout is the result channel, nothing else may print to it
        with contextlib.redirect_stdout(sys.stderr):
            response = solve(req)
    except (InputError, ValueError, TypeError) as e:
        # the caller logs stderr as detail
        traceback.print_exc()
        json.dump(asdict(SolveResponse(error=str(e))), stdout)
        return 1

    json.dump(asdict(response), stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
