"""
Runs a vehicle routing problem from the dock: the table's points become a jobs and a vehicles
layer, and the VRP Processing algorithm runs in a QgsProcessingAlgRunnerTask (task manager →
cancelable) with the dock's provider, travel mode and costing options instead of its own
parameters.
"""

from enum import Enum
from typing import Callable, Dict, List, Optional, Type

from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayer,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingOutputLayerDefinition,
    QgsProject,
    QgsRendererCategory,
    QgsSymbol,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from ..core.results_factory import ResultsFactory
from ..global_definitions import RouterMethod, RouterProfile, RouterType
from ..processing.vehicle_routing.vrp import (
    FieldNames,
    ValhallaVrpBase,
    ValhallaVrpBicycle,
    ValhallaVrpBus,
    ValhallaVrpCar,
    ValhallaVrpMotorcycle,
    ValhallaVrpPedestrian,
    ValhallaVrpTruck,
)
from .spopt_run import _Feedback
from .widgets.waypoint_model import VRP_MODES, VrpRole, Waypoint


class FleetProblem(str, Enum):
    """The dock's menu key for vehicle routing (next to RouterEndpoint and SpoptProblem)."""

    VRP = "vrp"


ALGORITHMS: Dict[RouterProfile, Type[ValhallaVrpBase]] = {
    RouterProfile.CAR: ValhallaVrpCar,
    RouterProfile.TRUCK: ValhallaVrpTruck,
    RouterProfile.BUS: ValhallaVrpBus,
    RouterProfile.BIKE: ValhallaVrpBicycle,
    RouterProfile.PED: ValhallaVrpPedestrian,
    RouterProfile.MBIKE: ValhallaVrpMotorcycle,
}

# a qualitative palette, one color per vehicle, repeating
PALETTE = ("#6f9cf0", "#f76c5e", "#fdb147", "#3bb273", "#9b6ff0", "#2ec4c4", "#e86fb6", "#8c6d46")


def split_waypoints(waypoints: List[Waypoint]) -> tuple:
    """The VRP table's points as (jobs, vehicles); vehicle ends are in ``end_points``."""
    vehicles = [wp for wp in waypoints if wp.attrs.get("role") == VrpRole.VEHICLE.value]
    not_jobs = (VrpRole.VEHICLE.value, VrpRole.END.value)
    jobs = [wp for wp in waypoints if wp.attrs.get("role") not in not_jobs]
    return jobs, vehicles


def end_points(waypoints: List[Waypoint]) -> List[Waypoint]:
    """The table's vehicle ends: each finishes the route of the vehicle named in its Name."""
    return [wp for wp in waypoints if wp.attrs.get("role") == VrpRole.END.value]


def ends_layer(ends: List[Waypoint]) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "vehicle ends", "memory")
    layer.dataProvider().addAttributes([QgsField("vehicle", QVariant.String)])
    layer.updateFields()
    feats = list()
    for wp in ends:
        feat = QgsFeature(layer.fields())
        feat.setAttributes([_text(wp.attrs.get("name"))])
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(wp.lon, wp.lat)))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    return layer


def _text(value) -> Optional[str]:
    return str(value).strip() or None if value is not None else None


def _positive(value, scale: float = 1) -> Optional[int]:
    """A count/duration from the table, None for 0 (= not set)."""
    number = int(round(float(value or 0) * scale))
    return number if number > 0 else None


def jobs_layer(jobs: List[Waypoint], with_amounts: bool) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "jobs", "memory")
    layer.dataProvider().addAttributes(
        [
            QgsField("name", QVariant.String),
            QgsField("amount", QVariant.Int),
            QgsField("service", QVariant.Int),
            QgsField("tw_start", QVariant.String),
            QgsField("tw_end", QVariant.String),
            QgsField("skills", QVariant.String),
            QgsField("priority", QVariant.Int),
            QgsField("vehicle", QVariant.String),
            QgsField("sequence", QVariant.Int),
            QgsField("shipment", QVariant.String),
            QgsField("shipment_role", QVariant.String),
        ]
    )
    layer.updateFields()
    feats = list()
    for idx, wp in enumerate(jobs):
        feat = QgsFeature(layer.fields())
        feat.setAttributes(
            [
                _text(wp.attrs.get("name")) or f"job {idx + 1}",
                int(wp.attrs.get("amount") or 0) if with_amounts else None,
                int(round(float(wp.attrs.get("service") or 0) * 60)),  # minutes in the table
                _text(wp.attrs.get("start")),
                _text(wp.attrs.get("end")),
                _text(wp.attrs.get("skills")),
                _positive(wp.attrs.get("priority")),
                _text(wp.attrs.get("vehicle")),
                _positive(wp.attrs.get("order")),
                _text(wp.attrs.get("shipment")),
                _text(wp.attrs.get("shipment_role")),
            ]
        )
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(wp.lon, wp.lat)))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    return layer


