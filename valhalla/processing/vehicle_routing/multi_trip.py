"""
Multi-trip routing (waste collection): vehicles empty out at a landfill whenever they're full
and carry on with the next trip, until the jobs or their shifts run out.

VROOM has no reloads, so this solves in rounds. In each round every vehicle that's still
going gets one trip, from where it is (its depot, then a landfill) to the landfill nearest
to it, starting when it's free and ending early enough to unload and still get home before
its shift ends. The jobs served are removed, the vehicles move on to their landfill, and the
next round starts. It's a heuristic: each round packs as many jobs as it can, which is what a
dispatcher planning trip by trip would do too, but it never trades jobs between trips.

Plain data in, VROOM's solution format out (with "landfill" steps and location indices into
Problem.coords), so the outputs are written by the same code as a normal VRP.
"""

from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .problem import OPEN_END, Matrices, Problem, build_vroom_problem

LANDFILL = "landfill"
JOB_STEPS = ("job", "pickup", "delivery")


@dataclass
class _Truck:
    index: int  # in problem.vehicles
    location: int
    time: int
    shift_end: int
    home: Optional[int]
    break_pending: bool
    steps: List[dict]
    landfill: Optional[int] = None
    trips: int = 0
    done: bool = False
    # travel so far, against the vehicle's max travel time / distance over all its trips
    travel_time: int = 0
    travel_distance: int = 0


def _add(a: Optional[List[int]], b: Optional[List[int]]) -> List[int]:
    a, b = a or [], b or []
    n = max(len(a), len(b))
    return [(a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0) for i in range(n)]


