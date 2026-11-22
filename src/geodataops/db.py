"""PostgreSQL transactions, ETL run history, and analytics reads."""

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from importlib.resources import files
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

Connection = psycopg.Connection[dict[str, Any]]
Row = Mapping[str, Any]

# All runs share the CSV destination by default and serialize publication.
PIPELINE_LOCK_KEY = 0x47454F444154414F

_INSERT_RAW = """
    INSERT INTO raw.source_records (dataset, source, content_sha256, payload, first_run_id)
    VALUES (%s, %s, %s, %s::jsonb, %s)
    ON CONFLICT (dataset, source, content_sha256) DO NOTHING
"""

_UPSERT_MUNICIPALITY = """
    INSERT INTO staging.municipalities AS target
        (dataset, cvegeo, name, geometry, last_run_id)
    VALUES (
        %s, %s, %s,
        public.ST_Multi(public.ST_Transform(public.ST_GeomFromGeoJSON(%s), 4326)),
        %s
    )
    ON CONFLICT (dataset, cvegeo) DO UPDATE SET
        name = EXCLUDED.name,
        geometry = EXCLUDED.geometry,
        last_run_id = EXCLUDED.last_run_id,
        updated_at = clock_timestamp()
    WHERE target.name IS DISTINCT FROM EXCLUDED.name
       OR public.ST_AsEWKB(target.geometry)
           IS DISTINCT FROM public.ST_AsEWKB(EXCLUDED.geometry)
"""

_UPSERT_INDICATOR = """
    INSERT INTO staging.indicators AS target
        (dataset, cvegeo, periodo, indicador, valor, unidad, fuente, last_run_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (dataset, cvegeo, periodo, indicador) DO UPDATE SET
        valor = EXCLUDED.valor,
        unidad = EXCLUDED.unidad,
        fuente = EXCLUDED.fuente,
        last_run_id = EXCLUDED.last_run_id,
        updated_at = clock_timestamp()
    WHERE (target.valor, target.unidad, target.fuente)
          IS DISTINCT FROM (EXCLUDED.valor, EXCLUDED.unidad, EXCLUDED.fuente)
"""

_POPULATION_CONFLICT = """
    WITH affected AS (
        SELECT DISTINCT dataset, cvegeo, periodo
        FROM staging.indicators
        WHERE dataset = %s AND last_run_id = %s
    )
    SELECT i.cvegeo, i.periodo
    FROM affected
    JOIN staging.indicators AS i USING (dataset, cvegeo, periodo)
    WHERE i.indicador IN ('poblacion_total', 'poblacion_femenina', 'poblacion_masculina')
    GROUP BY i.cvegeo, i.periodo
    HAVING count(*) = 3
       AND max(i.valor) FILTER (WHERE i.indicador = 'poblacion_total')
           <> sum(i.valor) FILTER (WHERE i.indicador <> 'poblacion_total')
    ORDER BY i.cvegeo, i.periodo
    LIMIT 1
"""


class ConsistencyError(ValueError):
    """A population update disagrees with measurements already loaded."""

    def __init__(self, cvegeo: str, periodo: int) -> None:
        self.cvegeo = cvegeo
        self.periodo = periodo
        super().__init__(
            f"{cvegeo}/{periodo}: poblacion_total difiere de la suma de sus componentes"
        )


def connect(dsn: str) -> Connection:
    return psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        application_name="geodataops_etl",
    )


@contextmanager
def pipeline_lock(conn: Connection) -> Iterator[None]:
    """Hold a session lock across database commits and CSV publication."""
    conn.execute("SELECT pg_advisory_lock(%s::bigint)", (PIPELINE_LOCK_KEY,))
    try:
        yield
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (PIPELINE_LOCK_KEY,))


def init_schema(conn: Connection) -> None:
    schema = files("geodataops").joinpath("schema.sql").read_text(encoding="utf-8")
    with pipeline_lock(conn), conn.transaction():
        conn.execute(schema, prepare=False)


def start_run(conn: Connection, run_id: UUID | str, dataset: str) -> None:
    """Record a run while the caller holds the pipeline lock."""
    with conn.transaction():
        conn.execute(
            """
            UPDATE raw.etl_runs
            SET status = 'failed', finished_at = clock_timestamp(),
                error = jsonb_build_object(
                    'phase', 'unknown',
                    'reason', 'interrupted_run',
                    'message', 'La ejecución anterior terminó sin registrar su estado final.'
                )
            WHERE status = 'running'
            """
        )
        conn.execute(
            "INSERT INTO raw.etl_runs (run_id, dataset) VALUES (%s, %s)",
            (run_id, dataset),
        )