def vehicles_layer(vehicles: List[Waypoint], with_amounts: bool) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "vehicles", "memory")
    layer.dataProvider().addAttributes(
        [
            QgsField("name", QVariant.String),
            QgsField("capacity", QVariant.Int),
            QgsField("shift_start", QVariant.String),
            QgsField("shift_end", QVariant.String),
            QgsField("skills", QVariant.String),
            QgsField("mode", QVariant.String),
            QgsField("max_jobs", QVariant.Int),
            QgsField("break_start", QVariant.String),
            QgsField("break_end", QVariant.String),
            QgsField("break_seconds", QVariant.Int),
            QgsField("fixed_cost", QVariant.Int),
            QgsField("cost_per_hour", QVariant.Int),
            QgsField("cost_per_km", QVariant.Int),
            QgsField("max_travel_time", QVariant.Int),
            QgsField("max_distance", QVariant.Int),
        ]
    )
    layer.updateFields()
    feats = list()
    for idx, wp in enumerate(vehicles):
        feat = QgsFeature(layer.fields())
        mode = wp.attrs.get("mode") or VRP_MODES[0]
        capacity = int(wp.attrs.get("amount") or 0)
        feat.setAttributes(
            [
                _text(wp.attrs.get("name")) or f"vehicle {idx + 1}",
                # 0 = no capacity set: an error if jobs have amounts, rather than an empty route
                capacity if with_amounts and capacity else None,
                _text(wp.attrs.get("start")),
                _text(wp.attrs.get("end")),
                _text(wp.attrs.get("skills")),
                None if mode == VRP_MODES[0] else mode,
                _positive(wp.attrs.get("max_jobs")),
                _text(wp.attrs.get("break_start")),
                _text(wp.attrs.get("break_end")),
                _positive(wp.attrs.get("break_minutes"), 60),  # minutes in the table
                # VROOM's defaults when not set: no fixed cost, the cost is the travel time
                int(wp.attrs.get("fixed_cost") or 0),
                int(3600 if wp.attrs.get("cost_per_hour") is None else wp.attrs["cost_per_hour"]),
                int(wp.attrs.get("cost_per_km") or 0),
                _positive(wp.attrs.get("max_minutes"), 60),
                _positive(wp.attrs.get("max_km"), 1000),
            ]
        )
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(wp.lon, wp.lat)))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    return layer


def style_by_vehicle(layer: QgsVectorLayer):
    """One color per vehicle, so routes and stops read together."""
    idx = layer.fields().indexOf(FieldNames.VEHICLE_ID)
    values = sorted({str(v) for v in layer.uniqueValues(idx)})
    categories = list()
    for n, value in enumerate(values):
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        symbol.setColor(QColor(PALETTE[n % len(PALETTE)]))
        if layer.geometryType() == QgsWkbTypes.GeometryType.LineGeometry:
            symbol.setWidth(0.8)
        categories.append(QgsRendererCategory(value, symbol, value))
    layer.setRenderer(QgsCategorizedSymbolRenderer(FieldNames.VEHICLE_ID, categories))