def solve_multi_trip(  # noqa: C901 - one loop over rounds
    problem: Problem,
    matrices: Matrices,
    landfills: Sequence[int],
    dump_time: int,
    return_to_start: bool,
    solve: Callable[[dict], Optional[dict]],
    skip_jobs: Sequence[int] = (),
    max_trips: int = 10,
    is_canceled: Callable[[], bool] = lambda: False,
) -> Optional[dict]:
    """
    Solves ``problem`` in trips that each end at a landfill.

    :param landfills: location indices (into problem.coords) of the landfills
    :param dump_time: seconds to unload at a landfill
    :param solve: solves one VROOM problem, e.g. ``lambda p: vroom.solve(p, ...)``; None = canceled
    :param skip_jobs: jobs to leave out, e.g. unreachable ones
    :param max_trips: per vehicle
    :returns: a VROOM-like solution: one route per vehicle with all its trips, "landfill" steps
        in between, ``location_index`` into problem.coords; the jobs left over in "unassigned".
        None if canceled.
    :raises ValueError: on shipments, or no landfill
    """
    if not landfills:
        raise ValueError("Waste collection needs at least one landfill")
    if any(job.shipment_id is not None for job in problem.jobs):
        raise ValueError("Waste collection has no pickup and delivery pairs")

    remaining: Set[int] = {k for k in range(len(problem.jobs)) if k not in set(skip_jobs)}
    trucks = [
        _Truck(
            index=i,
            location=v.location,
            time=v.shift[0] if v.shift else 0,
            shift_end=v.shift[1] if v.shift else OPEN_END,
            home=v.end(return_to_start),
            break_pending=bool(v.break_duration),
            steps=[],
        )
        for i, v in enumerate(problem.vehicles)
    ]
    totals: Dict[int, Dict[str, object]] = {
        t.index: {
            "duration": 0,
            "service": 0,
            "waiting_time": 0,
            "cost": 0,
            "pickup": [],
            "delivery": [],
        }
        for t in trucks
    }

    def time(profile: str, a: int, b: int) -> Optional[float]:
        return matrices[profile][0][a][b]

    def distance(profile: str, a: int, b: int) -> float:
        return 0 if b is None else matrices[profile][1][a][b] or 0

    def budget(limit: Optional[int], used: int, home: float) -> Optional[int]:
        """What's left of a max travel time / distance for this trip, keeping the way home."""
        return None if limit is None else int(round(limit - used - home))  # VROOM wants integers

    for _ in range(max_trips):
        if not remaining:
            break
        round_trucks: List[_Truck] = list()
        round_vehicles = list()
        for truck in trucks:
            if truck.done:
                continue
            vehicle = problem.vehicles[truck.index]
            # the landfill nearest to where the truck is, from which it can still get home
            options = list()
            for landfill in landfills:
                there = time(vehicle.profile, truck.location, landfill)
                home = 0 if truck.home is None else time(vehicle.profile, landfill, truck.home)
                if there is not None and home is not None:
                    options.append((there, home, landfill))
            if not options:
                truck.done = True
                continue
            _, home, landfill = min(options)
            latest = truck.shift_end - dump_time - int(round(home))
            if latest <= truck.time:
                truck.done = True
                continue
            # the trip may use what's left of the day's travel, minus the way home
            travel_left = budget(vehicle.max_travel_time, truck.travel_time, home)
            home_m = distance(vehicle.profile, landfill, truck.home)
            distance_left = budget(vehicle.max_distance, truck.travel_distance, home_m)
            if any(left is not None and left <= 0 for left in (travel_left, distance_left)):
                truck.done = True
                continue
            truck.landfill = landfill
            round_trucks.append(truck)
            round_vehicles.append(
                replace(
                    vehicle,
                    location=truck.location,
                    end_location=landfill,
                    shift=[truck.time, latest],
                    break_duration=vehicle.break_duration if truck.break_pending else None,
                    max_travel_time=travel_left,
                    max_distance=distance_left,
                    max_tasks=(
                        None
                        if vehicle.max_tasks is None
                        else vehicle.max_tasks - sum(1 for s in truck.steps if s["type"] in JOB_STEPS)
                    ),
                )
            )
        if not round_trucks:
            break

        sub = Problem(vehicles=round_vehicles, jobs=problem.jobs, coords=problem.coords)
        skip = [k for k in range(len(problem.jobs)) if k not in remaining]
        vroom_problem, keep, _ = build_vroom_problem(sub, False, matrices, skip)
        if is_canceled():
            return None
        solution = solve(vroom_problem)
        if solution is None:
            return None

        used = set()
        for route in solution.get("routes", []):
            truck = round_trucks[route["vehicle"]]
            used.add(truck.index)
            steps = route.get("steps", [])
            for step in steps:
                step = dict(step)
                if "location_index" in step:
                    step["location_index"] = keep[step["location_index"]]
                if step["type"] == "start" and truck.trips:
                    continue  # the previous trip's landfill step is where this one starts
                if step["type"] == "end":
                    # arriving at the landfill: unload, the load is empty afterwards
                    step.update(
                        type=LANDFILL,
                        location_index=truck.landfill,
                        service=dump_time,
                        load=[0] * len(step.get("load", [])),
                    )
                    truck.time = step["arrival"] + dump_time
                if step["type"] == "break":
                    truck.break_pending = False
                if step["type"] in JOB_STEPS:
                    remaining.discard(step["id"])
                truck.steps.append(step)
            truck.location = truck.landfill
            truck.trips += 1
            truck.travel_time += route.get("duration", 0)
            # VROOM's output has no distance: add it up along the trip from the matrix
            stops = [keep[step["location_index"]] for step in steps if "location_index" in step]
            truck.travel_distance += sum(
                distance(problem.vehicles[truck.index].profile, a, b) for a, b in zip(stops, stops[1:])
            )
            total = totals[truck.index]
            total["duration"] += route.get("duration", 0)
            total["cost"] += route.get("cost", 0)
            total["service"] += route.get("service", 0) + dump_time
            total["waiting_time"] += route.get("waiting_time", 0)
            total["pickup"] = _add(total["pickup"], route.get("pickup"))
            total["delivery"] = _add(total["delivery"], route.get("delivery"))

        # a truck that got nothing this round won't later: it only gets later and further away
        for truck in round_trucks:
            if truck.index not in used:
                truck.done = True
        if not used:
            break

    routes = list()
    for truck in trucks:
        if not truck.trips:
            continue
        vehicle = problem.vehicles[truck.index]
        total = totals[truck.index]
        if truck.home is not None:
            back = int(round(time(vehicle.profile, truck.location, truck.home)))
            loads = truck.steps[-1].get("load", [])
            truck.steps.append(
                {
                    "type": "end",
                    "location_index": truck.home,
                    "arrival": truck.time + back,
                    "duration": truck.steps[-1].get("duration", 0) + back,
                    "service": 0,
                    "waiting_time": 0,
                    "load": loads,
                    "violations": [],
                }
            )
            total["duration"] += back
        routes.append(
            {
                "vehicle": truck.index,
                "steps": truck.steps,
                "trips": truck.trips,
                "violations": [],
                **total,
            }
        )

    unassigned = [
        {"id": k, "location_index": problem.jobs[k].location, "type": "job"} for k in sorted(remaining)
    ]
    return {
        "code": 0,
        "summary": {"routes": len(routes), "unassigned": len(unassigned)},
        "routes": routes,
        "unassigned": unassigned,
    }


def identity_keep(problem: Problem) -> List[int]:
    """The multi-trip solution's location indices are already into problem.coords."""
    return list(range(len(problem.coords)))


__all__: Tuple[str, ...] = ("LANDFILL", "solve_multi_trip", "identity_keep")
