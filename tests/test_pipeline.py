import csv
import json

import pytest

from geodataops import db
from geodataops.cli import main
from geodataops.inputs import CSV_FIELDS, DEMO_SOURCE

pytestmark = pytest.mark.integration


def _read_export(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == CSV_FIELDS
        return list(reader)


def _run_history(dsn, capsys):
    capsys.readouterr()
    assert main(["--dsn", dsn, "diagnose"]) == 0
    return json.loads(capsys.readouterr().out)


def _write_demo_batch(path, rows, *, source=DEMO_SOURCE):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_FIELDS)
        writer.writerows((*row, "personas", source) for row in rows)


def test_default_real_load_and_idempotence(dsn, tmp_path, capsys):
    output = tmp_path / "export.csv"
    assert main(["--dsn", dsn, "health"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"

    command = ["--dsn", dsn, "run", "--output", str(output)]
    assert main(command) == 0
    first_export = output.read_bytes()
    rows = _read_export(output)
    assert len(rows) == 18
    keys = [(row["cvegeo"], row["periodo"], row["indicador"]) for row in rows]
    assert keys == sorted(set(keys))
    assert all("DEMO" not in row["fuente"] for row in rows)
    with db.connect(dsn) as conn:
        counts = conn.execute(
            """SELECT periodo, municipality_count FROM analytics.indicator_summary
               WHERE dataset = 'jalisco' AND indicador = 'poblacion_total' ORDER BY periodo"""
        ).fetchall()
        assert [(row["periodo"], row["municipality_count"]) for row in counts] == [(2010, 3), (2020, 3)]
        geom = conn.execute(
            """SELECT ST_SRID(geometry) AS srid, ST_IsValid(geometry) AS valid,
                      ST_Area(geometry::geography) > 0 AS positive_area
               FROM staging.municipalities WHERE cvegeo = '14039'"""
        ).fetchone()
        assert geom == {"srid": 4326, "valid": True, "positive_area": True}

    assert main(command) == 0
    assert output.read_bytes() == first_export
    history = _run_history(dsn, capsys)
    assert [row["status"] for row in history] == ["succeeded", "succeeded"]
    assert history[1]["report"]["indicators_upserted"] == 18
    for key in ("raw_inserted", "municipalities_upserted", "indicators_upserted"):
        assert history[0]["report"][key] == 0
    assert main(["--dsn", dsn, "health"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_sql_failure_rollback_and_corrected_replay(dsn, tmp_path, capsys):
    output = tmp_path / "demo.csv"
    batch = tmp_path / "batch.csv"
    command = ["--dsn", dsn, "run", "--dataset", "demo", "--output", str(output)]
    assert main(command) == 0
    original = output.read_bytes()
    with db.connect(dsn) as conn:
        raw_count = conn.execute("SELECT count(*) AS n FROM raw.source_records").fetchone()["n"]

    _write_demo_batch(batch, [("14039", 2025, "poblacion_total", 123), ("14999", 2025, "poblacion_total", 456)])
    assert main([*command, "--csv", str(batch)]) == 1
    messages = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert messages[-1]["error"]["sqlstate"] == "23503"
    assert output.read_bytes() == original
    history = _run_history(dsn, capsys)
    assert history[0]["status"] == "failed"
    assert history[0]["error"]["constraint"] == "indicators_municipality_fk"
    assert history[0]["report"]["db_committed"] is False
    with db.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.source_records").fetchone()["n"] == raw_count
        assert len(db.export_rows(conn, "demo")) == 27

    _write_demo_batch(batch, [("14039", 2025, "poblacion_total", 123), ("14098", 2025, "poblacion_total", 456)])
    assert main([*command, "--csv", str(batch)]) == 0
    recovered = _read_export(output)
    assert len(recovered) == 29
    assert {row["cvegeo"]: row["valor"] for row in recovered if row["periodo"] == "2025"} == {
        "14039": "123", "14098": "456",
    }
    history = _run_history(dsn, capsys)
    assert [row["status"] for row in history] == ["succeeded", "failed", "succeeded"]
    assert history[0]["report"]["indicators_upserted"] == 2


def test_recovery_after_database_commit_and_csv_failure(dsn, tmp_path, capsys):
    blocked_output = tmp_path / "directory.csv"
    blocked_output.mkdir()
    command = ["--dsn", dsn, "run", "--dataset", "demo", "--output"]
    assert main([*command, str(blocked_output)]) == 1
    history = _run_history(dsn, capsys)
    assert history[0]["error"]["phase"] == "export"
    assert history[0]["report"]["db_committed"] is True
    assert history[0]["report"]["csv_published"] is False

    output = tmp_path / "recovered.csv"
    assert main([*command, str(output)]) == 0
    assert len(_read_export(output)) == 27
    history = _run_history(dsn, capsys)
    assert history[0]["report"]["indicators_upserted"] == 0
    assert history[0]["report"]["csv_published"] is True


@pytest.mark.parametrize(
    "source", [DEMO_SOURCE, f"{DEMO_SOURCE} | revisión"], ids=["same-source", "mixed-source"]
)
def test_consistency_across_incremental_batches(dsn, tmp_path, capsys, source):
    output = tmp_path / "demo.csv"
    batch = tmp_path / "update.csv"
    command = ["--dsn", dsn, "run", "--dataset", "demo", "--output", str(output)]
    assert main(command) == 0
    rows = _read_export(output)
    current = {
        row["indicador"]: int(row["valor"]) for row in rows
        if row["cvegeo"] == "14039" and row["periodo"] == "2020"
    }
    total = current["poblacion_total"] + 1
    _write_demo_batch(batch, [("14039", 2020, "poblacion_total", total)], source=source)
    assert main([*command, "--csv", str(batch)]) == 1
    assert _read_export(output) == rows
    failure = _run_history(dsn, capsys)[0]
    assert failure["report"]["db_committed"] is False
    assert failure["error"]["type"] == "ConsistencyError"
    assert (failure["error"]["cvegeo"], failure["error"]["periodo"]) == ("14039", 2020)

    female = current["poblacion_femenina"] + 1
    _write_demo_batch(
        batch,
        [("14039", 2020, "poblacion_total", total), ("14039", 2020, "poblacion_femenina", female)],
        source=source,
    )
    assert main([*command, "--csv", str(batch)]) == 0
    assert _run_history(dsn, capsys)[0]["report"]["indicators_upserted"] == 2
    recovered = _read_export(output)
    assert len(recovered) == len(rows)
    assert {
        row["indicador"]: (int(row["valor"]), row["fuente"]) for row in recovered
        if row["cvegeo"] == "14039" and row["periodo"] == "2020"
    } == {
        "poblacion_total": (total, source),
        "poblacion_femenina": (female, source),
        "poblacion_masculina": (current["poblacion_masculina"], DEMO_SOURCE),
    }