class VrpRun:
    """
    One solve in the background. Keep a reference until ``on_done`` was called: it owns the
    input layers, the context with the results and the task.

    ``on_done(layers, error)`` gets the routes, stops & unassigned layers, or an error message
    (None if it was canceled). Warnings of a successful run (e.g. unreachable jobs) are in
    ``warnings``.
    """

    def __init__(
        self,
        jobs: List[Waypoint],
        vehicles: List[Waypoint],
        provider_url: str,
        profile: RouterProfile,
        request_params: dict,
        return_to_start: bool,
        exploration_level: int,
        timeout: int,
        on_done: Callable[[List[QgsMapLayer], Optional[str]], None],
        keep_order: bool = False,
        ends: List[Waypoint] = (),
    ):
        self.on_done = on_done
        self.warnings: List[str] = list()
        with_amounts = any(int(wp.attrs.get("amount") or 0) for wp in jobs)
        self.jobs_layer = jobs_layer(jobs, with_amounts)
        self.vehicles_layer = vehicles_layer(vehicles, with_amounts)

        base_cls = ALGORITHMS[profile]

        class DockVrp(base_cls):
            """Requests with the dock's provider and options instead of the algorithm's."""

            def get_base_params(self, parameters, context):
                jobs_source, job_id_field, _, _ = super().get_base_params(parameters, context)
                factory = ResultsFactory(RouterType.VALHALLA, RouterMethod.REMOTE, profile, provider_url)
                return jobs_source, job_id_field, dict(request_params), factory

        alg_cls = DockVrp
        self.params = {
            alg_cls.IN_1: self.jobs_layer,
            alg_cls.IN_FIELD_1: "name",
            alg_cls.IN_JOB_DELIVERY: "amount",
            alg_cls.IN_JOB_SERVICE: "service",
            alg_cls.IN_JOB_TW_START: "tw_start",
            alg_cls.IN_JOB_TW_END: "tw_end",
            alg_cls.IN_JOB_SKILLS: "skills",
            alg_cls.IN_VEH: self.vehicles_layer,
            alg_cls.IN_VEH_ID: "name",
            alg_cls.IN_VEH_CAPACITY: "capacity",
            alg_cls.IN_VEH_TW_START: "shift_start",
            alg_cls.IN_VEH_TW_END: "shift_end",
            alg_cls.IN_VEH_SKILLS: "skills",
            alg_cls.IN_VEH_PROFILE: "mode",
            alg_cls.IN_JOB_PRIORITY: "priority",
            alg_cls.IN_JOB_VEHICLE: "vehicle",
            alg_cls.IN_JOB_SEQUENCE: "sequence",
            alg_cls.IN_JOB_SHIPMENT: "shipment",
            alg_cls.IN_JOB_ROLE: "shipment_role",
            alg_cls.IN_VEH_MAX_TASKS: "max_jobs",
            alg_cls.IN_VEH_FIXED_COST: "fixed_cost",
            alg_cls.IN_VEH_COST_PER_HOUR: "cost_per_hour",
            alg_cls.IN_VEH_COST_PER_KM: "cost_per_km",
            alg_cls.IN_VEH_MAX_TRAVEL_TIME: "max_travel_time",
            alg_cls.IN_VEH_MAX_DISTANCE: "max_distance",
            alg_cls.IN_KEEP_ORDER: keep_order,
            alg_cls.IN_RETURN: return_to_start,
            alg_cls.IN_EXPLORATION: exploration_level,
            alg_cls.IN_TIMEOUT: timeout,
        }
        self.ends_layer = ends_layer(ends) if ends else None
        if self.ends_layer is not None:
            self.params[alg_cls.IN_VEH_ENDS] = self.ends_layer
            self.params[alg_cls.IN_VEH_END_ID] = "vehicle"
        # breaks only if a vehicle has one: keeping the order warns about any break field
        if any(_positive(wp.attrs.get("break_minutes"), 60) for wp in vehicles):
            self.params.update(
                {
                    alg_cls.IN_VEH_BREAK_START: "break_start",
                    alg_cls.IN_VEH_BREAK_END: "break_end",
                    alg_cls.IN_VEH_BREAK_DURATION: "break_seconds",
                }
            )
        for out in (alg_cls.OUT, alg_cls.OUT_STOPS, alg_cls.OUT_UNASSIGNED):
            self.params[out] = QgsProcessingOutputLayerDefinition(QgsProcessing.TEMPORARY_OUTPUT)

        self.alg = alg_cls().create()
        self.context = QgsProcessingContext()
        self.context.setProject(QgsProject.instance())
        self.feedback = _Feedback()
        self.task = QgsProcessingAlgRunnerTask(self.alg, self.params, self.context, self.feedback)
        self.task.executed.connect(self._on_executed)

    def start(self):
        QgsApplication.taskManager().addTask(self.task)

    def cancel(self):
        self.task.cancel()

    def _on_executed(self, successful: bool, results: dict):
        if not successful:
            if self.feedback.isCanceled():
                self.on_done([], None)
            else:
                self.on_done([], self.feedback.errors[-1] if self.feedback.errors else "Unknown error")
            return

        self.warnings = list(self.feedback.errors)
        layers = list()
        for key, name in (
            (self.alg.OUT_UNASSIGNED, "VRP Unassigned"),
            (self.alg.OUT, "VRP Routes"),
            (self.alg.OUT_STOPS, "VRP Stops"),
        ):
            layer = self.context.takeResultLayer(results[key])
            layer.setName(name)
            if key != self.alg.OUT_UNASSIGNED:
                style_by_vehicle(layer)
            layers.append(layer)
        self.on_done(layers, None)
