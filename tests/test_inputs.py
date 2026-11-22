import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from zipfile import ZipFile

import pytest

from geodataops import inputs


DATA = Path(__file__).resolve().parents[1] / "data"
DEMO_CSV = DATA / "demo" / "indicadores.csv"
DEMO_GEOJSON = DATA / "demo" / "municipios.geojson"
REAL_CSV = DATA / "jalisco" / "poblacion.csv"
REAL_GEOJSON = DATA / "jalisco" / "municipios.geojson"


def observation(**overrides):
    return {
        "cvegeo": "14039",
        "periodo": "2020",
        "indicador": "poblacion_total",
        "valor": "32",
        "unidad": "personas",
        "fuente": inputs.DEMO_SOURCE,
        **overrides,
    }


def write_csv(path, *rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=inputs.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.parametrize(
    ("dataset", "folder", "filename", "expected_periods", "expected_count"),
    [
        ("demo", "demo", "indicadores.csv", {2020, 2021, 2022}, 27),
        ("jalisco", "jalisco", "poblacion.csv", {2010, 2020}, 18),
    ],
)
def test_loads_bundled_datasets(dataset, folder, filename, expected_periods, expected_count):
    municipalities, indicators, raw = inputs.read_inputs(
        DATA / folder / filename, DATA / folder / "municipios.geojson", dataset=dataset
    )

    expected_codes = {"14039", "14098", "14120"}
    assert {item["cvegeo"] for item in municipalities} == expected_codes
    assert {item["cvegeo"] for item in indicators} == expected_codes
    assert {item["periodo"] for item in indicators} == expected_periods
    assert {item["indicador"] for item in indicators} == {
        "poblacion_total", "poblacion_femenina", "poblacion_masculina"
    }
    assert len(indicators) == expected_count
    assert len(raw) == len(municipalities) + len(indicators)
    assert all(isinstance(item["valor"], Decimal) for item in indicators)
    if dataset == "demo":
        assert {item["fuente"] for item in indicators} == {inputs.DEMO_SOURCE}
    else:
        assert all("IIEG con datos de INEGI" in item["fuente"] for item in indicators)


def test_demo_source_requires_demo_dataset():
    with pytest.raises(inputs.InputError, match="fuente sintética"):
        inputs.read_inputs(DEMO_CSV, REAL_GEOJSON, dataset="jalisco")


def test_demo_source_marker_applies_to_other_fixtures(tmp_path):
    path = write_csv(tmp_path / "observations.csv", observation(fuente="DEMO sintético (lote de prueba alterno)"))
    with pytest.raises(inputs.InputError, match="fuente sintética"):
        inputs.read_inputs(path, REAL_GEOJSON, dataset="jalisco")


def test_demo_geometry_requires_demo_dataset():
    with pytest.raises(inputs.InputError, match="sintétic"):
        inputs.read_inputs(REAL_CSV, DEMO_GEOJSON, dataset="jalisco")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cvegeo", "4039"),
        ("cvegeo", "14000"),
        ("cvegeo", "１４０３９"),
        ("periodo", "0999"),
        ("periodo", "20200"),
        ("indicador", "Población Total"),
        ("valor", "NaN"),
        ("valor", "Infinity"),
        ("valor", "-1"),
        ("valor", "1.5"),
        ("valor", "1,200"),
        ("unidad", ""),
        ("fuente", " "),
    ],
)
def test_csv_rejects_invalid_observation(tmp_path, field, value):
    path = write_csv(tmp_path / "observations.csv", observation(**{field: value}))

    with pytest.raises(inputs.InputError, match="observations.csv:2"):
        inputs.read_inputs(path, DEMO_GEOJSON)


def test_csv_preserves_leading_zero_and_decimal_precision(tmp_path):
    path = write_csv(
        tmp_path / "observations.csv",
        observation(cvegeo="01001", indicador="densidad", valor="12.3400", unidad="personas/km²"),
    )

    _, indicators, _ = inputs.read_inputs(path, DEMO_GEOJSON)

    assert indicators[0]["cvegeo"] == "01001"
    assert indicators[0]["valor"].as_tuple() == Decimal("12.3400").as_tuple()


