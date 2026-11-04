"""
Vehicle routing (VRP) with VROOM on Valhalla cost matrices.

Valhalla supplies one time/distance matrix between all vehicle starts and jobs, VROOM
(core/vroom, out of process) assigns and orders the jobs, and Valhalla routes each
vehicle's stops to draw its line. VROOM never talks to Valhalla itself: the matrix is
requested through the plugin's own provider + costing options, like every other algorithm.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsFeature,
    QgsFeatureSource,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDateTime, QTime, QVariant
from qgis.PyQt.QtGui import QIcon

from ...core import vroom
from ...exceptions import VroomError
from ...global_definitions import RouterEndpoint, RouterProfile, RouterType
from ...third_party.routingpy import routingpy
from ...utils.geom_utils import WGS84
from ...utils.layer_utils import get_wgs_coords_from_feature
from ...utils.misc_utils import wrap_in_html_tag
from ...utils.resource_utils import get_icon
from ..processing_definitions import HELP_DIR
from ..routing.base_algorithm import ValhallaBaseAlgorithm
from ..spatial_optimization.base_algorithm import SpoptBaseAlgorithm

# an open time window end; VROOM's times are uint32, keep clear of overflows when it adds up
OPEN_END = 2**31 - 1
# the cost of a matrix cell Valhalla couldn't route (~115 days), so VROOM avoids it
UNROUTABLE = 10_000_000
_TIME_RE = re.compile(r"^\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\s*$")


class FieldNames:
    """The VRP outputs' result fields."""

    VEHICLE_ID = "vehicle_id"
    SEQUENCE = "sequence"
    TYPE = "type"
    ARRIVAL = "arrival"
    ARRIVAL_HMS = "arrival_hms"
    DEPARTURE = "departure"
    SERVICE = "service"
    WAITING = "waiting"
    LOAD = "load"
    STOPS = "stops"
    DURATION = "duration"
    DISTANCE = "distance"
    START = "start"
    END = "end"
    DELIVERY = "delivery"
    PICKUP = "pickup"
    REASON = "reason"


# ---------------------------------------------------------------------------------------
# attribute parsing, no QGIS layers involved (unit-tested on its own)
# ---------------------------------------------------------------------------------------


def is_null(value: Any) -> bool:
    """NULL attributes are None, or a null QVariant/QTime/QDateTime on QGIS 3."""
    if value is None:
        return True
    is_null_fn = getattr(value, "isNull", None)
    return bool(is_null_fn()) if callable(is_null_fn) else False


def to_seconds(value: Any) -> Optional[int]:
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


def unreachable_jobs(
    durations: List[List[Optional[float]]],
    vehicle_starts: Sequence[int],
    vehicle_ends: Sequence[Optional[int]],
    job_locations: Sequence[int],
) -> List[int]:
    """The jobs (as positions in ``job_locations``) no vehicle can get to and back from."""
    unreachable = list()
    for job, loc in enumerate(job_locations):
        if not any(
            durations[start][loc] is not None and (end is None or durations[loc][end] is not None)
            for start, end in zip(vehicle_starts, vehicle_ends)
        ):
            unreachable.append(job)

    return unreachable


def vroom_matrix(rows: List[List[Optional[float]]], keep: Sequence[int]) -> List[List[int]]:
    """The submatrix of the ``keep`` locations, as integers, unroutable cells very expensive."""
    return [[UNROUTABLE if rows[i][j] is None else int(round(rows[i][j])) for j in keep] for i in keep]


@dataclass
class Vehicle:
    feature: QgsFeature
    vehicle_id: Any
    location: int
    capacity: Optional[List[int]] = None
    shift: Optional[List[int]] = None
    skills: Optional[List[int]] = None
    max_tasks: Optional[int] = None
    break_window: Optional[List[int]] = None
    break_duration: Optional[int] = None


@dataclass
class Job:
    feature: QgsFeature
    location: int
    service: Optional[int] = None
    delivery: Optional[List[int]] = None
    pickup: Optional[List[int]] = None
    window: Optional[List[int]] = None
    skills: Optional[List[int]] = None
    priority: Optional[int] = None


@dataclass
class Problem:
    vehicles: List[Vehicle] = field(default_factory=list)
    jobs: List[Job] = field(default_factory=list)
    coords: List[Tuple[float, float]] = field(default_factory=list)  # WGS84, by location index


