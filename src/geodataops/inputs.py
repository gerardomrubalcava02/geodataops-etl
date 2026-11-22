"""Read municipal indicators and geometry from CSV, GeoJSON, or the INEGI API."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

CSV_FIELDS = ("cvegeo", "periodo", "indicador", "valor", "unidad", "fuente")
DEMO_SOURCE = "DEMO sintético (GeoDataOps)"
ITER_SOURCE = "INEGI | Censo de Población y Vivienda 2020 | ITER Jalisco"
ITER_POPULATION = {
    "POBTOT": "poblacion_total",
    "POBFEM": "poblacion_femenina",
    "POBMAS": "poblacion_masculina",
}
ITER_FIELDS = {"ENTIDAD", "MUN", "LOC", *ITER_POPULATION}
MUNICIPIO = re.compile(r"(?:0[1-9]|[12][0-9]|3[0-2])(?!000)[0-9]{3}")
PERIOD = re.compile(r"[1-9][0-9]{3}")
INDICATOR = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
UNSUPPORTED_JSON_TEXT = re.compile(r"[\x00\ud800-\udfff]")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


class InputError(ValueError):
    """An input cannot be extracted or fails the data contract."""


def _text(value: object, field: str, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InputError(f"{location}: {field} debe contener texto válido")
    return value.strip()


def _municipal_code(value: object, location: str) -> str:
    code = _text(value, "cvegeo", location)
    if not MUNICIPIO.fullmatch(code):
        raise InputError(f"{location}: cvegeo debe ser una clave municipal INEGI de cinco dígitos")
    return code


def _indicator(row: dict, location: str) -> dict:
    code = _municipal_code(row["cvegeo"], location)
    period = _text(row["periodo"], "periodo", location)
    if not PERIOD.fullmatch(period):
        raise InputError(f"{location}: periodo debe ser un año de cuatro dígitos (1000–9999)")

    name = _text(row["indicador"], "indicador", location)
    if not INDICATOR.fullmatch(name):
        raise InputError(f"{location}: indicador debe usar letras minúsculas, dígitos y guion bajo")

    number = _text(row["valor"], "valor", location)
    if not NUMBER.fullmatch(number):
        raise InputError(f"{location}: valor debe ser un decimal finito con punto como separador")
    value = Decimal(number)
    unit = _text(row["unidad"], "unidad", location)
    if name in ITER_POPULATION.values() and (
        unit != "personas" or value < 0 or value != value.to_integral_value()
    ):
        raise InputError(f"{location}: {name} debe ser un entero no negativo en personas")

    return {
        "cvegeo": code,
        "periodo": int(period),
        "indicador": name,
        "valor": value,
        "unidad": unit,
        "fuente": _text(row["fuente"], "fuente", location),
    }


def _read_bounded(stream, label: str) -> bytes:
    data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise InputError(f"{label}: la fuente supera 64 MiB")
    return data


def _file_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            return _read_bounded(stream, path.name)
    except OSError as error:
        raise InputError(f"{path.name}: no se pudo leer el archivo ({error.strerror})") from error


def _csv_bytes(path: Path) -> tuple[str, bytes]:
    if path.suffix.lower() != ".zip":
        return path.name, _file_bytes(path)

    try:
        with ZipFile(path) as archive:
            candidates = [
                member
                for member in archive.infolist()
                if not member.is_dir()
                and member.filename.lower().endswith(".csv")
                and "conjunto_de_datos" in PurePosixPath(member.filename.lower()).parts
            ]
            if len(candidates) != 1:
                raise InputError(f"{path.name}: se esperaba un CSV dentro de conjunto_de_datos/")
            member = candidates[0]
            label = f"{path.name}:{PurePosixPath(member.filename).name}"
            if member.file_size > MAX_SOURCE_BYTES:
                raise InputError(f"{label}: la fuente supera 64 MiB")
            with archive.open(member) as stream:
                return label, _read_bounded(stream, label)
    except (OSError, BadZipFile, NotImplementedError, RuntimeError) as error:
        raise InputError(f"{path.name}: no se pudo leer el ZIP de ITER") from error


def _decode_csv(data: bytes, source: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Older INEGI archives have been distributed with Windows-encoded text.
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError as error:
            raise InputError(f"{source}: la codificación del CSV no es válida") from error


def _iter_indicators(row: dict, location: str) -> list[dict]:
    if row["ENTIDAD"].strip() != "14" or row["LOC"].strip().zfill(4) != "0000":
        return []
    municipality = row["MUN"].strip()
    if not re.fullmatch(r"[0-9]{1,3}", municipality):
        raise InputError(f"{location}: MUN debe contener entre 1 y 3 dígitos")
    if int(municipality) == 0:
        return []
    return [
        _indicator(
            {
                "cvegeo": f"14{municipality.zfill(3)}",
                "periodo": "2020",
                "indicador": indicator,
                "valor": row[column],
                "unidad": "personas",
                "fuente": ITER_SOURCE,
            },
            location,
        )
        for column, indicator in ITER_POPULATION.items()
    ]


def _validate_population(rows: list[dict]) -> None:
    population = {}
    for row in rows:
        if row["indicador"] in ITER_POPULATION.values():
            key = (row["cvegeo"], row["periodo"], row["fuente"])
            population.setdefault(key, {})[row["indicador"]] = int(row["valor"])

    for (code, period, _source), values in population.items():
        if len(values) == len(ITER_POPULATION) and values["poblacion_total"] != (
            values["poblacion_femenina"] + values["poblacion_masculina"]
        ):
            raise InputError(f"{code}/{period}: poblacion_total difiere de la suma de sus componentes")


def _read_indicators(path: Path, dataset: str) -> tuple[list[dict], list[dict]]:
    source, data = _csv_bytes(path)
    reader = csv.DictReader(io.StringIO(_decode_csv(data, source), newline=""), strict=True)
    indicators, raw, keys = [], [], set()
    try:
        header = tuple(reader.fieldnames or ())
        is_iter = ITER_FIELDS.issubset(header) and len(header) == len(set(header))
        if header != CSV_FIELDS and not is_iter:
            raise InputError(f"{source}: encabezado desconocido, se esperaba el contrato CSV o ITER 2020")
        if is_iter and dataset == "demo":
            raise InputError(f"{source}: utiliza el dataset real para un archivo de INEGI")

        for row in reader:
            location = f"{source}:{reader.line_num}"
            if None in row or any(value is None or "\x00" in value for value in row.values()):
                raise InputError(f"{location}: columnas incompletas, sobrantes o con caracteres NUL")
            normalized = _iter_indicators(row, location) if is_iter else [_indicator(row, location)]
            if not normalized:
                continue

            for item in normalized:
                if dataset != "demo" and item["fuente"] == DEMO_SOURCE:
                    raise InputError(f"{location}: la fuente sintética requiere el dataset demo")
                key = (item["cvegeo"], item["periodo"], item["indicador"])
                if key in keys:
                    raise InputError(f"{location}: indicador duplicado {key}")
                keys.add(key)
                indicators.append(item)
            raw.append({"source": source, "payload": row})
    except csv.Error as error:
        raise InputError(f"{source}:{reader.line_num}: CSV inválido ({error})") from error

    if not indicators:
        raise InputError(f"{source}: el CSV no contiene indicadores municipales para cargar")
    _validate_population(indicators)
    return indicators, raw


def _public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname and parsed.username is None:
            return parsed._replace(query="", fragment="").geturl()
    except ValueError:
        pass
    raise InputError("La URL de GeoJSON debe usar HTTPS y carecer de credenciales")


class _HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _api_bytes(url: str) -> tuple[str, bytes]:
    label = _public_url(url)
    request = Request(url, headers={"Accept": "application/geo+json, application/json"})
    opener = build_opener(_HttpsRedirects())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=10) as response:
                return label, _read_bounded(response, label)
        except HTTPError as error:
            error.close()
            message = f"HTTP {error.code}"
            if error.code not in TRANSIENT_HTTP_STATUS or attempt == 2:
                raise InputError(f"{label}: no se pudo obtener el GeoJSON ({message})") from error
        except (URLError, OSError, HTTPException) as error:
            if attempt == 2:
                raise InputError(f"{label}: no se pudo obtener el GeoJSON tras tres intentos") from error
        time.sleep(0.5 * 2**attempt)
    raise AssertionError("Se agotaron los intentos sin resultado")


def _validate_position(position: object, location: str) -> None:
    if not isinstance(position, list) or len(position) != 2:
        raise InputError(f"{location}: cada posición debe tener longitud y latitud")
    if any(type(axis) not in (float, int) for axis in position):
        raise InputError(f"{location}: las coordenadas deben ser numéricas")
    longitude, latitude = position
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise InputError(f"{location}: coordenadas fuera del rango WGS84")


def _geometry(geometry: object, location: str) -> dict:
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise InputError(f"{location}: se esperaba una geometría Polygon o MultiPolygon")
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise InputError(f"{location}: geometría sin coordenadas")
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise InputError(f"{location}: polígono sin anillos")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise InputError(f"{location}: el anillo debe estar cerrado y tener al menos cuatro posiciones")
            for position in ring:
                _validate_position(position, location)
    return {"type": geometry["type"], "coordinates": coordinates}


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Número JSON no finito")
    return number


def _validate_json_text(value: object) -> None:
    if isinstance(value, str) and UNSUPPORTED_JSON_TEXT.search(value):
        raise ValueError("El GeoJSON contiene texto incompatible con PostgreSQL")
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_text(key)
            _validate_json_text(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json_text(item)


def _read_municipalities(data: bytes, source: str, dataset: str) -> tuple[list[dict], list[dict]]:
    try:
        document = json.loads(data.decode("utf-8-sig"), parse_float=_json_float, parse_constant=_json_float)
        _validate_json_text(document)
    except (ValueError, RecursionError) as error:
        raise InputError(f"{source}: GeoJSON inválido") from error

    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise InputError(f"{source}: se esperaba una FeatureCollection GeoJSON")
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise InputError(f"{source}: la FeatureCollection no contiene municipios")

    municipalities, raw, codes = [], [], set()
    for index, feature in enumerate(features, 1):
        location = f"{source} feature {index}"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise InputError(f"{location}: se esperaba un objeto Feature")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise InputError(f"{location}: se esperaban propiedades municipales")
        if dataset != "demo" and properties.get("source") == DEMO_SOURCE:
            raise InputError(f"{location}: la geometría sintética requiere el dataset demo")
        code = _municipal_code(properties.get("cvegeo"), location)
        if code in codes:
            raise InputError(f"{location}: municipio duplicado {code}")
        codes.add(code)
        municipalities.append(
            {
                "cvegeo": code,
                "name": _text(properties.get("name", properties.get("nomgeo")), "nombre", location),
                "geometry": _geometry(feature.get("geometry"), location),
            }
        )
        raw.append({"source": source, "payload": feature})
    return municipalities, raw


def read_inputs(
    csv_path: Path,
    geojson_path: Path | None = None,
    *,
    geojson_url: str | None = None,
    dataset: str = "demo",
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return normalized municipalities and indicators followed by their source records."""
    if (geojson_path is None) == (geojson_url is None):
        raise InputError("Indica un archivo GeoJSON o una URL GeoJSON")

    indicators, raw_csv = _read_indicators(csv_path, dataset)
    if geojson_path is not None:
        source, data = geojson_path.name, _file_bytes(geojson_path)
    else:
        source, data = _api_bytes(geojson_url)
    municipalities, raw_geojson = _read_municipalities(data, source, dataset)
    return municipalities, indicators, raw_csv + raw_geojson
