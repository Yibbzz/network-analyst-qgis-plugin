"""
Builds vrp_corridor.osm.pbf, a tiny made-up road network with exact travel times for the VRP
tests (tests/test_localhost_docker/.../test_vrp_corridor.py). Served by its own Valhalla
(port 8003, see the CI workflows): built together with Andorra, valhalla_build_tiles ignored it.

On the equator at 10°E, where nothing else is, so 1 km is the same in x and y:
  - Main Road: 20 km west -> east from (10, 0), primary, maxspeed 60 (Valhalla drives 85%:
    70.588 s/km), a vertex every km
  - Side Road: 5 km north from Main Road's km 10, residential, maxspeed 30 (225 s/km)

Run with NetworkForge (https://github.com/Yibbzz/networkforge) installed:
    python make_corridor.py
"""

import subprocess
from pathlib import Path

from osgeo import ogr, osr

HERE = Path(__file__).parent
KM = 1 / 111.319490793  # degrees per km at the equator
X0, Y0 = 10.0, 0.0


def main():
    ogr.UseExceptions()
    streets = HERE / "streets.gpkg"
    streets.unlink(missing_ok=True)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(streets))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layer = ds.CreateLayer("streets", srs, ogr.wkbLineString)
    for name in ("name", "highway", "maxspeed"):
        layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))

    def line(name, highway, maxspeed, points):
        geom = ogr.Geometry(ogr.wkbLineString)
        for x, y in points:
            geom.AddPoint_2D(x, y)
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetGeometry(geom)
        feat.SetField("name", name)
        feat.SetField("highway", highway)
        feat.SetField("maxspeed", maxspeed)
        layer.CreateFeature(feat)

    line("Main Road", "primary", "60", [(X0 + k * KM, Y0) for k in range(21)])
    line("Side Road", "residential", "30", [(X0 + 10 * KM, Y0 + k * KM) for k in range(6)])
    ds = None

    out = HERE / "vrp_corridor.osm.pbf"
    subprocess.run(
        ["networkforge", "build", "--no-osm", "--custom", str(streets), "--out", str(out)], check=True
    )
    streets.unlink()


if __name__ == "__main__":
    main()