def test_csv_rejects_duplicate_key_even_if_value_changes(tmp_path):
    path = write_csv(tmp_path / "observations.csv", observation(), observation(valor="33"))

    with pytest.raises(inputs.InputError, match="indicador duplicado"):
        inputs.read_inputs(path, DEMO_GEOJSON)


def test_csv_rejects_inconsistent_population_total(tmp_path):
    path = write_csv(
        tmp_path / "observations.csv",
        observation(valor="32"),
        observation(indicador="poblacion_femenina", valor="19"),
        observation(indicador="poblacion_masculina", valor="14"),
    )

    with pytest.raises(inputs.InputError, match="14039/2020.*suma"):
        inputs.read_inputs(path, DEMO_GEOJSON)


def test_iter_zip_selects_only_municipal_jalisco_totals(tmp_path):
    path = tmp_path / "iter.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "ITER/conjunto_de_datos/iter_14.csv",
            "ENTIDAD,MUN,LOC,POBTOT,POBFEM,POBMAS\n"
            "14,000,0000,150,80,70\n"
            "13,039,0000,52,23,29\n"
            "14,039,0001,24,14,10\n"
            "14,39,0,32,19,13\n",
        )
        archive.writestr("ITER/diccionario_de_datos/diccionario.csv", "campo,descripción\n")

    _, indicators, raw = inputs.read_inputs(path, REAL_GEOJSON, dataset="jalisco")

    assert {row["cvegeo"] for row in indicators} == {"14039"}
    assert {row["periodo"] for row in indicators} == {2020}
    assert {row["indicador"]: row["valor"] for row in indicators} == {
        "poblacion_total": Decimal("32"),
        "poblacion_femenina": Decimal("19"),
        "poblacion_masculina": Decimal("13"),
    }
    assert raw[0]["source"] == "iter.zip:iter_14.csv"
    assert raw[0]["payload"]["MUN"] == "39"


def test_geojson_rejects_duplicate_municipality(tmp_path):
    data = json.loads(DEMO_GEOJSON.read_text(encoding="utf-8"))
    data["features"].append(data["features"][0])
    path = tmp_path / "duplicated.geojson"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(inputs.InputError, match="municipio duplicado"):
        inputs.read_inputs(DEMO_CSV, path)


@pytest.mark.parametrize(
    ("closing_point", "message"),
    [([2, 2], "anillo debe estar cerrado"), ([float("inf"), 0], "GeoJSON inválido")],
)
def test_geojson_rejects_invalid_ring(tmp_path, closing_point, message):
    data = json.loads(DEMO_GEOJSON.read_text(encoding="utf-8"))
    data["features"][0]["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], closing_point]],
    }
    path = tmp_path / "invalid.geojson"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(inputs.InputError, match=message):
        inputs.read_inputs(DEMO_CSV, path)


def test_geojson_api_retries_and_keeps_query_out_of_provenance(monkeypatch):
    url = "https://example.test/municipios"
    requests = []
    waits = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request.full_url)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO())
            return io.BytesIO(REAL_GEOJSON.read_bytes())

    monkeypatch.setattr(inputs, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(inputs.time, "sleep", waits.append)
    query_url = f"{url}?state=14"

    municipalities, _, raw = inputs.read_inputs(REAL_CSV, geojson_url=query_url, dataset="jalisco")

    assert requests == [query_url, query_url]
    assert len(waits) == 1
    assert len(municipalities) == 3
    assert all(row["source"] == url for row in raw[-3:])


@pytest.mark.parametrize("url", ["http://example.test/municipios", "https://user@example.test/"])
def test_geojson_api_requires_https_without_credentials(url):
    with pytest.raises(inputs.InputError, match="HTTPS"):
        inputs.read_inputs(REAL_CSV, geojson_url=url, dataset="jalisco")
