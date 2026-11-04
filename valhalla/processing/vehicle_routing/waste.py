"""
Waste collection: a VRP whose vehicles empty out at a landfill whenever they're full and carry
on with the next trip (multi_trip.py), until the bins or their shifts run out.
"""

from pathlib import Path
from typing import List, Optional, Tuple

from qgis.core import (
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
)

from ...global_definitions import RouterProfile
from ...utils.layer_utils import get_wgs_coords_from_feature
from ...utils.misc_utils import wrap_in_html_tag
from ..processing_definitions import HELP_DIR
from .multi_trip import identity_keep, solve_multi_trip
from .problem import Matrices, Problem
from .vrp import ValhallaVrpBase


class ValhallaWasteBase(ValhallaVrpBase):
    """The jobs are bins (their pickup amount is the waste), plus a landfill layer."""

    IN_LANDFILLS = "INPUT_LANDFILLS"
    IN_DUMP_TIME = "INPUT_DUMP_TIME"
    IN_MAX_TRIPS = "INPUT_MAX_TRIPS"

    # what a waste round doesn't have
    REMOVED = (
        ValhallaVrpBase.IN_JOB_DELIVERY,
        ValhallaVrpBase.IN_JOB_SHIPMENT,
        ValhallaVrpBase.IN_JOB_ROLE,
        ValhallaVrpBase.IN_JOB_VEHICLE,
        ValhallaVrpBase.IN_JOB_SEQUENCE,
        ValhallaVrpBase.IN_KEEP_ORDER,
    )

    def initAlgorithm(self, configuration, p_str=None, Any=None, *args, **kwargs):
        super().initAlgorithm(configuration)
        for name in self.REMOVED:
            self.removeParameter(name)
        self.parameterDefinition(self.IN_1).setDescription(
            f"{wrap_in_html_tag('Stops', 'b')}: point layer of the bins or stops to collect from"
        )
        self.parameterDefinition(self.IN_JOB_PICKUP).setDescription(
            "Waste amount field (number, or comma separated, e.g. weight and volume)"
        )
        self.parameterDefinition(self.IN_VEH_CAPACITY).setDescription(
            "Vehicle capacity field: what it holds before it has to empty out"
        )
        for name, desc in (
            (self.OUT, "waste_routes"),
            (self.OUT_STOPS, "waste_stops"),
            (self.OUT_UNASSIGNED, "waste_unassigned"),
        ):
            self.parameterDefinition(name).setDescription(desc)

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_LANDFILLS,
                f"{wrap_in_html_tag('Landfills', 'b')}: point layer of where vehicles empty out",
                types=[QgsProcessing.SourceType.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IN_DUMP_TIME,
                "Time to empty out at a landfill (seconds)",
                type=QgsProcessingParameterNumber.Type.Integer,
                defaultValue=900,
                minValue=0,
            )
        )
        max_trips = QgsProcessingParameterNumber(
            self.IN_MAX_TRIPS,
            "Maximum trips per vehicle",
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=10,
            minValue=1,
            maxValue=100,
        )
        max_trips.setFlags(max_trips.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(max_trips)

    def _read_extra(self, parameters, context, problem: Problem):
        """The landfills go after jobs & vehicles in problem.coords, so the matrices include them."""
        source = self.parameterAsSource(parameters, self.IN_LANDFILLS, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.IN_LANDFILLS))
        self._landfills: List[int] = list()
        for feat in source.getFeatures():
            if not feat.hasGeometry() or feat.geometry().isEmpty():
                continue
            coords = get_wgs_coords_from_feature(feat, source.sourceCrs())
            if coords and isinstance(coords[0], list):  # a MultiPoint
                coords = coords[0]
            problem.coords.append(tuple(coords[:2]))
            self._landfills.append(len(problem.coords) - 1)
        if not self._landfills:
            raise QgsProcessingException("The landfills layer has no points")

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
        try:
            solution = solve_multi_trip(
                problem,
                matrices,
                self._landfills,
                self.parameterAsInt(parameters, self.IN_DUMP_TIME, context),
                return_to_start,
                self._vroom_solver(parameters, context, feedback),
                skip_jobs=unreachable,
                max_trips=self.parameterAsInt(parameters, self.IN_MAX_TRIPS, context),
                is_canceled=feedback.isCanceled,
            )
        except ValueError as e:
            raise QgsProcessingException(str(e))
        if solution is None:
            return None
        trips = sum(route.get("trips", 0) for route in solution["routes"])
        feedback.pushInfo(f"{trips} trips to the landfill")
        return solution, identity_keep(problem), []

    def name(self):
        return f"valhalla_waste_{self.profile.lower()}"

    def displayName(self):
        return f"Waste Collection {self.profile.capitalize()}"

    def shortHelpString(self):
        with open(HELP_DIR / Path("valhalla_waste.help")) as fh:
            return fh.read()


class ValhallaWasteTruck(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.TRUCK)


class ValhallaWasteCar(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.CAR)


class ValhallaWasteBus(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.BUS)


class ValhallaWasteMotorcycle(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.MBIKE)


class ValhallaWasteBicycle(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.BIKE)


class ValhallaWastePedestrian(ValhallaWasteBase):
    def __init__(self):
        super().__init__(profile=RouterProfile.PED)
