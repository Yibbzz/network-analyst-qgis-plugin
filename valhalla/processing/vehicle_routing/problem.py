"""
The vehicle routing problem as plain data: parsing attributes, building VROOM's input JSON
and splitting requests to a server's limits. No layers, no Processing, no Valhalla: tested
on its own (tests/test_localhost_plugin/test_utils/test_vrp_problem.py).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QDateTime, QTime

from ...global_definitions import RouterProfile

# an open time window end; VROOM's times are uint32, keep clear of overflows when it adds up
OPEN_END = 2**31 - 1
# the cost of a matrix cell Valhalla couldn't route (~115 days), so VROOM avoids it
UNROUTABLE = 10_000_000
_TIME_RE = re.compile(r"^\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\s*$")


def is_null(value: Any) -> bool:
    """
    NULL attributes are None, or a null QVariant/QTime/QDateTime on QGIS 3. Blank text counts
    as NULL too: a text column from a CSV has "" where there's no value.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    is_null_fn = getattr(value, "isNull", None)
    return bool(is_null_fn()) if callable(is_null_fn) else False


def to_seconds(value: Any) -> Optional[int]:  # noqa: C901 - a flat list of types
    """
    A time attribute as integer seconds, None for NULL. Numbers are seconds already, a time
    of day (QTime, "08:30", "08:30:15") counts from midnight, a date & time from the Unix
    epoch. All times of one run must use the same kind, VROOM only compares numbers.

    :raises ValueError: for anything else
    """
    if is_null(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"'{value}' is not a time")
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, QTime):
        return value.msecsSinceStartOfDay() // 1000
    if isinstance(value, QDateTime):
        return int(value.toSecsSinceEpoch())
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, str):
        if match := _TIME_RE.match(value):
            hours, minutes, seconds = (int(g or 0) for g in match.groups())
            return hours * 3600 + minutes * 60 + seconds
        try:
            return int(round(float(value)))
        except ValueError:
            pass

    raise ValueError(f"'{value}' is not a time: use seconds, HH:MM[:SS] or a time/date-time field")


def to_amount(value: Any) -> Optional[List[int]]:
    """
    A capacity/delivery/pickup attribute as VROOM amount, None for NULL. A number is one
    dimension, a comma separated text several (e.g. "10, 2" for weight and pallets).

    :raises ValueError: for negative or non-numeric values
    """
    if is_null(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"'{value}' is not an amount")
    if isinstance(value, (int, float)):
        parts = [value]
    else:
        parts = [p for p in str(value).split(",") if p.strip()]
    try:
        amount = [int(round(float(p))) for p in parts]
    except ValueError:
        raise ValueError(f"'{value}' is not an amount: use a number or comma separated numbers")
    if any(a < 0 for a in amount):
        raise ValueError(f"'{value}' is negative")

    return amount


def to_skills(value: Any) -> Optional[List[int]]:
    """A skills attribute (an integer or comma separated integers) as VROOM skills."""
    amount = to_amount(value)
    return sorted(set(amount)) if amount is not None else None


def to_int(value: Any, what: str) -> Optional[int]:
    if is_null(value):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"'{value}' is not a valid {what}")


def time_window(start: Optional[int], end: Optional[int]) -> Optional[List[int]]:
    """A VROOM time window, open at the side that's None; None if both are."""
    if start is None and end is None:
        return None
    start = 0 if start is None else start
    end = OPEN_END if end is None else end
    if start < 0 or end < start:
        raise ValueError(f"the time window {start} to {end} is invalid")

    return [start, end]


def seconds_to_hms(seconds: Optional[int]) -> Optional[str]:
    """HH:MM:SS for durations and times of day; None for epoch times (> 10 days)."""
    if seconds is None or seconds < 0 or seconds >= 10 * 86400:
        return None
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def pad_amounts(amounts: Sequence[Optional[List[int]]], dims: int) -> List[List[int]]:
    """VROOM wants every amount with the same number of dimensions."""
    return [(a or []) + [0] * (dims - len(a or [])) for a in amounts]