def build_vroom_problem(
    problem: Problem,
    profile: str,
    return_to_start: bool,
    durations: List[List[Optional[float]]],
    distances: List[List[Optional[float]]],
    skip_jobs: Sequence[int] = (),
) -> Tuple[dict, List[int]]:
    """
    VROOM's input JSON for ``problem``, and the location indices it kept (VROOM's location
    index i is ``keep[i]``). Vehicle i has VROOM id i, job k has VROOM id k, so the solution
    maps straight back onto the lists. The skipped jobs' locations are left out.

    :raises ValueError: on inconsistent amounts
    """
    skip = set(skip_jobs)
    jobs = [(k, job) for k, job in enumerate(problem.jobs) if k not in skip]
    keep = sorted({v.location for v in problem.vehicles} | {job.location for _, job in jobs})
    new_index = {old: new for new, old in enumerate(keep)}

    has_amounts = any(job.delivery or job.pickup for _, job in jobs)
    has_capacity = any(v.capacity for v in problem.vehicles)
    if has_amounts and not all(v.capacity for v in problem.vehicles):
        raise ValueError(
            "Jobs have delivery or pickup amounts, so every vehicle needs a capacity"
            if not has_capacity
            else "Some vehicles have no capacity, set it for all of them"
        )
    dims = max(
        [len(v.capacity or []) for v in problem.vehicles]
        + [len(job.delivery or []) for _, job in jobs]
        + [len(job.pickup or []) for _, job in jobs]
        + [0]
    )

    vehicles = list()
    for i, v in enumerate(problem.vehicles):
        vehicle = {"id": i, "profile": profile, "start_index": new_index[v.location]}
        if return_to_start:
            vehicle["end_index"] = new_index[v.location]
        if dims and has_capacity:
            vehicle["capacity"] = pad_amounts([v.capacity], dims)[0]
        if v.shift:
            vehicle["time_window"] = v.shift
        if v.skills:
            vehicle["skills"] = v.skills
        if v.max_tasks is not None:
            vehicle["max_tasks"] = v.max_tasks
        if v.break_duration:
            window = v.break_window or v.shift or [0, OPEN_END]
            vehicle["breaks"] = [{"id": i, "time_windows": [window], "service": v.break_duration}]
        vehicles.append(vehicle)

    vroom_jobs = list()
    for k, job in jobs:
        vroom_job = {"id": k, "location_index": new_index[job.location]}
        if job.service:
            vroom_job["service"] = job.service
        if dims and has_amounts:
            vroom_job["delivery"], vroom_job["pickup"] = pad_amounts([job.delivery, job.pickup], dims)
        if job.window:
            vroom_job["time_windows"] = [job.window]
        if job.skills:
            vroom_job["skills"] = job.skills
        if job.priority is not None:
            vroom_job["priority"] = job.priority
        vroom_jobs.append(vroom_job)

    return {
        "vehicles": vehicles,
        "jobs": vroom_jobs,
        "matrices": {
            profile: {
                "durations": vroom_matrix(durations, keep),
                "distances": vroom_matrix(distances, keep),
            }
        },
    }, keep


# ---------------------------------------------------------------------------------------
# the Processing algorithm
# ---------------------------------------------------------------------------------------


