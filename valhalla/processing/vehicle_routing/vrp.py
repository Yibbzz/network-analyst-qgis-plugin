"""
Vehicle routing (VRP) with VROOM on Valhalla cost matrices.

Valhalla supplies a time/distance matrix between all vehicle starts and jobs (one per travel
mode in the fleet), VROOM (core/vroom, out of process) assigns and orders the jobs, and
Valhalla routes each vehicle's stops to draw its line. VROOM never talks to Valhalla itself:
the matrices are requested through the plugin's own provider + costing options, like every
other algorithm. The problem as plain data lives in problem.py.
"""

import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import (
    QgsDistanceArea,
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
from qgis.PyQt.QtCore import QVariant
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
from .problem import (
    Job,
    Matrices,
    Problem,
    Vehicle,
    build_vroom_problem,
    check_time_kinds,
    is_null,
    matrix_row_chunks,
    route_chunks,
    seconds_to_hms,
    server_limit,
    time_window,
    to_amount,
    to_int,
    to_non_negative,
    to_profile,
    to_role,
    to_seconds,
    to_skills,
    unreachable_jobs,
)

JOB_STEPS = ("job", "pickup", "delivery")


class FieldNames:
    """The VRP outputs' result fields."""

    VEHICLE_ID = "vehicle_id"
    PROFILE = "profile"
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
    COST = "cost"
    START = "start"
    END = "end"
    DELIVERY = "delivery"
    PICKUP = "pickup"
    VIOLATIONS = "violations"
    REASON = "reason"


class Reasons:
    UNREACHABLE = "unreachable"
    NOT_ASSIGNED = "not assigned"
    NOT_PLANNED = "no vehicle or sequence"


def violations_text(violations: List[dict]) -> Optional[str]:
    """VROOM's violations as text, e.g. "delay 600 s, load"; None if there are none."""
    parts = [
        f"{v['cause']} {v['duration']} s" if v.get("duration") else v["cause"] for v in violations or []
    ]
    return ", ".join(parts) or None


class ServerLimitError(QgsProcessingException):
    """The server refused a request for too many locations; ``limit`` is what it allows."""

    def __init__(self, message: str, limit: int):
        super().__init__(message)
        self.limit = limit


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
    IN_JOB_SHIPMENT = "INPUT_JOB_SHIPMENT_ID"
    IN_JOB_ROLE = "INPUT_JOB_SHIPMENT_ROLE"
    IN_JOB_VEHICLE = "INPUT_JOB_VEHICLE"
    IN_JOB_SEQUENCE = "INPUT_JOB_SEQUENCE"

    IN_VEH = "INPUT_VEHICLES"
    IN_VEH_ID = "INPUT_VEHICLE_ID"
    IN_VEH_PROFILE = "INPUT_VEHICLE_PROFILE"
    IN_VEH_CAPACITY = "INPUT_VEHICLE_CAPACITY"
    IN_VEH_TW_START = "INPUT_VEHICLE_SHIFT_START"
    IN_VEH_TW_END = "INPUT_VEHICLE_SHIFT_END"
    IN_VEH_SKILLS = "INPUT_VEHICLE_SKILLS"
    IN_VEH_MAX_TASKS = "INPUT_VEHICLE_MAX_TASKS"
    IN_VEH_BREAK_START = "INPUT_VEHICLE_BREAK_START"
    IN_VEH_BREAK_END = "INPUT_VEHICLE_BREAK_END"
    IN_VEH_BREAK_DURATION = "INPUT_VEHICLE_BREAK_DURATION"
    IN_VEH_FIXED_COST = "INPUT_VEHICLE_FIXED_COST"
    IN_VEH_COST_PER_HOUR = "INPUT_VEHICLE_COST_PER_HOUR"
    IN_VEH_COST_PER_KM = "INPUT_VEHICLE_COST_PER_KM"
    IN_VEH_MAX_TRAVEL_TIME = "INPUT_VEHICLE_MAX_TRAVEL_TIME"
    IN_VEH_MAX_DISTANCE = "INPUT_VEHICLE_MAX_DISTANCE"
    IN_VEH_ENDS = "INPUT_VEHICLE_ENDS"
    IN_VEH_END_ID = "INPUT_VEHICLE_END_ID"
    IN_RETURN = "INPUT_RETURN_TO_START"
    IN_KEEP_ORDER = "INPUT_KEEP_ORDER"

    IN_EXPLORATION = "INPUT_EXPLORATION_LEVEL"
    IN_TIMEOUT = "INPUT_TIMEOUT"
    IN_THREADS = "INPUT_THREADS"
    IN_MAX_PAIRS = "INPUT_MAX_MATRIX_PAIRS"
    IN_MAX_LOCATIONS = "INPUT_MAX_ROUTE_LOCATIONS"
    IN_FAR_POINTS = "INPUT_FAR_POINT_WARNING"

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

        for name, desc, field_type, advanced in (
            (self.IN_JOB_SERVICE, "Job service time field (seconds)", "numeric", False),
            (
                self.IN_JOB_DELIVERY,
                "Job delivery amount field (number, or comma separated)",
                "any",
                False,
            ),
            (self.IN_JOB_PICKUP, "Job pickup amount field (number, or comma separated)", "any", False),
            (self.IN_JOB_TW_START, "Job time window start field (seconds, HH:MM or time)", "any", False),
            (self.IN_JOB_TW_END, "Job time window end field (seconds, HH:MM or time)", "any", False),
            (self.IN_JOB_SKILLS, "Job required skills field (integers, comma separated)", "any", False),
            (
                self.IN_JOB_PRIORITY,
                "Job priority field (0 to 100, higher is served first)",
                "numeric",
                False,
            ),
            (
                self.IN_JOB_SHIPMENT,
                "Shipment ID field: two jobs with one ID are a pickup and its delivery",
                "any",
                True,
            ),
            (self.IN_JOB_ROLE, "Shipment role field: 'pickup' or 'delivery'", "any", True),
            (
                self.IN_JOB_VEHICLE,
                "Assigned vehicle field: keeps a job on this vehicle (its vehicle ID)",
                "any",
                True,
            ),
            (
                self.IN_JOB_SEQUENCE,
                "Sequence field: a job's position in its vehicle's route (for keeping the order)",
                "numeric",
                True,
            ),
        ):
            self._add_field_param(name, desc, self.IN_1, field_type, advanced)

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                name=self.IN_VEH,
                description=f"{wrap_in_html_tag('Vehicles', 'b')}: point layer of where each vehicle starts",
                types=[QgsProcessing.SourceType.TypeVectorPoint],
            )
        )
        for name, desc, field_type in (
            (self.IN_VEH_ID, "Vehicle ID field (feature IDs if empty)", "any"),
            (
                self.IN_VEH_PROFILE,
                "Vehicle travel mode field (auto, truck, bus, bicycle, pedestrian, motorcycle; "
                "empty = this algorithm's)",
                "any",
            ),
            (self.IN_VEH_CAPACITY, "Vehicle capacity field (number, or comma separated)", "any"),
            (self.IN_VEH_TW_START, "Vehicle shift start field (seconds, HH:MM or time)", "any"),
            (self.IN_VEH_TW_END, "Vehicle shift end field (seconds, HH:MM or time)", "any"),
            (self.IN_VEH_SKILLS, "Vehicle skills field (integers, comma separated)", "any"),
            (self.IN_VEH_MAX_TASKS, "Vehicle maximum number of jobs field", "numeric"),
            (self.IN_VEH_BREAK_START, "Vehicle break window start field", "any"),
            (self.IN_VEH_BREAK_END, "Vehicle break window end field", "any"),
            (self.IN_VEH_BREAK_DURATION, "Vehicle break duration field (seconds)", "numeric"),
        ):
            self._add_field_param(name, desc, self.IN_VEH, field_type, False)
        for name, desc in (
            (
                self.IN_VEH_FIXED_COST,
                "Vehicle fixed cost field: what using the vehicle at all costs (e.g. to use fewer)",
            ),
            (
                self.IN_VEH_COST_PER_HOUR,
                "Vehicle cost per hour of travel field (default 3600: the cost is the travel time)",
            ),
            (self.IN_VEH_COST_PER_KM, "Vehicle cost per km field (default 0)"),
            (
                self.IN_VEH_MAX_TRAVEL_TIME,
                "Vehicle maximum travel time field (seconds, service and waiting not counted)",
            ),
            (self.IN_VEH_MAX_DISTANCE, "Vehicle maximum distance field (meters)"),
        ):
            self._add_field_param(name, desc, self.IN_VEH, "numeric", True)

        ends = QgsProcessingParameterFeatureSource(
            name=self.IN_VEH_ENDS,
            description="Vehicle end points: where vehicles finish, if not where they started",
            types=[QgsProcessing.SourceType.TypeVectorPoint],
            optional=True,
        )
        ends.setFlags(ends.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(ends)
        self._add_field_param(
            self.IN_VEH_END_ID,
            "Vehicle ID field of the end points (the vehicle each one is for)",
            self.IN_VEH_ENDS,
            "any",
            True,
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.IN_RETURN, "Vehicles return to where they started", defaultValue=True
            )
        )
        keep_order = QgsProcessingParameterBoolean(
            self.IN_KEEP_ORDER,
            "Keep the assigned vehicles and sequence, only compute times (needs both fields)",
            defaultValue=False,
        )
        keep_order.setFlags(keep_order.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(keep_order)

        for name, desc, default, minimum, maximum in (
            (self.IN_EXPLORATION, "Solver exploration level (1 fast to 5 thorough)", 5, 1, 5),
            (self.IN_TIMEOUT, "Solver time limit in seconds (0 = none)", 0, 0, None),
            (self.IN_THREADS, "Solver threads", 4, 1, 64),
            (
                self.IN_MAX_PAIRS,
                "Max matrix pairs per request (lowered automatically if the server allows fewer)",
                2500,
                1,
                None,
            ),
            (
                self.IN_MAX_LOCATIONS,
                "Max locations per route request (lowered automatically if the server allows fewer)",
                20,
                2,
                None,
            ),
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

        far = QgsProcessingParameterNumber(
            self.IN_FAR_POINTS,
            "Warn about points farther than this from a road (meters, 0 = don't check)",
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=500,
            minValue=0,
        )
        far.setFlags(far.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(far)

        self.addParameter(
            QgsProcessingParameterFeatureSink(self.OUT_STOPS, "vrp_stops", createByDefault=True)
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUT_UNASSIGNED, "vrp_unassigned", createByDefault=True
            )
        )

    def _add_field_param(self, name: str, desc: str, parent: str, field_type: str, advanced: bool):
        types = {
            "numeric": QgsProcessingParameterField.DataType.Numeric,
            "any": QgsProcessingParameterField.DataType.Any,
        }
        param = QgsProcessingParameterField(
            name, desc, parentLayerParameterName=parent, type=types[field_type], optional=True
        )
        if advanced:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(param)

    def processAlgorithm(  # noqa: C901 - matrices, solve, three outputs
        self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback
    ):
        jobs_source, job_id_field, params, results_factory = self.get_base_params(parameters, context)
        veh_source = self.parameterAsSource(parameters, self.IN_VEH, context)
        for source, name in ((jobs_source, self.IN_1), (veh_source, self.IN_VEH)):
            if source is None:
                raise QgsProcessingException(self.invalidSourceError(parameters, name))
        return_to_start = self.parameterAsBoolean(parameters, self.IN_RETURN, context)
        plan = self.parameterAsBoolean(parameters, self.IN_KEEP_ORDER, context)
        max_pairs = self.parameterAsInt(parameters, self.IN_MAX_PAIRS, context)
        max_locations = self.parameterAsInt(parameters, self.IN_MAX_LOCATIONS, context)

        if plan and not all(
            self.parameterAsString(parameters, p, context)
            for p in (self.IN_JOB_VEHICLE, self.IN_JOB_SEQUENCE)
        ):
            raise QgsProcessingException(
                "Keeping the order needs the assigned vehicle and the sequence field"
            )
        if plan and self.parameterAsString(parameters, self.IN_VEH_BREAK_DURATION, context):
            feedback.reportError("Breaks are left out when keeping the order", fatalError=False)

        problem = self._read_problem(parameters, context, jobs_source, job_id_field, veh_source)
        if not problem.jobs:
            raise QgsProcessingException("The jobs layer has no features")
        if not problem.vehicles:
            raise QgsProcessingException("The vehicles layer has no features")
        try:
            problem.shipments()  # validates the pairs before any request
            check_time_kinds(problem)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        self._read_extra(parameters, context, problem)
        feedback.pushInfo(
            f"{len(problem.jobs)} jobs, {len(problem.vehicles)} vehicles ({', '.join(problem.profiles())})"
        )

        # the route lines adapt this to the server's limit, see _route_geometry
        self._max_locations = max_locations

        # Valhalla routes a point from its nearest road, however far: say so
        far_limit = self.parameterAsDouble(parameters, self.IN_FAR_POINTS, context)
        if far_limit > 0:
            self._warn_far_points(results_factory, problem, far_limit, feedback)
            if feedback.isCanceled():
                return {}

        # 1. a matrix between all locations per travel mode, in chunks of source rows
        matrices = self._matrices(results_factory, problem, params, max_pairs, feedback)
        if matrices is None:
            return {}

        # 2. solve, without the jobs no vehicle can get to
        unreachable = unreachable_jobs(problem, matrices, return_to_start)
        if unreachable:
            feedback.reportError(
                f"{len(unreachable)} jobs can't be reached by any vehicle, they're in the unassigned output",
                fatalError=False,
            )
        if len(unreachable) == len(problem.jobs):
            raise QgsProcessingException("No job can be reached by any vehicle")

        solved = self._solve(
            parameters, context, feedback, problem, matrices, unreachable, return_to_start, plan
        )
        if solved is None:  # canceled
            return {}
        solution, keep, unplanned = solved
        feedback.setProgress(70)
        # plan mode (VROOM's check) also returns every vehicle without a job, as an empty
        # start-end route; a solve leaves unused vehicles out: match that
        solution["routes"] = [
            r
            for r in solution.get("routes", [])
            if any(s.get("type") in JOB_STEPS for s in r.get("steps", []))
        ]
        summary = solution.get("summary", {})
        feedback.pushInfo(
            f"VROOM: {len(solution['routes'])} routes, {summary.get('unassigned', 0)} jobs unassigned"
        )
        if plan and (violations := violations_text(summary.get("violations"))):
            feedback.reportError(f"The kept routes break constraints: {violations}", fatalError=False)

        # 3. outputs: routes with their Valhalla geometry, stops, unassigned jobs
        sinks = self._create_sinks(parameters, context, jobs_source, veh_source)
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

            with self._profile(results_factory, vehicle.profile):
                geometry, distance = self._route_geometry(
                    results_factory,
                    self._profile_params(params, vehicle.profile),
                    problem.coords,
                    stop_locations,
                    feedback,
                )
            route_feat = QgsFeature(routes_fields)
            veh_attrs = vehicle.feature.attributes()
            route_feat.setAttributes(
                [veh_attrs[i] for i in veh_copied]
                + [
                    vehicle.vehicle_id,
                    vehicle.profile,
                    sum(1 for s in steps if s["type"] in JOB_STEPS),
                    route.get("duration"),
                    distance,
                    route.get("service"),
                    route.get("waiting_time"),
                    route.get("cost"),
                    steps[0].get("arrival") if steps else None,
                    steps[-1].get("arrival") if steps else None,
                    ",".join(map(str, route.get("delivery", []))) or None,
                    ",".join(map(str, route.get("pickup", []))) or None,
                    violations_text(route.get("violations")),
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
                if step["type"] in JOB_STEPS:
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
                        violations_text(step.get("violations")),
                    ]
                )
                stop_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*problem.coords[location])))
                stops_sink.addFeature(stop_feat)

            feedback.setProgress(70 + int(30 * (n + 1) / max(len(routes), 1)))

        # a shipment's two parts are both listed by VROOM
        unassigned = (
            [(k, Reasons.UNREACHABLE) for k in unreachable]
            + [(k, Reasons.NOT_PLANNED) for k in unplanned]
            + [(u["id"], Reasons.NOT_ASSIGNED) for u in solution.get("unassigned", [])]
        )
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

    def _read_extra(self, parameters, context, problem: Problem):
        """Adds what a subclass needs beyond jobs & vehicles, e.g. landfills, before the matrices."""

    def _vroom_solver(
        self, parameters, context, feedback, plan: bool = False
    ) -> Callable[[dict], Optional[dict]]:
        """Solves one VROOM problem with this run's solver settings; None if canceled."""

        def solve(vroom_problem: dict) -> Optional[dict]:
            try:
                return vroom.solve(
                    vroom_problem,
                    exploration_level=self.parameterAsInt(parameters, self.IN_EXPLORATION, context),
                    threads=self.parameterAsInt(parameters, self.IN_THREADS, context),
                    timeout=self.parameterAsInt(parameters, self.IN_TIMEOUT, context) or None,
                    plan=plan,
                    is_canceled=feedback.isCanceled,
                )
            except VroomError as e:
                feedback.pushDebugInfo(e.detail)
                raise QgsProcessingException(str(e))

        return solve

    def _solve(
        self,
        parameters,
        context,
        feedback,
        problem: Problem,
        matrices: Matrices,
        unreachable: List[int],
        return_to_start: bool,
        plan: bool,
    ) -> Optional[Tuple[dict, List[int], List[int]]]:
        """
        VROOM's solution, the location indices its ``location_index`` points into and the jobs
        left out of plan mode; None if canceled.
        """
        try:
            vroom_problem, keep, unplanned = build_vroom_problem(
                problem, return_to_start, matrices, unreachable, plan=plan
            )
        except ValueError as e:
            raise QgsProcessingException(str(e))

        solution = self._vroom_solver(parameters, context, feedback, plan)(vroom_problem)
        return None if solution is None else (solution, keep, unplanned)

    def _read_problem(  # noqa: C901 - one read per field
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

        def raw(value: Any) -> Any:
            """An ID attribute, None for NULL or empty text."""
            return None if is_null(value) or str(value).strip() == "" else value

        def cost(value):
            return to_non_negative(value, "cost")

        def duration(value):
            return to_non_negative(value, "duration")

        def distance(value):
            return to_non_negative(value, "distance")

        own_profile = self.profile.value
        veh_id_field = field(self.IN_VEH_ID)
        seen_ids = set()
        for feat in veh_source.getFeatures():
            veh_id = feat[veh_id_field] if veh_id_field else feat.id()
            what = f"Vehicle {veh_id}"
            if str(veh_id) in seen_ids:
                raise QgsProcessingException(
                    f"{what} appears twice, the vehicle ID field must be unique"
                )
            seen_ids.add(str(veh_id))
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
                    vehicle_id=veh_id,
                    location=add_location(feat, veh_source.sourceCrs(), what),
                    profile=read(feat, self.IN_VEH_PROFILE, to_profile, what) or own_profile,
                    capacity=read(feat, self.IN_VEH_CAPACITY, to_amount, what),
                    shift=shift,
                    skills=read(feat, self.IN_VEH_SKILLS, to_skills, what),
                    max_tasks=read(feat, self.IN_VEH_MAX_TASKS, lambda v: to_int(v, "count"), what),
                    break_window=break_window,
                    break_duration=read(
                        feat, self.IN_VEH_BREAK_DURATION, lambda v: to_int(v, "duration"), what
                    ),
                    fixed_cost=read(feat, self.IN_VEH_FIXED_COST, cost, what),
                    cost_per_hour=read(feat, self.IN_VEH_COST_PER_HOUR, cost, what),
                    cost_per_km=read(feat, self.IN_VEH_COST_PER_KM, cost, what),
                    max_travel_time=read(feat, self.IN_VEH_MAX_TRAVEL_TIME, duration, what),
                    max_distance=read(feat, self.IN_VEH_MAX_DISTANCE, distance, what),
                    feature=feat,
                )
            )

        ends = self.parameterAsSource(parameters, self.IN_VEH_ENDS, context)
        if ends is not None:
            end_id_field = field(self.IN_VEH_END_ID)
            if not end_id_field:
                raise QgsProcessingException(
                    "The vehicle end points need the field with their vehicle ID"
                )
            by_id = {str(v.vehicle_id): v for v in problem.vehicles}
            for feat in ends.getFeatures():
                end_id = raw(feat[end_id_field])
                what = f"The end point of vehicle {end_id}"
                if end_id is None:
                    raise QgsProcessingException(f"End point {feat.id()} has no vehicle ID")
                vehicle = by_id.get(str(end_id))
                if vehicle is None:
                    raise QgsProcessingException(f"{what}: there's no such vehicle")
                if vehicle.end_location is not None:
                    raise QgsProcessingException(f"Vehicle {end_id} has more than one end point")
                vehicle.end_location = add_location(feat, ends.sourceCrs(), what)

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
                    location=add_location(feat, jobs_source.sourceCrs(), what),
                    service=read(feat, self.IN_JOB_SERVICE, lambda v: to_int(v, "duration"), what),
                    delivery=read(feat, self.IN_JOB_DELIVERY, to_amount, what),
                    pickup=read(feat, self.IN_JOB_PICKUP, to_amount, what),
                    window=window,
                    skills=read(feat, self.IN_JOB_SKILLS, to_skills, what),
                    priority=priority,
                    shipment_id=read(feat, self.IN_JOB_SHIPMENT, raw, what),
                    role=read(feat, self.IN_JOB_ROLE, to_role, what),
                    vehicle_id=read(feat, self.IN_JOB_VEHICLE, raw, what),
                    sequence=read(feat, self.IN_JOB_SEQUENCE, lambda v: to_int(v, "sequence"), what),
                    label=what,
                    feature=feat,
                )
            )

        return problem

    def _create_sinks(self, parameters, context, jobs_source, veh_source) -> dict:
        """The three outputs; each starts with the attributes of the layer it describes."""
        output_fields = SpoptBaseAlgorithm._output_fields
        veh_id = SpoptBaseAlgorithm._id_field(
            FieldNames.VEHICLE_ID,
            veh_source,
            self.parameterAsString(parameters, self.IN_VEH_ID, context),
        )

        route_result = QgsFields()
        route_result.append(veh_id)
        route_result.append(QgsField(FieldNames.PROFILE, QVariant.String))
        route_result.append(QgsField(FieldNames.STOPS, QVariant.Int))
        for name in (FieldNames.DURATION, FieldNames.DISTANCE, FieldNames.SERVICE, FieldNames.WAITING):
            route_result.append(QgsField(name, QVariant.Double))
        route_result.append(QgsField(FieldNames.COST, QVariant.LongLong))
        for name in (FieldNames.START, FieldNames.END):
            route_result.append(QgsField(name, QVariant.LongLong))
        for name in (FieldNames.DELIVERY, FieldNames.PICKUP, FieldNames.VIOLATIONS):
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
        stop_result.append(QgsField(FieldNames.VIOLATIONS, QVariant.String))
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

    def _profile_params(self, params: dict, profile: str) -> dict:
        """
        The request params for a travel mode: this algorithm's costing options only apply to
        its own profile, other vehicles get Valhalla's defaults (and the same avoid areas).
        """
        if profile == self.profile.value or "options" not in params:
            return params
        return {**params, "options": {"shortest": params["options"].get("shortest", False)}}

    class _profile:
        """Temporarily switches the results factory to another travel mode."""

        def __init__(self, results_factory, profile: str):
            self.results_factory, self.profile = results_factory, RouterProfile(profile)

        def __enter__(self):
            self.previous = self.results_factory.profile
            self.results_factory.profile = self.profile

        def __exit__(self, *exc):
            self.results_factory.profile = self.previous

    def _route_geometry(
        self,
        results_factory,
        params: dict,
        coords: List[Tuple[float, float]],
        stop_locations: List[int],
        feedback: QgsProcessingFeedback,
    ) -> Tuple[Optional[QgsGeometry], Optional[float]]:
        """
        One line through the stops in order, routed in chunks; its length in meters. A server
        that allows fewer locations per route says so in its error: then retried within it,
        and the later routes use that limit straight away.
        """
        while True:
            try:
                return self._route_in_chunks(
                    results_factory, params, coords, stop_locations, self._max_locations
                )
            except ServerLimitError as e:
                self._max_locations = self._smaller_limit(
                    e, self._max_locations, 2, "route locations", feedback
                )

    def _route_in_chunks(
        self,
        results_factory,
        params: dict,
        coords: List[Tuple[float, float]],
        stop_locations: List[int],
        max_locations: int,
    ) -> Tuple[Optional[QgsGeometry], Optional[float]]:
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

    def _location_labels(self, problem: Problem) -> Dict[int, str]:
        """What each of problem.coords is, for messages. Subclasses add their own points."""
        labels = {job.location: job.label for job in problem.jobs}
        for v in problem.vehicles:
            labels[v.location] = f"Vehicle {v.vehicle_id}"
            if v.end_location is not None:
                labels[v.end_location] = f"Vehicle {v.vehicle_id}'s end point"
        return labels

    def _warn_far_points(self, results_factory, problem: Problem, limit: float, feedback):
        """
        Warns about points more than ``limit`` meters from the road Valhalla routes them from.
        Valhalla snaps a point to its nearest road up to 35 km away without a word, so a job in
        the wrong place (or the wrong CRS) looks served while the vehicle stops somewhere else.
        A vehicle's points count on its own travel mode, the others on the closest of the fleet's.
        """
        own = {v.location: v.profile for v in problem.vehicles}
        own.update({v.end_location: v.profile for v in problem.vehicles if v.end_location is not None})
        off_road: Dict[int, float] = dict()
        for profile in problem.profiles():
            try:
                distances = self._snap_distances(results_factory, profile, problem.coords, feedback)
            except QgsProcessingException as e:
                feedback.reportError(f"Couldn't check how far the points are from roads: {e}", False)
                return
            for i, d in enumerate(distances):
                if own.get(i, profile) == profile:
                    off_road[i] = min(off_road.get(i, math.inf), d)

        far = sorted(((d, i) for i, d in off_road.items() if d > limit), reverse=True)
        if not far:
            return
        labels = self._location_labels(problem)

        def text(meters: float) -> str:
            if math.isinf(meters):
                return "no road found"
            return f"{meters / 1000:.1f} km" if meters >= 1000 else f"{meters:.0f} m"

        listed = ", ".join(f"{labels.get(i, f'Point {i + 1}')} ({text(d)})" for d, i in far[:5])
        more = f" and {len(far) - 5} more" if len(far) > 5 else ""
        feedback.reportError(
            f"{len(far)} {'point is' if len(far) == 1 else 'points are'} more than {text(limit)} from a "
            f"road, so they're served from the nearest road instead: {listed}{more}",
            fatalError=False,
        )

    def _snap_distances(self, results_factory, profile: str, coords, feedback) -> List[float]:
        """Meters from each point to where Valhalla's /locate puts it on a road (inf: none)."""
        distance_area = QgsDistanceArea()
        distance_area.setEllipsoid("WGS84")
        client = results_factory.router.router.client
        out: List[float] = list()
        start = 0
        while start < len(coords):
            chunk = coords[start : start + self._max_locations]
            body = {"locations": [{"lon": x, "lat": y} for x, y in chunk], "costing": profile}
            try:
                located = self._client_request(client, "/locate", body)
            except ServerLimitError as e:
                self._max_locations = self._smaller_limit(
                    e, self._max_locations, 1, "locations", feedback
                )
                continue
            for (x, y), location in zip(chunk, located or []):
                out.append(
                    min(
                        (
                            distance_area.measureLine(
                                QgsPointXY(x, y), QgsPointXY(e["correlated_lon"], e["correlated_lat"])
                            )
                            for e in location.get("edges") or []
                        ),
                        default=math.inf,
                    )
                )
            start += len(chunk)
        return out

    @staticmethod
    def _client_request(client, url: str, body: dict):
        try:
            return client._request(url, post_params=body)
        except (routingpy.exceptions.RouterApiError, routingpy.exceptions.RouterServerError) as e:
            limit = server_limit(e.message)
            if limit is not None:
                raise ServerLimitError(f"HTTP {e.status}: {e.message}", limit)
            raise QgsProcessingException(f"HTTP {e.status}: {e.message}")

    def _matrices(
        self, results_factory, problem: Problem, params: dict, max_pairs: int, feedback
    ) -> Optional[Matrices]:
        """
        A matrix between all locations per travel mode, in chunks of source rows; None if
        canceled. ``max_pairs`` is only a first guess (Valhalla's default): a server that
        allows fewer says so in its error, e.g. FOSSGIS' 100, and it's all redone within that.
        """
        while True:
            try:
                chunks = matrix_row_chunks(len(problem.coords), max_pairs)
            except ValueError as e:
                raise QgsProcessingException(str(e))
            try:
                return self._matrices_in_chunks(results_factory, problem, params, chunks, feedback)
            except ServerLimitError as e:
                max_pairs = self._smaller_limit(e, max_pairs, 1, "matrix pairs", feedback)

    def _matrices_in_chunks(
        self, results_factory, problem: Problem, params: dict, chunks: List[List[int]], feedback
    ) -> Optional[Matrices]:
        matrices: Matrices = dict()
        all_targets = list(range(len(problem.coords)))
        n_requests, done = len(chunks) * len(problem.profiles()), 0
        for profile in problem.profiles():
            durations: List[List[Optional[float]]] = list()
            distances: List[List[Optional[float]]] = list()
            with self._profile(results_factory, profile):
                for rows in chunks:
                    if feedback.isCanceled():
                        return None
                    matrix = self._request(
                        results_factory,
                        RouterEndpoint.MATRIX,
                        problem.coords,
                        {
                            **self._profile_params(params, profile),
                            "sources": rows,
                            "destinations": all_targets,
                        },
                    )
                    durations.extend(matrix.durations)
                    distances.extend(matrix.distances)
                    done += 1
                    feedback.setProgress(int(40 * done / n_requests))
            matrices[profile] = (durations, distances)
        return matrices

    @staticmethod
    def _smaller_limit(
        error: "ServerLimitError", current: int, minimum: int, what: str, feedback
    ) -> int:
        """The server's limit to retry with, or the error again if retrying can't help."""
        if not minimum <= error.limit < current:
            raise error
        feedback.pushWarning(
            f"The server allows {error.limit} {what} per request, not {current}: "
            "retrying with smaller requests"
        )
        return error.limit

    @staticmethod
    def _request(results_factory, endpoint: RouterEndpoint, coords, params: dict):
        try:
            return results_factory.router.request(endpoint, [list(c) for c in coords], **params)
        except (
            routingpy.exceptions.RouterApiError,
            routingpy.exceptions.RouterServerError,
        ) as e:
            limit = server_limit(e.message)
            if limit is not None:
                raise ServerLimitError(f"HTTP {e.status}: {e.message}", limit)
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
