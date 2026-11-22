import argparse
import csv
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from uuid import uuid4

import psycopg

from geodataops import db
from geodataops.inputs import CSV_FIELDS, InputError, read_inputs

SQL_HINTS = {
    "23503": "Verifica si la clave municipal del CSV existe en el GeoJSON o en staging.municipalities para el mismo conjunto.",
    "23505": "Revisa claves duplicadas y procesos externos que escriban en los esquemas del ETL.",
    "23514": "Inspecciona la restricción SQL indicada y valida la observación o geometría de origen.",
    "42501": "Verifica los permisos de la cuenta y la inicialización de PostGIS.",
    "55P03": "Espera a que finalice la ejecución concurrente y vuelve a lanzar el lote.",
}


def _json(value):
    return json.dumps(value, ensure_ascii=True, default=str, allow_nan=False)


def _log(event, *, level="info", **fields):
    entry = {"timestamp": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields}
    print(_json(entry), file=sys.stderr, flush=True)


def _diagnostic(exc, phase):
    result = {"phase": phase, "type": type(exc).__name__}
    if isinstance(exc, psycopg.Error):
        if exc.sqlstate:
            result.update(
                sqlstate=exc.sqlstate,
                message=exc.diag.message_primary,
                table=exc.diag.table_name,
                constraint=exc.diag.constraint_name,
                hint=SQL_HINTS.get(exc.sqlstate, "Consulta el estado SQL y los logs de PostgreSQL."),
            )
        else:
            result["message"] = "PostgreSQL no respondió o cerró la conexión. Revisa el health check y la configuración PG*."
    elif isinstance(exc, db.ConsistencyError):
        result.update(message=str(exc), cvegeo=exc.cvegeo, periodo=exc.periodo)
    elif isinstance(exc, (InputError, OSError)):
        result["message"] = str(exc)
    else:
        result["message"] = "La ejecución se interrumpió de forma inesperada."
    return result


def publish_csv(rows, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False
        ) as stream:
            temp_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, "valor": format(row["valor"], "f")})
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.chmod(0o644)
        temp_path.replace(output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _run(args):
    data_dir = Path("data") / args.dataset
    csv_path = args.csv or data_dir / ("poblacion.csv" if args.dataset == "jalisco" else "indicadores.csv")
    geojson_path = None if args.municipios_url else args.geojson or data_dir / "municipios.geojson"
    output_name = "municipios_indicadores.csv" if args.dataset == "jalisco" else "municipios_indicadores_demo.csv"
    output = args.output or Path("output") / output_name
    run_id = str(uuid4())
    fields = {"run_id": run_id, "dataset": args.dataset}
    started = monotonic()
    phase = "connect"
    try:
        with db.connect(args.dsn) as conn:
            phase = "lock"
            with db.pipeline_lock(conn):
                phase = "init"
                db.init_schema(conn)
                phase = "start"
                db.start_run(conn, run_id, args.dataset)
                report = {"db_committed": False, "csv_published": False}
                _log("etl_started", **fields)
                try:
                    phase = "extract_validate"
                    municipalities, indicators, raw_rows = read_inputs(
                        csv_path, geojson_path, geojson_url=args.municipios_url, dataset=args.dataset
                    )
                    report.update(municipalities_read=len(municipalities), indicators_read=len(indicators))
                    phase = "load"
                    report.update(db.load(conn, run_id, args.dataset, municipalities, indicators, raw_rows))
                    report["db_committed"] = True
                    phase = "export"
                    rows = db.export_rows(conn, args.dataset)
                    publish_csv(rows, output)
                    report.update(csv_published=True, exported_rows=len(rows), output=output.as_posix())
                except (Exception, KeyboardInterrupt) as exc:
                    if phase == "load" and (conn.broken or isinstance(exc, KeyboardInterrupt)):
                        report["db_committed"] = None
                    error = _diagnostic(exc, phase)
                    _log("etl_failed", level="error", **fields, error=error, report=report)
                    try:
                        db.finish_run(conn, run_id, error=error, report=report)
                    except psycopg.Error as ledger_error:
                        _log("ledger_update_failed", level="error", **fields, error=_diagnostic(ledger_error, "ledger"))
                    return 130 if isinstance(exc, KeyboardInterrupt) else 1
                phase = "finish"
                db.finish_run(conn, run_id, report=report)
    except (psycopg.Error, OSError) as exc:
        _log("etl_failed", level="error", **fields, error=_diagnostic(exc, phase))
        return 1
    _log("etl_succeeded", **fields, elapsed_ms=round((monotonic() - started) * 1000), report=report)
    return 0


def _positive_limit(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("usa un valor entre 1 y 100")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Carga y diagnóstico del ETL municipal")
    parser.add_argument("--dsn", default=os.environ.get("GEODATAOPS_DSN", ""), help="DSN; también GEODATAOPS_DSN o variables libpq PG*")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Inicializa los esquemas y la extensión PostGIS")
    commands.add_parser("health", help="Comprueba conectividad, PostGIS y esquemas")
    diagnose = commands.add_parser("diagnose", help="Muestra el estado y los errores de ejecuciones recientes")
    diagnose.add_argument("--limit", type=_positive_limit, default=10)
    run = commands.add_parser("run", help="Extrae, valida, carga y publica el CSV")
    run.add_argument("--dataset", choices=("jalisco", "demo"), default="jalisco")
    run.add_argument("--csv", type=Path)
    geography = run.add_mutually_exclusive_group()
    geography.add_argument("--geojson", type=Path)
    geography.add_argument("--municipios-url", help="URL HTTPS de una colección municipal GeoJSON")
    run.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run(args)
    try:
        with db.connect(args.dsn) as conn:
            if args.command == "init":
                db.init_schema(conn)
                _log("schema_initialized")
            elif args.command == "health":
                checks = db.health(conn)
                print(_json(checks))
                return int(checks["status"] != "ok")
            else:
                print(_json(db.recent_runs(conn, args.limit)))
    except (psycopg.Error, OSError) as exc:
        _log("command_failed", level="error", error=_diagnostic(exc, args.command))
        return 1
    return 0