def _raw_params(
    records: Iterable[Row], dataset: str, run_id: UUID | str
) -> Iterator[tuple[str, str, bytes, str, UUID | str]]:
    for record in records:
        payload = json.dumps(
            record["payload"],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        yield dataset, record["source"], digest, payload, run_id


def load(
    conn: Connection,
    run_id: UUID | str,
    dataset: str,
    municipalities: Iterable[Row],
    indicators: Iterable[Row],
    raw_records: Iterable[Row],
) -> dict[str, int]:
    """Write the raw and staging layers in a single transaction."""
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM raw.etl_runs
            WHERE run_id = %s AND dataset = %s AND status = 'running'
            FOR UPDATE
            """,
            (run_id, dataset),
        )
        if cursor.fetchone() is None:
            raise ValueError("La carga requiere una ejecución activa para el dataset.")
        cursor.executemany(_INSERT_RAW, _raw_params(raw_records, dataset, run_id))
        raw_inserted = cursor.rowcount
        cursor.executemany(
            _UPSERT_MUNICIPALITY,
            (
                (dataset, item["cvegeo"], item["name"], Jsonb(item["geometry"]), run_id)
                for item in municipalities
            ),
        )
        municipalities_upserted = cursor.rowcount
        cursor.executemany(
            _UPSERT_INDICATOR,
            (
                (
                    dataset,
                    item["cvegeo"],
                    item["periodo"],
                    item["indicador"],
                    item["valor"],
                    item["unidad"],
                    item["fuente"],
                    run_id,
                )
                for item in indicators
            ),
        )
        indicators_upserted = cursor.rowcount

        if indicators_upserted:
            cursor.execute(_POPULATION_CONFLICT, (dataset, run_id))
            conflict = cursor.fetchone()
            if conflict is not None:
                raise ConsistencyError(conflict["cvegeo"], conflict["periodo"])

    return {
        "raw_inserted": raw_inserted,
        "municipalities_upserted": municipalities_upserted,
        "indicators_upserted": indicators_upserted,
    }


def export_rows(conn: Connection, dataset: str) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT cvegeo, periodo, indicador, valor, unidad, fuente
        FROM analytics.municipal_indicators
        WHERE dataset = %s
        ORDER BY cvegeo COLLATE "C", periodo, indicador COLLATE "C"
        """,
        (dataset,),
    ).fetchall()


def finish_run(
    conn: Connection,
    run_id: UUID | str,
    *,
    error: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
) -> None:
    result = conn.execute(
        """
        UPDATE raw.etl_runs SET status = %s, finished_at = clock_timestamp(),
                               report = %s, error = %s
        WHERE run_id = %s AND status = 'running'
        """,
        (
            "failed" if error is not None else "succeeded",
            Jsonb(report) if report is not None else None,
            Jsonb(error) if error is not None else None,
            run_id,
        ),
    )
    if result.rowcount != 1:
        raise ValueError("La ejecución no existe o ya tiene un estado final.")


def health(conn: Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT current_database() AS database,
               current_setting('server_version') AS postgresql,
               (SELECT extversion FROM pg_extension WHERE extname = 'postgis') AS postgis,
               to_regclass('raw.etl_runs') IS NOT NULL
                   AND to_regclass('raw.source_records') IS NOT NULL AS raw_ready,
               to_regclass('staging.municipalities') IS NOT NULL
                   AND to_regclass('staging.indicators') IS NOT NULL AS staging_ready,
               to_regclass('analytics.municipal_indicators') IS NOT NULL
                   AND to_regclass('analytics.indicator_summary') IS NOT NULL
                   AS analytics_ready
        """
    ).fetchone()
    assert row is not None
    checks = {
        "raw": row["raw_ready"],
        "staging": row["staging_ready"],
        "analytics": row["analytics_ready"],
    }
    return {
        "status": "ok" if row["postgis"] and all(checks.values()) else "degraded",
        "database": row["database"],
        "postgresql": row["postgresql"],
        "postgis": row["postgis"],
        "schemas": checks,
    }


def recent_runs(conn: Connection, limit: int = 10) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT run_id::text, dataset, status, started_at, finished_at, report, error
        FROM raw.etl_runs
        ORDER BY started_at DESC, run_id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