class ValhallaVrpBase(ValhallaBaseAlgorithm):
    """
    The jobs are the base class' input point layer (IN_1, IN_FIELD_1), the vehicles a second
    point layer whose points are where they start (and end, unless told otherwise).
    """

    IN_JOB_SERVICE = "INPUT_JOB_SERVICE"
    IN_JOB_DELIVERY = "INPUT_JOB_DELIVERY"
    IN_JOB_PICKUP = "INPUT_JOB_PICKUP"
    IN_JOB_TW_START = "INPUT_JOB_TW_START"
    IN_JOB_TW_END = "INPUT_JOB_TW_END"
    IN_JOB_SKILLS = "INPUT_JOB_SKILLS"
    IN_JOB_PRIORITY = "INPUT_JOB_PRIORITY"

    IN_VEH = "INPUT_VEHICLES"
    IN_VEH_ID = "INPUT_VEHICLE_ID"
    IN_VEH_CAPACITY = "INPUT_VEHICLE_CAPACITY"
    IN_VEH_TW_START = "INPUT_VEHICLE_SHIFT_START"
    IN_VEH_TW_END = "INPUT_VEHICLE_SHIFT_END"
    IN_VEH_SKILLS = "INPUT_VEHICLE_SKILLS"
    IN_VEH_MAX_TASKS = "INPUT_VEHICLE_MAX_TASKS"
    IN_VEH_BREAK_START = "INPUT_VEHICLE_BREAK_START"
    IN_VEH_BREAK_END = "INPUT_VEHICLE_BREAK_END"
    IN_VEH_BREAK_DURATION = "INPUT_VEHICLE_BREAK_DURATION"
    IN_RETURN = "INPUT_RETURN_TO_START"

    IN_EXPLORATION = "INPUT_EXPLORATION_LEVEL"
    IN_TIMEOUT = "INPUT_TIMEOUT"
    IN_THREADS = "INPUT_THREADS"
    IN_MAX_PAIRS = "INPUT_MAX_MATRIX_PAIRS"
    IN_MAX_LOCATIONS = "INPUT_MAX_ROUTE_LOCATIONS"

    OUT_STOPS = "OUTPUT_STOPS"
    OUT_UNASSIGNED = "OUTPUT_UNASSIGNED"

    def __init__(self, profile: RouterProfile):
        super().__init__(provider=RouterType.VALHALLA, endpoint=RouterEndpoint.MATRIX, profile=profile)

    def initAlgorithm(self, configuration, p_str=None, Any=None, *args, **kwargs):
        self.init_base_params()
        self.parameterDefinition(self.IN_1).setDescription(
            f"{wrap_in_html_tag('Jobs', 'b')}: point layer of the stops to serve"
        )
        self.parameterDefinition(self.IN_FIELD_1).setDescription(
            "Job ID field (copied to the outputs anyway, used in messages)"
        )
        self.parameterDefinition(self.OUT).setDescription("vrp_routes")

        for name, desc, field_type in (
            (self.IN_JOB_SERVICE, "Job service time field (seconds)", "numeric"),
            (self.IN_JOB_DELIVERY, "Job delivery amount field (number, or comma separated)", "any"),
            (self.IN_JOB_PICKUP, "Job pickup amount field (number, or comma separated)", "any"),
            (self.IN_JOB_TW_START, "Job time window start field (seconds, HH:MM or time)", "any"),
            (self.IN_JOB_TW_END, "Job time window end field (seconds, HH:MM or time)", "any"),
            (self.IN_JOB_SKILLS, "Job required skills field (integers, comma separated)", "any"),
            (self.IN_JOB_PRIORITY, "Job priority field (0 to 100, higher is served first)", "numeric"),
        ):
            self._add_field_param(name, desc, self.IN_1, field_type)

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                name=self.IN_VEH,
                description=f"{wrap_in_html_tag('Vehicles', 'b')}: point layer of where each vehicle starts",
                types=[QgsProcessing.SourceType.TypeVectorPoint],
            )
        )
        for name, desc, field_type in (
            (self.IN_VEH_ID, "Vehicle ID field (feature IDs if empty)", "any"),
            (self.IN_VEH_CAPACITY, "Vehicle capacity field (number, or comma separated)", "any"),
            (self.IN_VEH_TW_START, "Vehicle shift start field (seconds, HH:MM or time)", "any"),
            (self.IN_VEH_TW_END, "Vehicle shift end field (seconds, HH:MM or time)", "any"),
            (self.IN_VEH_SKILLS, "Vehicle skills field (integers, comma separated)", "any"),
            (self.IN_VEH_MAX_TASKS, "Vehicle maximum number of jobs field", "numeric"),
            (self.IN_VEH_BREAK_START, "Vehicle break window start field", "any"),
            (self.IN_VEH_BREAK_END, "Vehicle break window end field", "any"),
            (self.IN_VEH_BREAK_DURATION, "Vehicle break duration field (seconds)", "numeric"),
        ):
            self._add_field_param(name, desc, self.IN_VEH, field_type)

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.IN_RETURN, "Vehicles return to where they started", defaultValue=True
            )
        )

        for name, desc, default, minimum, maximum in (
            (self.IN_EXPLORATION, "Solver exploration level (1 fast to 5 thorough)", 5, 1, 5),
            (self.IN_TIMEOUT, "Solver time limit in seconds (0 = none)", 0, 0, None),
            (self.IN_THREADS, "Solver threads", 4, 1, 64),
            (self.IN_MAX_PAIRS, "Max matrix pairs per request (the server's limit)", 2500, 1, None),
            (self.IN_MAX_LOCATIONS, "Max locations per route request (the server's limit)", 20, 2, None),
        ):
            param = QgsProcessingParameterNumber(
                name,
                desc,
                type=QgsProcessingParameterNumber.Type.Integer,
                defaultValue=default,
                minValue=minimum,
                **({"maxValue": maximum} if maximum is not None else {}),
            )
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(param)

        self.addParameter(
            QgsProcessingParameterFeatureSink(self.OUT_STOPS, "vrp_stops", createByDefault=True)
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUT_UNASSIGNED, "vrp_unassigned", createByDefault=True
            )
        )

    def _add_field_param(self, name: str, desc: str, parent: str, field_type: str):
        types = {
            "numeric": QgsProcessingParameterField.DataType.Numeric,
            "any": QgsProcessingParameterField.DataType.Any,
        }
        self.addParameter(
            QgsProcessingParameterField(
                name, desc, parentLayerParameterName=parent, type=types[field_type], optional=True
            )
        )

    # flake8: noqa: C901
    def processAlgorithm(
        self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback
    ):
        jobs_source, job_id_field, params, results_factory = self.get_base_params(parameters, context)
        veh_source = self.parameterAsSource(parameters, self.IN_VEH, context)
        for source, name in ((jobs_source, self.IN_1), (veh_source, self.IN_VEH)):
            if source is None:
                raise QgsProcessingException(self.invalidSourceError(parameters, name))
        return_to_start = self.parameterAsBoolean(parameters, self.IN_RETURN, context)
        max_pairs = self.parameterAsInt(parameters, self.IN_MAX_PAIRS, context)
        max_locations = self.parameterAsInt(parameters, self.IN_MAX_LOCATIONS, context)

        problem = self._read_problem(parameters, context, jobs_source, job_id_field, veh_source)
        if not problem.jobs:
            raise QgsProcessingException("The jobs layer has no features")
        if not problem.vehicles:
            raise QgsProcessingException("The vehicles layer has no features")
        feedback.pushInfo(f"{len(problem.jobs)} jobs, {len(problem.vehicles)} vehicles")

        # 1. one matrix between all locations, in chunks of source rows
        try:
            chunks = matrix_row_chunks(len(problem.coords), max_pairs)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        durations: List[List[Optional[float]]] = list()
        distances: List[List[Optional[float]]] = list()
        all_targets = list(range(len(problem.coords)))
        for n, rows in enumerate(chunks):
            if feedback.isCanceled():
                return {}
            matrix = self._request(
                results_factory,
                RouterEndpoint.MATRIX,
                problem.coords,
                {**params, "sources": rows, "destinations": all_targets},
            )
            durations.extend(matrix.durations)
            distances.extend(matrix.distances)
            feedback.setProgress(int(40 * (n + 1) / len(chunks)))

        # 2. solve, without the jobs no vehicle can get to
        vehicle_ends = [v.location if return_to_start else None for v in problem.vehicles]
        unreachable = unreachable_jobs(
            durations,
            [v.location for v in problem.vehicles],
            vehicle_ends,
            [job.location for job in problem.jobs],
        )
        if unreachable:
            feedback.reportError(
                f"{len(unreachable)} jobs can't be reached by any vehicle, "
                "they're in the unassigned output",
                fatalError=False,
            )
        if len(unreachable) == len(problem.jobs):
            raise QgsProcessingException("No job can be reached by any vehicle")

        profile = self.profile.lower()
        try:
            vroom_problem, keep = build_vroom_problem(
                problem, profile, return_to_start, durations, distances, unreachable
            )
        except ValueError as e:
            raise QgsProcessingException(str(e))

        try:
            solution = vroom.solve(
                vroom_problem,
                exploration_level=self.parameterAsInt(parameters, self.IN_EXPLORATION, context),
                threads=self.parameterAsInt(parameters, self.IN_THREADS, context),
                timeout=self.parameterAsInt(parameters, self.IN_TIMEOUT, context) or None,
                is_canceled=feedback.isCanceled,
            )
        except VroomError as e:
            feedback.pushDebugInfo(e.detail)
            raise QgsProcessingException(str(e))
        if solution is None:  # canceled
            return {}
        feedback.setProgress(70)
        summary = solution.get("summary", {})
        feedback.pushInfo(
            f"VROOM: {summary.get('routes', 0)} routes, {summary.get('unassigned', 0)} jobs unassigned"
        )

        # 3. outputs: routes with their Valhalla geometry, stops, unassigned jobs
        sinks = self._create_sinks(parameters, context, jobs_source, job_id_field, veh_source)
        routes_sink, routes_dest, routes_fields, veh_copied = sinks["routes"]
        stops_sink, stops_dest, stops_fields, job_copied = sinks["stops"]
        unassigned_sink, unassigned_dest, unassigned_fields, _ = sinks["unassigned"]

        routes = solution.get("routes", [])
        for n, route in enumerate(routes):
            if feedback.isCanceled():
                return {}
            vehicle = problem.vehicles[route["vehicle"]]
            steps = route.get("steps", [])
            # VROOM's location indices are into the submatrix ``keep``
            stop_locations = [keep[s["location_index"]] for s in steps if "location_index" in s]

            geometry, distance = self._route_geometry(
                results_factory, params, problem.coords, stop_locations, max_locations
            )
            n_jobs = sum(1 for s in steps if s["type"] in ("job", "pickup", "delivery"))
            route_feat = QgsFeature(routes_fields)
            veh_attrs = vehicle.feature.attributes()
            route_feat.setAttributes(
                [veh_attrs[i] for i in veh_copied]
                + [
                    vehicle.vehicle_id,
                    n_jobs,
                    route.get("duration"),
                    distance,
                    route.get("service"),
                    route.get("waiting_time"),
                    steps[0].get("arrival") if steps else None,
                    steps[-1].get("arrival") if steps else None,
                    ",".join(map(str, route.get("delivery", []))) or None,
                    ",".join(map(str, route.get("pickup", []))) or None,
                ]
            )
            if geometry is not None:
                route_feat.setGeometry(geometry)
            routes_sink.addFeature(route_feat)

            last_location = vehicle.location
            for seq, step in enumerate(steps):
                location = keep[step["location_index"]] if "location_index" in step else last_location
                last_location = location
                arrival = step.get("arrival")
                service = step.get("service", 0)
                waiting = step.get("waiting_time", 0)
                is_job = step["type"] in ("job", "pickup", "delivery")
                if is_job:
                    job_attrs = problem.jobs[step["id"]].feature.attributes()
                    copied = [job_attrs[i] for i in job_copied]
                else:  # start, end, break: no job to copy from
                    copied = [None] * len(job_copied)
                stop_feat = QgsFeature(stops_fields)
                stop_feat.setAttributes(
                    copied
                    + [
                        vehicle.vehicle_id,
                        seq,
                        step["type"],
                        arrival,
                        seconds_to_hms(arrival),
                        None if arrival is None else arrival + waiting + service,
                        service,
                        waiting,
                        ",".join(map(str, step.get("load", []))) or None,
                    ]
                )
                stop_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*problem.coords[location])))
                stops_sink.addFeature(stop_feat)

            feedback.setProgress(70 + int(30 * (n + 1) / max(len(routes), 1)))

        unassigned = [(k, "unreachable") for k in unreachable] + [
            (u["id"], "not assigned") for u in solution.get("unassigned", [])
        ]
        for k, reason in unassigned:
            job = problem.jobs[k]
            feat = QgsFeature(unassigned_fields)
            attrs = job.feature.attributes()
            feat.setAttributes([attrs[i] for i in job_copied] + [reason])
            feat.setGeometry(job.feature.geometry())
            unassigned_sink.addFeature(feat)

        return {
            self.OUT: routes_dest,
            self.OUT_STOPS: stops_dest,
            self.OUT_UNASSIGNED: unassigned_dest,
        }

    def _read_problem(
        self,
        parameters,
        context,
        jobs_source: QgsFeatureSource,
        job_id_field: str,
        veh_source: QgsFeatureSource,
    ) -> Problem:
        """Reads vehicles (location indices first) and jobs, validating every attribute."""
        field = lambda name: self.parameterAsString(parameters, name, context)  # noqa: E731
        problem = Problem()

        def read(feature: QgsFeature, name: str, parse: Callable[[Any], Any], what: str):
            field_name = field(name)
            if not field_name:
                return None
            try:
                return parse(feature[field_name])
            except ValueError as e:
                raise QgsProcessingException(f"{what}, field '{field_name}': {e}")

        def add_location(feature: QgsFeature, crs, what: str) -> int:
            if not feature.hasGeometry() or feature.geometry().isEmpty():
                raise QgsProcessingException(f"{what} has no geometry")
            coords = get_wgs_coords_from_feature(feature, crs)
            if coords and isinstance(coords[0], list):  # a MultiPoint
                coords = coords[0]
            problem.coords.append(tuple(coords[:2]))
            return len(problem.coords) - 1

        veh_id_field = field(self.IN_VEH_ID)
        seen_ids = set()
        for feat in veh_source.getFeatures():
            veh_id = feat[veh_id_field] if veh_id_field else feat.id()
            what = f"Vehicle {veh_id}"
            if veh_id in seen_ids:
                raise QgsProcessingException(
                    f"{what} appears twice, the vehicle ID field must be unique"
                )
            seen_ids.add(veh_id)
            try:
                shift = time_window(
                    read(feat, self.IN_VEH_TW_START, to_seconds, what),
                    read(feat, self.IN_VEH_TW_END, to_seconds, what),
                )
                break_window = time_window(
                    read(feat, self.IN_VEH_BREAK_START, to_seconds, what),
                    read(feat, self.IN_VEH_BREAK_END, to_seconds, what),
                )
            except ValueError as e:
                raise QgsProcessingException(f"{what}: {e}")
            problem.vehicles.append(
                Vehicle(
                    feature=feat,
                    vehicle_id=veh_id,
                    location=add_location(feat, veh_source.sourceCrs(), what),
                    capacity=read(feat, self.IN_VEH_CAPACITY, to_amount, what),
                    shift=shift,
                    skills=read(feat, self.IN_VEH_SKILLS, to_skills, what),
                    max_tasks=read(feat, self.IN_VEH_MAX_TASKS, lambda v: to_int(v, "count"), what),
                    break_window=break_window,
                    break_duration=read(
                        feat, self.IN_VEH_BREAK_DURATION, lambda v: to_int(v, "duration"), what
                    ),
                )
            )

        for feat in jobs_source.getFeatures():
            what = f"Job {feat[job_id_field] if job_id_field else feat.id()}"
            try:
                window = time_window(
                    read(feat, self.IN_JOB_TW_START, to_seconds, what),
                    read(feat, self.IN_JOB_TW_END, to_seconds, what),
                )
            except ValueError as e:
                raise QgsProcessingException(f"{what}: {e}")
            priority = read(feat, self.IN_JOB_PRIORITY, lambda v: to_int(v, "priority"), what)
            if priority is not None and not 0 <= priority <= 100:
                raise QgsProcessingException(f"{what}: the priority must be 0 to 100, not {priority}")
            problem.jobs.append(
                Job(
                    feature=feat,
                    location=add_location(feat, jobs_source.sourceCrs(), what),
                    service=read(feat, self.IN_JOB_SERVICE, lambda v: to_int(v, "duration"), what),
                    delivery=read(feat, self.IN_JOB_DELIVERY, to_amount, what),
                    pickup=read(feat, self.IN_JOB_PICKUP, to_amount, what),
                    window=window,
                    skills=read(feat, self.IN_JOB_SKILLS, to_skills, what),
                    priority=priority,
                )
            )

        return problem

    def _create_sinks(self, parameters, context, jobs_source, job_id_field, veh_source) -> dict:
        """The three outputs; each starts with the attributes of the layer it describes."""
        output_fields = SpoptBaseAlgorithm._output_fields
        veh_id = SpoptBaseAlgorithm._id_field(
            FieldNames.VEHICLE_ID,
            veh_source,
            self.parameterAsString(parameters, self.IN_VEH_ID, context),
        )

        route_result = QgsFields()
        route_result.append(veh_id)
        route_result.append(QgsField(FieldNames.STOPS, QVariant.Int))
        for name in (FieldNames.DURATION, FieldNames.DISTANCE, FieldNames.SERVICE, FieldNames.WAITING):
            route_result.append(QgsField(name, QVariant.Double))
        for name in (FieldNames.START, FieldNames.END):
            route_result.append(QgsField(name, QVariant.LongLong))
        for name in (FieldNames.DELIVERY, FieldNames.PICKUP):
            route_result.append(QgsField(name, QVariant.String))
        routes_fields, veh_copied = output_fields(veh_source, route_result)

        stop_result = QgsFields()
        stop_result.append(QgsField(veh_id))
        stop_result.append(QgsField(FieldNames.SEQUENCE, QVariant.Int))
        stop_result.append(QgsField(FieldNames.TYPE, QVariant.String))
        stop_result.append(QgsField(FieldNames.ARRIVAL, QVariant.LongLong))
        stop_result.append(QgsField(FieldNames.ARRIVAL_HMS, QVariant.String))
        stop_result.append(QgsField(FieldNames.DEPARTURE, QVariant.LongLong))
        stop_result.append(QgsField(FieldNames.SERVICE, QVariant.Int))
        stop_result.append(QgsField(FieldNames.WAITING, QVariant.Int))
        stop_result.append(QgsField(FieldNames.LOAD, QVariant.String))
        stops_fields, job_copied = output_fields(jobs_source, stop_result)

        unassigned_result = QgsFields()
        unassigned_result.append(QgsField(FieldNames.REASON, QVariant.String))
        unassigned_fields, job_copied_2 = output_fields(jobs_source, unassigned_result)
        assert job_copied == job_copied_2  # the same input fields, both skip only "fid"

        sinks = dict()
        for key, name, fields, geom_type, crs, copied in (
            ("routes", self.OUT, routes_fields, QgsWkbTypes.Type.LineString, WGS84, veh_copied),
            ("stops", self.OUT_STOPS, stops_fields, QgsWkbTypes.Type.Point, WGS84, job_copied),
            (
                "unassigned",
                self.OUT_UNASSIGNED,
                unassigned_fields,
                jobs_source.wkbType(),
                jobs_source.sourceCrs(),
                job_copied,
            ),
        ):
            sink, dest = self.parameterAsSink(parameters, name, context, fields, geom_type, crs)
            if sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, name))
            sinks[key] = (sink, dest, fields, copied)

        return sinks

    def _route_geometry(
        self,
        results_factory,
        params: dict,
        coords: List[Tuple[float, float]],
        stop_locations: List[int],
        max_locations: int,
    ) -> Tuple[Optional[QgsGeometry], Optional[float]]:
        """One line through the stops in order, routed in chunks; its length in meters."""
        points: List[QgsPointXY] = list()
        distance = 0.0
        chunks = route_chunks(stop_locations, max_locations)
        if not chunks:
            return None, 0.0
        for chunk in chunks:
            direction = self._request(
                results_factory, RouterEndpoint.DIRECTIONS, [coords[i] for i in chunk], params
            )
            line = [QgsPointXY(*c[:2]) for c in direction.geometry]
            # each chunk starts where the previous ended
            points.extend(line[1:] if points else line)
            distance += direction.distance or 0

        return QgsGeometry.fromPolylineXY(points), distance

    @staticmethod
    def _request(results_factory, endpoint: RouterEndpoint, coords, params: dict):
        try:
            return results_factory.router.request(endpoint, [list(c) for c in coords], **params)
        except (
            routingpy.exceptions.RouterApiError,
            routingpy.exceptions.RouterServerError,
        ) as e:
            raise QgsProcessingException(f"HTTP {e.status}: {e.message}")

    def group(self):
        return "Vehicle Routing"

    def groupId(self):
        return "valhalla_vehicle_routing"

    def name(self):
        return f"valhalla_vrp_{self.profile.lower()}"

    def displayName(self):
        return f"VRP {self.profile.capitalize()}"

    def icon(self) -> QIcon:
        return get_icon("optimized_directions_icon.svg")

    def shortHelpString(self):
        with open(HELP_DIR / Path("valhalla_vrp.help")) as fh:
            return fh.read()


class ValhallaVrpCar(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.CAR)


class ValhallaVrpTruck(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.TRUCK)


class ValhallaVrpMotorcycle(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.MBIKE)


class ValhallaVrpBicycle(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.BIKE)


class ValhallaVrpPedestrian(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.PED)


class ValhallaVrpBus(ValhallaVrpBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.BUS)
