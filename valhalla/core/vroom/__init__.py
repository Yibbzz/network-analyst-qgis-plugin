"""
Vehicle routing via pyvroom, solved out of process: runner.py runs under
pypi.python_exe() with only the pyvroom tree (pypi.vroom_root_dir()) on PYTHONPATH.

Never import pyvroom in-process: it brings its own numpy/pandas, which clash with the host
numpy already in sys.modules, and a long solve couldn't be canceled.
"""

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from ...exceptions import PyPiError, VroomError
from .. import pypi

# stdlib-only at module level, safe to import in-process: the protocol lives there
from .runner import SolveRequest, SolveResponse

RUNNER = Path(__file__).parent.joinpath("runner.py")


def solve(
    problem: dict,
    exploration_level: int = 5,
    threads: int = 4,
    timeout: Optional[float] = None,
    is_canceled: Callable[[], bool] = lambda: False,
) -> Optional[dict]:
    """
    Solves a VROOM problem and returns VROOM's solution JSON.

    :param problem: VROOM input JSON; must carry its own "matrices", there's no routing server
    :param exploration_level: 1 (fast) to 5 (thorough)
    :param threads: number of threads VROOM may use
    :param timeout: seconds after which VROOM returns its best solution so far, None = no limit
    :param is_canceled: polled while solving, e.g. a QgsFeedback's isCanceled
    :returns: the solution, None if canceled
    :raises VroomError: if pyvroom isn't installed or the solve failed
    """
    if not pypi.is_installed(pypi.VROOM_PKG):
        raise VroomError(
            f"{pypi.VROOM_PKG.pypi_name} is not installed, install it in the plugin settings"
        )

    request = SolveRequest(
        problem=problem, exploration_level=exploration_level, threads=threads, timeout=timeout
    )

    # only the pyvroom tree, not the host's PYTHONPATH: nothing from the QGIS env may mix in
    env = {**os.environ, "PYTHONPATH": str(pypi.vroom_root_dir())}
    try:
        proc = pypi.run_python(
            [RUNNER], stdin=json.dumps(asdict(request)), env=env, is_canceled=is_canceled
        )
    except PyPiError as e:
        raise VroomError(str(e), detail=e.detail)

    if proc is None:
        return None

    detail = f"{' '.join(proc.args)}\n\nexit code {proc.returncode}\n\n{proc.stderr}"
    try:
        response = SolveResponse(**json.loads(proc.stdout))
    except (ValueError, TypeError):
        # the runner died before it could answer, e.g. a crash in a native lib
        raise VroomError(f"The VROOM solver crashed with exit code {proc.returncode}", detail=detail)

    if proc.returncode != 0 or response.error or response.solution is None:
        raise VroomError(f"VROOM failed: {response.error or 'unknown error'}", detail=detail)

    return response.solution
