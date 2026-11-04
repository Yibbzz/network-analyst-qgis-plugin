from pathlib import Path
from typing import List, Optional, Tuple, Type

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeatureSink,
    QgsFeatureSource,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ...core.barriers import drop_unmatched, point_barrier_lines, polygon_barrier_lines
from ...core.results_factory import ResultsFactory
from ...core.settings import DEFAULT_PROVIDERS, ProviderSetting, ValhallaSettings
from ...global_definitions import (
    SETTINGS_WIDGETS_MAP,
    RouterEndpoint,
    RouterMethod,
    RouterProfile,
    RouterType,
    RoutingMetric,
)
from ...gui.widgets.costing_settings.widget_settings_valhalla_base import (
    ValhallaSettingsBase,
)
from ...third_party.routingpy import routingpy
from ...third_party.routingpy.routingpy.utils import decode_polyline6
from ...utils.geom_utils import WGS84
from ...utils.layer_utils import get_wgs_coords_from_layer
from ...utils.logger_utils import qgis_log
from ...utils.misc_utils import wrap_in_html_tag
from ...utils.resource_utils import get_icon
from ..processing_definitions import HELP_DIR

# Valhalla's error for a linear_cost_factors line it can't edge-walk
LINE_NOT_MATCHED = 233


class ValhallaBaseAlgorithm(QgsProcessingAlgorithm):

    IN_PROVIDER = "INPUT_PROVIDER"
    # IN_URL = "INPUT_URL"
    IN_PKG = "INPUT_PACKAGE"
    IN_MODE = "INPUT_MODE"
    IN_AVOID_LOCATIONS = "INPUT_AVOID_LOCATIONS"
    IN_AVOID_POLYGONS = "INPUT_AVOID_POLYGONS"
    IN_POINT_BARRIERS = "INPUT_POINT_BARRIERS"
    IN_POINT_BARRIER_FIELD = "INPUT_POINT_BARRIER_FIELD"
    IN_POINT_BARRIER_COST = "INPUT_POINT_BARRIER_COST"
    IN_POLYGON_BARRIERS = "INPUT_POLYGON_BARRIERS"
    IN_POLYGON_BARRIER_FIELD = "INPUT_POLYGON_BARRIER_FIELD"
    IN_POLYGON_BARRIER_FACTOR = "INPUT_POLYGON_BARRIER_FACTOR"
    IN_1 = "INPUT_LAYER_1"
    IN_FIELD_1 = "INPUT_FIELD_1"

    OUT = "OUTPUT"
    WITH_COSTING_OPTIONS = True
    IN_1_TYPES = [QgsProcessing.SourceType.TypeVectorPoint]

    def __init__(
        self,
        provider: RouterType,
        endpoint: RouterEndpoint,
        profile: Optional[RouterProfile] = None,
    ):
        super(ValhallaBaseAlgorithm, self).__init__()

        self.router = provider  # avoid overriding QGIS' built-in provider method
        self.providers: List[ProviderSetting] = list()  # will be set in init_base_params
        self.endpoint = endpoint
        self.profile = profile
        if provider == RouterType.VALHALLA and endpoint != RouterEndpoint.ELEVATION:
            costing_widget: Type[ValhallaSettingsBase] = SETTINGS_WIDGETS_MAP[self.profile]["widget"]

            # we keep the costing params dict for retrieving the parameter values later
            self.costing_defaults = self.get_costing_defaults(costing_widget)

    def tr(self, string):
        """
        Returns a translatable string with the self.tr() function.
        """
        return QCoreApplication.translate("Processing", string)

    def init_base_params(self, multi_layer: bool = False) -> None:
        """
        We call this method in each subclass' initAlgorithm method, since these parameters are constant
        across algorithms.

        :param multi_layer: If True, adds a number to the input layer and input field descriptions.
        """

        # make sure we have at least one remote HTTP API URL
        settings = ValhallaSettings()
        if not settings.get_providers(RouterType.VALHALLA):
            for prov in DEFAULT_PROVIDERS:
                settings.set_provider(RouterType.VALHALLA.lower(), prov)

        self.providers = settings.get_providers(RouterType.VALHALLA)
        servers = [prov.name for prov in self.providers]

        url_param = QgsProcessingParameterEnum(
            self.IN_PROVIDER,
            "Provider",
            servers,
            defaultValue=servers[0],
        )
        self.addParameter(url_param)

        # pkg_param = QgsProcessingParameterEnum(
        #     self.IN_PKG,
        #     "Package path (used if method is Bindings)",
        #     options=self.pkgs,
        #     defaultValue=0,
        # )
        # self.addParameter(pkg_param)

        # url_param = QgsProcessingParameterString(
        #     self.IN_URL, "URL (used if method is HTTP)", defaultValue=defaultUrl
        # )
        # self.addParameter(url_param)

        if self.router == RouterType.VALHALLA and self.WITH_COSTING_OPTIONS:
            mode_param = QgsProcessingParameterEnum(
                self.IN_MODE,
                "Mode",
                options=list(RoutingMetric),
                defaultValue=RoutingMetric[0],
            )
            mode_param.setHelp(
                "If 'shortest', Valhalla will solely use distance as cost and disregard all other costs, penalties and factors."
            )

            mode_param.setFlags(QgsProcessingParameterDefinition.Flag.FlagAdvanced)

            self.addParameter(mode_param)

            avoid_locations_param = QgsProcessingParameterFeatureSource(
                name=self.IN_AVOID_LOCATIONS,
                description="Point layer with locations to avoid",
                types=[QgsProcessing.SourceType.TypeVectorPoint],
                optional=True,
            )

            avoid_locations_param.setFlags(
                avoid_locations_param.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced
            )
            self.addParameter(avoid_locations_param)

            avoid_polygons_param = QgsProcessingParameterFeatureSource(
                name=self.IN_AVOID_POLYGONS,
                description="Point layer with polygons to avoid",
                types=[QgsProcessing.SourceType.TypeVectorPolygon],
                optional=True,
            )
            avoid_polygons_param.setFlags(
                avoid_polygons_param.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced
            )
            self.addParameter(avoid_polygons_param)
            self.init_barrier_params()
            self.setup_costing_options()

        input_layer_desc = f"Input point layer{' 1' if multi_layer else ''}"
        input_param = QgsProcessingParameterFeatureSource(
            name=self.IN_1,
            description=wrap_in_html_tag(input_layer_desc, "b"),
            types=self.IN_1_TYPES,
        )
        self.addParameter(input_param)

        input_field_desc = f"Layer{' 1' if multi_layer else ''} ID field (can be used for joining)"
        input_field_param = QgsProcessingParameterField(
            name=self.IN_FIELD_1,
            description=input_field_desc,
            parentLayerParameterName=self.IN_1,
            optional=True,
        )
        self.addParameter(input_field_param)

        output_param = QgsProcessingParameterFeatureSink(
            name=self.OUT,
            description=f"{self.router.lower()}_{self.endpoint.lower()}"
            f"{f'_{self.profile}' if self.router == RouterType.VALHALLA and self.profile else str()}",
            createByDefault=True,
        )
        self.addParameter(output_param)

    def init_barrier_params(self):
        """Added-cost point barriers and scaled-cost polygon barriers, all advanced."""
        advanced = QgsProcessingParameterDefinition.Flag.FlagAdvanced
        for (
            layer_name,
            layer_desc,
            layer_type,
            field_name,
            field_desc,
            value_name,
            value_desc,
            default,
            minimum,
        ) in (
            (
                self.IN_POINT_BARRIERS,
                "Point barriers: crossing one adds a cost",
                QgsProcessing.SourceType.TypeVectorPoint,
                self.IN_POINT_BARRIER_FIELD,
                "Point barrier added cost field (about seconds)",
                self.IN_POINT_BARRIER_COST,
                "Point barrier added cost, if no field (about seconds)",
                300.0,
                0.0,
            ),
            (
                self.IN_POLYGON_BARRIERS,
                "Polygon barriers: roads inside cost a factor more",
                QgsProcessing.SourceType.TypeVectorPolygon,
                self.IN_POLYGON_BARRIER_FIELD,
                "Polygon barrier factor field",
                self.IN_POLYGON_BARRIER_FACTOR,
                "Polygon barrier factor, if no field (e.g. 2 = twice the cost)",
                2.0,
                0.0,
            ),
        ):
            layer = QgsProcessingParameterFeatureSource(
                layer_name, layer_desc, [layer_type], optional=True
            )
            layer.setHelp(
                "Valhalla uses the barriers to choose the path, but reports durations and costs "
                "without them: a barrier not worth a detour changes nothing you can see."
            )
            barrier_field = QgsProcessingParameterField(
                field_name,
                field_desc,
                parentLayerParameterName=layer_name,
                type=QgsProcessingParameterField.DataType.Numeric,
                optional=True,
            )
            value = QgsProcessingParameterNumber(
                value_name,
                value_desc,
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=default,
                minValue=minimum,
            )
            for param in (layer, barrier_field, value):
                param.setFlags(param.flags() | advanced)
                self.addParameter(param)

    def get_barrier_factors(  # noqa: C901 - three small request helpers inside
        self, parameters, context, results_factory, options: dict
    ) -> List[dict]:
        """
        The barriers as Valhalla ``linear_cost_factors``. Costs a few requests per barrier:
        an expansion around it, and for points a short route to price the stretch.
        """
        router = results_factory.router

        def expand(point, meters):
            try:
                result = router.request(
                    RouterEndpoint.EXPANSION,
                    list(point),
                    intervals=[meters],
                    interval_type="distance",
                    expansion_properties=["edge_id", "pred_edge_id"],
                    options=options,
                )
            except (routingpy.exceptions.RouterApiError, routingpy.exceptions.RouterServerError) as e:
                raise QgsProcessingException(f"Barrier lookup, HTTP {e.status}: {e.message}")
            return result.raw.get("features", []) if result.raw else []

        def cost(a, b):
            try:
                result = router.request(RouterEndpoint.DIRECTIONS, [list(a), list(b)], options=options)
            except (routingpy.exceptions.RouterApiError, routingpy.exceptions.RouterServerError):
                return None
            return result.raw["trip"]["summary"]["cost"] if result.raw else None

        def values(layer_name, field_name, default_name):
            source = self.parameterAsSource(parameters, layer_name, context)
            if source is None:
                return
            field = self.parameterAsString(parameters, field_name, context)
            default = self.parameterAsDouble(parameters, default_name, context)
            transform = QgsCoordinateTransform(source.sourceCrs(), WGS84, context.transformContext())
            for feat in source.getFeatures():
                if not feat.hasGeometry():
                    continue
                value = feat[field] if field else default
                if value is None or (hasattr(value, "isNull") and value.isNull()):
                    value = default
                geom = QgsGeometry(feat.geometry())
                geom.transform(transform)
                yield geom, float(value)

        factors: List[dict] = list()
        for geom, added in values(
            self.IN_POINT_BARRIERS, self.IN_POINT_BARRIER_FIELD, self.IN_POINT_BARRIER_COST
        ):
            for part in geom.asMultiPoint() if geom.isMultipart() else [geom.asPoint()]:
                factors += point_barrier_lines((part.x(), part.y()), added, expand, cost)
        for geom, factor in values(
            self.IN_POLYGON_BARRIERS, self.IN_POLYGON_BARRIER_FIELD, self.IN_POLYGON_BARRIER_FACTOR
        ):
            factors += polygon_barrier_lines(geom, factor, expand)

        def matches(lines: List[dict]) -> bool:
            """Whether Valhalla takes these lines: a tiny route along the first one carries them."""
            ends = decode_polyline6(lines[0]["shape"])
            try:
                router.request(
                    RouterEndpoint.DIRECTIONS,
                    [list(ends[0]), list(ends[-1])],
                    options=options,
                    linear_cost_factors=lines,
                )
            except routingpy.exceptions.RouterApiError as e:
                if isinstance(e.message, dict) and e.message.get("error_code") == LINE_NOT_MATCHED:
                    return False
                raise QgsProcessingException(f"Barrier check, HTTP {e.status}: {e.message}")
            return True

        factors, dropped = drop_unmatched(factors, matches)
        if dropped:
            qgis_log(
                f"{dropped} barrier road pieces were left out: Valhalla couldn't match them to its roads",
                Qgis.MessageLevel.Warning,
            )

        return factors

    def get_base_params(self, parameters, context):
        provider: ProviderSetting = self.providers[
            self.parameterAsEnum(parameters, self.IN_PROVIDER, context)
        ]
        mode = RoutingMetric[self.parameterAsEnum(parameters, self.IN_MODE, context)]
        # url = self.parameterAsString(parameters, self.IN_URL, context)
        # pkg = (
        #     self.pkgs[self.parameterAsEnum(parameters, self.IN_PKG, context)]
        #     if len(self.pkgs) > 0
        #     else ""
        # )
        layer_1: QgsFeatureSource = self.parameterAsSource(parameters, self.IN_1, context)
        layer_field_name_1 = self.parameterAsString(parameters, self.IN_FIELD_1, context)

        params = dict()
        if self.router == RouterType.VALHALLA and self.WITH_COSTING_OPTIONS:
            params["options"] = dict()
            avoid_locations = self.parameterAsSource(parameters, self.IN_AVOID_LOCATIONS, context)
            if avoid_locations:
                params["avoid_locations"] = get_wgs_coords_from_layer(avoid_locations)

            avoid_polygons = self.parameterAsSource(parameters, self.IN_AVOID_POLYGONS, context)
            if avoid_polygons:
                params["avoid_polygons"] = get_wgs_coords_from_layer(avoid_polygons)

            params["options"] = self.get_costing_options(parameters, context)
            params["options"]["shortest"] = True if mode == RoutingMetric.SHORTEST else False

        factory_args = {
            "provider": self.router,
            "url": provider.url,
            "profile": self.profile,
            "method": RouterMethod.REMOTE,
        }

        # if method == RouterMethod.LOCAL:
        #     if not pkg:
        #         raise QgsProcessingException(
        #             f"You chose the '{method}' method but don't seem to have any packages downloaded."
        #         )
        #     factory_args.update(
        #         {
        #             "method": RouterMethod.LOCAL,
        #             "url": None,
        #             "pkg_path": str(get_graph_dir(self.router) / pkg),
        #         }
        #     )
        results_factory = ResultsFactory(**factory_args)

        if self.router == RouterType.VALHALLA and self.WITH_COSTING_OPTIONS:
            if barrier_factors := self.get_barrier_factors(
                parameters, context, results_factory, params["options"]
            ):
                # alongside any linear_cost_factors already there (e.g. map matched lines)
                params["linear_cost_factors"] = params.get("linear_cost_factors", []) + barrier_factors

        return layer_1, layer_field_name_1, params, results_factory

    def setup_costing_options(self):
        """Takes the costing defaults retrieved from the settings widget and creates processing parameters from them."""
        for param_name, param_dict in self.costing_defaults:
            processing_param: QgsProcessingParameterDefinition

            processing_param_args = {
                "name": param_name,
                "description": " ".join(map(lambda w: w.capitalize(), param_name.split("_"))),
                "defaultValue": param_dict["value"],
                "optional": True,
            }

            param_type = param_dict["type"]

            if param_type in (int, float):
                param_min = param_dict["min"]
                param_max = param_dict["max"]

                processing_param = QgsProcessingParameterNumber(
                    **processing_param_args,
                    type=(
                        QgsProcessingParameterNumber.Type.Integer
                        if param_type == int
                        else QgsProcessingParameterNumber.Type.Double
                    ),
                    minValue=param_min,
                    maxValue=param_max,
                )
            elif param_type == bool:
                processing_param = QgsProcessingParameterBoolean(**processing_param_args)

            elif param_type == str:
                processing_param = QgsProcessingParameterString(**processing_param_args)

            else:
                raise TypeError(f"Type {param_type} not supported for costing options.")

            processing_param.setFlags(
                processing_param.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced
            )
            processing_param.setHelp(param_dict["help"])
            self.addParameter(processing_param)

    @staticmethod
    def get_costing_defaults(costing_widget: Type[ValhallaSettingsBase]):
        """Retrieves the costing defaults from the given costing_widgets."""
        return costing_widget().get_params(include_info=True).items()

    def get_costing_options(self, parameters, context) -> dict:
        """Returns the costing options as set by the user in the processing algo UI."""
        costing_params = dict()
        for param_name, param_dict in self.costing_defaults:
            param_type = param_dict["type"]

            if param_type == int:
                parameter_getter = self.parameterAsInt
            elif param_type == float:
                parameter_getter = self.parameterAsDouble
            elif param_type == bool:
                parameter_getter = self.parameterAsBoolean
            elif param_type == str:
                parameter_getter = self.parameterAsString
            else:
                raise TypeError(f"Type {param_type} not supported for costing options.")
            costing_params[param_name] = parameter_getter(parameters, param_name, context)

        return costing_params

    def get_feature_sink(self, parameters, context, fields: QgsFields) -> Tuple[QgsFeatureSink, str]:
        """
        Convenience method to get the algorithm's feature sink with the corresponding endpoint's QgsFields
        and optionally additional fields (e.g. an ID field).

        :param parameters: passed from processAlgorithm()
        :param context: passed from processAlgorithm()
        :param fields: QgsFields that will be appended after the endpoint fields or overwrite the default if specified twice.
        """

        return self.parameterAsSink(
            parameters,
            self.OUT,
            context,
            fields,
            ResultsFactory.geom_type(self.endpoint),
            WGS84,
        )

    def createInstance(self):
        return type(self)()

    def icon(self) -> QIcon:
        return get_icon(f"{self.endpoint.lower()}_icon.svg")

    def name(self):
        if self.router == RouterType.VALHALLA:
            if self.endpoint == RouterEndpoint.ELEVATION:
                return "valhalla_elevation"
            return f"{self.router.lower()}_{self.endpoint.lower()}_{self.profile.lower()}"

        return f"{self.router.lower()}_{self.endpoint.lower()}"

    def displayName(self):
        if self.router == RouterType.VALHALLA:
            if self.endpoint == RouterEndpoint.ELEVATION:
                return "Elevation"
            return self.profile.capitalize()

        return self.endpoint.capitalize()  # there are no profiles for OSRM

    def shortHelpString(self):
        """Displays the sidebar help in the algorithm window"""

        file = HELP_DIR / Path(f"{self.router.lower()}_{self.endpoint.lower()}.help")

        with open(file) as fh:
            msg = fh.read()

        return msg