def matrix_row_chunks(n_locations: int, max_pairs: int) -> List[List[int]]:
    """
    The source rows per matrix request so that rows x all targets stay within Valhalla's
    max_matrix_location_pairs.

    :raises ValueError: if not even one full row fits
    """
    if n_locations > max_pairs:
        raise ValueError(
            f"{n_locations} locations need at least {n_locations} matrix pairs per request, "
            f"but the limit is {max_pairs}. Raise max_matrix_location_pairs on your own "
            "Valhalla server and the matching parameter here."
        )
    rows = max(1, max_pairs // n_locations)
    return [list(range(i, min(i + rows, n_locations))) for i in range(0, n_locations, rows)]


def route_chunks(indices: Sequence[int], max_locations: int) -> List[List[int]]:
    """
    Consecutive stops split into route requests of at most ``max_locations``, each starting
    where the last one ended. Repeated consecutive stops (two jobs at one place) are dropped.
    """
    if max_locations < 2:
        raise ValueError("A route request needs at least 2 locations")
    stops = [i for n, i in enumerate(indices) if n == 0 or i != indices[n - 1]]
    if len(stops) < 2:
        return []
    chunks, start = list(), 0
    while start < len(stops) - 1:
        chunks.append(stops[start : start + max_locations])
        start += max_locations - 1

    return chunks


# a vehicle's own skill, given to the jobs locked onto it; user skills must stay below
LOCK_SKILL_BASE = 1_000_000_000
PICKUP, DELIVERY = "pickup", "delivery"

_PROFILE_ALIASES = {
    "auto": RouterProfile.CAR,
    "car": RouterProfile.CAR,
    "truck": RouterProfile.TRUCK,
    "bus": RouterProfile.BUS,
    "bicycle": RouterProfile.BIKE,
    "bike": RouterProfile.BIKE,
    "pedestrian": RouterProfile.PED,
    "walk": RouterProfile.PED,
    "foot": RouterProfile.PED,
    "motorcycle": RouterProfile.MBIKE,
    "motorbike": RouterProfile.MBIKE,
}


def to_profile(value: Any) -> Optional[str]:
    """A travel mode attribute as Valhalla costing name, None for NULL/empty."""
    if is_null(value) or not str(value).strip():
        return None
    try:
        return _PROFILE_ALIASES[str(value).strip().lower()].value
    except KeyError:
        raise ValueError(
            f"'{value}' is not a travel mode, use one of {', '.join(sorted(_PROFILE_ALIASES))}"
        )


def to_role(value: Any) -> Optional[str]:
    """A shipment role attribute: pickup or delivery."""
    if is_null(value) or not str(value).strip():
        return None
    role = str(value).strip().lower()
    if role not in (PICKUP, DELIVERY):
        raise ValueError(f"'{value}' is not a shipment role, use 'pickup' or 'delivery'")
    return role


def unreachable_jobs(problem: "Problem", matrices: "Matrices", return_to_start: bool) -> List[int]:
    """
    The jobs (positions in ``problem.jobs``) no vehicle can get to and back from, on its own
    profile's matrix. Both parts of a shipment count as unreachable unless one vehicle can do
    pickup, then delivery.
    """

    def reachable(vehicle: Vehicle, *locations: int) -> bool:
        durations = matrices[vehicle.profile][0]
        path = [vehicle.location, *locations] + ([vehicle.location] if return_to_start else [])
        return all(durations[a][b] is not None for a, b in zip(path, path[1:]))

    unreachable = list()
    for k, job in enumerate(problem.jobs):
        if job.shipment_id is None:
            locations = [job.location]
        else:
            pickup, delivery = problem.shipment(job.shipment_id)
            locations = [problem.jobs[pickup].location, problem.jobs[delivery].location]
        if not any(reachable(v, *locations) for v in problem.vehicles):
            unreachable.append(k)

    return unreachable


def vroom_matrix(rows: List[List[Optional[float]]], keep: Sequence[int]) -> List[List[int]]:
    """The submatrix of the ``keep`` locations, as integers, unroutable cells very expensive."""
    return [[UNROUTABLE if rows[i][j] is None else int(round(rows[i][j])) for j in keep] for i in keep]


@dataclass
class Vehicle:
    vehicle_id: Any
    location: int
    profile: str  # a Valhalla costing, e.g. "auto"
    capacity: Optional[List[int]] = None
    shift: Optional[List[int]] = None
    skills: Optional[List[int]] = None
    max_tasks: Optional[int] = None
    break_window: Optional[List[int]] = None
    break_duration: Optional[int] = None
    feature: Any = None  # the input feature, for the outputs
    # where the route ends; None = back at the start if returning, else wherever it ends
    end_location: Optional[int] = None


@dataclass
class Job:
    location: int
    service: Optional[int] = None
    delivery: Optional[List[int]] = None
    pickup: Optional[List[int]] = None
    window: Optional[List[int]] = None
    skills: Optional[List[int]] = None
    priority: Optional[int] = None
    shipment_id: Any = None  # two jobs with one shipment ID are a pickup and its delivery
    role: Optional[str] = None  # PICKUP or DELIVERY, for shipments
    vehicle_id: Any = None  # locked onto this vehicle
    sequence: Optional[int] = None  # position in the vehicle's route, for plan mode
    label: str = ""  # for messages
    feature: Any = None


# per profile: (durations, distances) between all of Problem.coords
Matrices = Dict[str, Tuple[List[List[Optional[float]]], List[List[Optional[float]]]]]


@dataclass
class Problem:
    vehicles: List[Vehicle] = field(default_factory=list)
    jobs: List[Job] = field(default_factory=list)
    coords: List[Tuple[float, float]] = field(default_factory=list)  # WGS84, by location index

    def profiles(self) -> List[str]:
        return sorted({v.profile for v in self.vehicles})

    def shipment(self, shipment_id: Any) -> Tuple[int, int]:
        """The (pickup, delivery) job positions of a shipment."""
        return self.shipments()[shipment_id]

    def shipments(self) -> Dict[Any, Tuple[int, int]]:
        """
        Every shipment's (pickup, delivery) job positions.

        :raises ValueError: unless each shipment ID has exactly one pickup and one delivery
        """
        parts: Dict[Any, Dict[str, List[int]]] = dict()
        for k, job in enumerate(self.jobs):
            if job.shipment_id is None:
                continue
            if job.role is None:
                raise ValueError(f"{job.label} is part of shipment {job.shipment_id} but has no role")
            parts.setdefault(job.shipment_id, {PICKUP: [], DELIVERY: []})[job.role].append(k)
        shipments = dict()
        for shipment_id, roles in parts.items():
            if len(roles[PICKUP]) != 1 or len(roles[DELIVERY]) != 1:
                raise ValueError(
                    f"Shipment {shipment_id} needs exactly one pickup and one delivery, it has "
                    f"{len(roles[PICKUP])} and {len(roles[DELIVERY])}"
                )
            shipments[shipment_id] = (roles[PICKUP][0], roles[DELIVERY][0])
        return shipments


def _shipment_amount(pickup: Job, delivery: Job) -> Optional[List[int]]:
    """A shipment carries the delivery amount of its pickup or delivery job (equal if both)."""
    amounts = [a for a in (pickup.delivery, delivery.delivery) if a]
    if len(amounts) == 2 and amounts[0] != amounts[1]:
        raise ValueError(
            f"{pickup.label} and {delivery.label} are one shipment with different amounts "
            f"{amounts[0]} and {amounts[1]}"
        )
    return amounts[0] if amounts else None


def build_vroom_problem(  # noqa: C901
    problem: Problem,
    return_to_start: bool,
    matrices: Matrices,
    skip_jobs: Sequence[int] = (),
    plan: bool = False,
) -> Tuple[dict, List[int], List[int]]:
    """
    VROOM's input JSON for ``problem``, the location indices it kept (VROOM's location index i
    is ``keep[i]``) and the jobs left out of plan mode for having no vehicle or sequence.

    Vehicle i has VROOM id i; job k has VROOM id k, as do a shipment's pickup k and delivery
    k' (the jobs' positions), so the solution maps straight back onto the lists. Jobs locked
    onto a vehicle require that vehicle's own skill. ``plan`` keeps the locked vehicles and
    the jobs' sequence and lets VROOM only compute the times (``vroom.solve(plan=True)``).

    :raises ValueError: on inconsistent input
    """
    shipments = problem.shipments()
    skip = set(skip_jobs)
    # IDs compare as text: the job's field needn't have the vehicle ID field's type
    vehicle_index = {str(v.vehicle_id): i for i, v in enumerate(problem.vehicles)}

    # the vehicle each job is locked onto; a shipment's parts share it
    locked: Dict[int, int] = dict()
    for k, job in enumerate(problem.jobs):
        if job.vehicle_id is None:
            continue
        if str(job.vehicle_id) not in vehicle_index:
            raise ValueError(f"{job.label} is assigned to vehicle {job.vehicle_id}, which doesn't exist")
        locked[k] = vehicle_index[str(job.vehicle_id)]
    for pickup, delivery in shipments.values():
        assigned = {locked[k] for k in (pickup, delivery) if k in locked}
        if len(assigned) > 1:
            raise ValueError(
                f"{problem.jobs[pickup].label} and {problem.jobs[delivery].label} are one shipment "
                "assigned to different vehicles"
            )
        if assigned:
            locked[pickup] = locked[delivery] = assigned.pop()

    unplanned: List[int] = []
    if plan:
        unplanned = [
            k
            for k, job in enumerate(problem.jobs)
            if k not in skip and (k not in locked or job.sequence is None)
        ]
        # a shipment is only planned whole
        for pickup, delivery in shipments.values():
            if pickup in unplanned or delivery in unplanned:
                unplanned += [k for k in (pickup, delivery) if k not in unplanned]
        skip |= set(unplanned)

    tasks = [k for k in range(len(problem.jobs)) if k not in skip]
    keep = sorted(
        {v.location for v in problem.vehicles}
        | {v.end_location for v in problem.vehicles if v.end_location is not None}
        | {problem.jobs[k].location for k in tasks}
    )
    new_index = {old: new for new, old in enumerate(keep)}

    single = [k for k in tasks if problem.jobs[k].shipment_id is None]
    pairs = [(p, d) for p, d in shipments.values() if p not in skip]
    amounts = [problem.jobs[k].delivery for k in single] + [problem.jobs[k].pickup for k in single]
    amounts += [_shipment_amount(problem.jobs[p], problem.jobs[d]) for p, d in pairs]
    has_amounts = any(amounts)
    has_capacity = any(v.capacity for v in problem.vehicles)
    if has_amounts and not all(v.capacity for v in problem.vehicles):
        raise ValueError(
            "Jobs have delivery or pickup amounts, so every vehicle needs a capacity"
            if not has_capacity
            else "Some vehicles have no capacity, set it for all of them"
        )
    dims = max([len(v.capacity or []) for v in problem.vehicles] + [len(a or []) for a in amounts] + [0])
    use_amounts = bool(dims and has_amounts)

    for job in problem.jobs:
        if any(s >= LOCK_SKILL_BASE for s in job.skills or []):
            raise ValueError(f"{job.label}: skills must be below {LOCK_SKILL_BASE}")

    vehicles = list()
    for i, v in enumerate(problem.vehicles):
        vehicle = {"id": i, "profile": v.profile, "start_index": new_index[v.location]}
        if v.end_location is not None:
            vehicle["end_index"] = new_index[v.end_location]
        elif return_to_start:
            vehicle["end_index"] = new_index[v.location]
        if dims and has_capacity:
            vehicle["capacity"] = pad_amounts([v.capacity], dims)[0]
        if v.shift:
            vehicle["time_window"] = v.shift
        skills = list(v.skills or []) + ([LOCK_SKILL_BASE + i] if locked else [])
        if skills:
            vehicle["skills"] = skills
        if v.max_tasks is not None:
            vehicle["max_tasks"] = v.max_tasks
        if v.break_duration and not plan:  # plan mode only knows breaks in the steps
            window = v.break_window or v.shift or [0, OPEN_END]
            vehicle["breaks"] = [{"id": i, "time_windows": [window], "service": v.break_duration}]
        if plan:
            steps = sorted((problem.jobs[k].sequence, k) for k in tasks if locked.get(k) == i)
            types = {k: problem.jobs[k].role or "job" for k in tasks}
            vehicle["steps"] = (
                [{"type": "start"}]
                + [{"type": types[k], "id": k} for _, k in steps]
                + ([{"type": "end"}] if return_to_start or v.end_location is not None else [])
            )
        vehicles.append(vehicle)

    def task(k: int) -> dict:
        job = problem.jobs[k]
        out = {"id": k, "location_index": new_index[job.location]}
        if job.service:
            out["service"] = job.service
        if job.window:
            out["time_windows"] = [job.window]
        return out

    def requirements(*ks: int) -> dict:
        jobs = [problem.jobs[k] for k in ks]
        skills = sorted({s for job in jobs for s in job.skills or []})
        if ks[0] in locked:
            skills.append(LOCK_SKILL_BASE + locked[ks[0]])
        priorities = [job.priority for job in jobs if job.priority is not None]
        return {
            **({"skills": skills} if skills else {}),
            **({"priority": max(priorities)} if priorities else {}),
        }

    vroom_jobs = list()
    for k in single:
        job = problem.jobs[k]
        vroom_job = {**task(k), **requirements(k)}
        if use_amounts:
            vroom_job["delivery"], vroom_job["pickup"] = pad_amounts([job.delivery, job.pickup], dims)
        vroom_jobs.append(vroom_job)

    vroom_shipments = list()
    for p, d in pairs:
        shipment = {"pickup": task(p), "delivery": task(d), **requirements(p, d)}
        if use_amounts:
            shipment["amount"] = pad_amounts([_shipment_amount(problem.jobs[p], problem.jobs[d])], dims)[
                0
            ]
        vroom_shipments.append(shipment)

    return (
        {
            "vehicles": vehicles,
            "jobs": vroom_jobs,
            "shipments": vroom_shipments,
            "matrices": {
                profile: {
                    "durations": vroom_matrix(matrices[profile][0], keep),
                    "distances": vroom_matrix(matrices[profile][1], keep),
                }
                for profile in problem.profiles()
            },
        },
        keep,
        unplanned,
    )
