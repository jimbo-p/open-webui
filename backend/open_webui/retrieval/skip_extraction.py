"""Fork-only: skip Tika and embeddings for formats that are never worth it.

Zip-of-documents is still extracted. These are coordinate/binary/array
containers: even unzipped they produce garbage chunks.
"""

from pathlib import Path

# GIS / CAD / meshes / scientific arrays. Not archives, office, pdf, or geojson.
SKIP_EXTRACTION_EXTENSIONS = frozenset(
    {
        # geospatial vectors / packages
        'kml',
        'kmz',
        'gpx',
        'gml',
        'shp',
        'shx',
        'dbf',
        'prj',
        'sbn',
        'sbx',
        'cpg',
        'qix',
        'gpkg',
        'gdb',
        'mbtiles',
        'mif',
        'mid',
        'osm',
        'pbf',
        'grib',
        'grib2',
        'grb',
        'las',
        'laz',
        'copc',
        'e00',
        'adf',
        'bil',
        'bip',
        'bsq',
        'sid',
        'ecw',
        # CAD / 3D
        'dwg',
        'dxf',
        'dgn',
        'stl',
        'glb',
        'gltf',
        'fbx',
        '3ds',
        'iges',
        'igs',
        'step',
        'stp',
        # scientific / columnar binary
        'nc',
        'cdf',
        'hdf',
        'hdf5',
        'h5',
        'he5',
        'fits',
        'parquet',
        'pqt',
        'feather',
        'arrow',
        'orc',
        'avro',
        'npy',
        'npz',
        'sqlite',
        'sqlite3',
        'pcap',
    }
)

SKIP_EXTRACTION_MIME_TYPES = frozenset(
    {
        'application/vnd.google-earth.kml+xml',
        'application/vnd.google-earth.kmz',
        'application/gpx+xml',
        'application/gml+xml',
        'application/geopackage+sqlite3',
        'application/x-sqlite3',
        'application/vnd.sqlite3',
        'application/x-shapefile',
        'application/x-qgis',
        'application/x-ogc-gpkg',
        'application/vnd.las',
        'application/vnd.laszip',
        'application/x-netcdf',
        'application/x-hdf',
        'application/x-hdf5',
        'application/vnd.apache.parquet',
        'application/x-parquet',
        'application/vnd.apache.arrow.file',
        'application/vnd.apache.arrow.stream',
        'application/vnd.apache.orc',
        'application/x-pcap',
        'application/vnd.tcpdump.pcap',
        'image/vnd.dwg',
        'image/x-dxf',
        'model/gltf-binary',
        'model/gltf+json',
        'model/stl',
        'application/acad',
        'application/x-dwg',
        'application/dxf',
    }
)


def _extension(filename: str | None) -> str:
    if not filename:
        return ''
    return Path(filename).suffix.lower().lstrip('.')


def skip_extraction_reason(filename: str | None, content_type: str | None = None) -> str | None:
    """Return a short reason if this file must never be extracted or embedded."""
    ext = _extension(filename)
    if ext in SKIP_EXTRACTION_EXTENSIONS:
        return ext
    mime = (content_type or '').split(';', 1)[0].strip().lower()
    if mime in SKIP_EXTRACTION_MIME_TYPES:
        return mime
    return None


def skip_extraction_stub(filename: str, file_id: str, reason: str) -> str:
    name = filename or 'this file'
    return (
        f'[Skipped text extraction]\n'
        f'{name} is stored as the original blob (id {file_id}). '
        f'Tika/embeddings are skipped ({reason}): the extracted bytes would be '
        f'coordinates, meshes, or columnar binary, not searchable text. '
        f'Use python against the original file.'
    )
