CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS raw.etl_runs (
    run_id uuid PRIMARY KEY,
    dataset text NOT NULL CHECK (btrim(dataset) <> ''),
    status text NOT NULL DEFAULT 'running',
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    report jsonb,
    error jsonb,
    UNIQUE (dataset, run_id),
    CONSTRAINT etl_runs_state_valid CHECK (
        (status = 'running' AND finished_at IS NULL AND error IS NULL)
        OR (status = 'succeeded' AND finished_at IS NOT NULL AND error IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL AND error IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS etl_runs_started_idx ON raw.etl_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS raw.source_records (
    dataset text NOT NULL,
    source text NOT NULL CHECK (btrim(source) <> ''),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    payload jsonb NOT NULL,
    first_run_id uuid NOT NULL,
    PRIMARY KEY (dataset, source, content_sha256),
    FOREIGN KEY (dataset, first_run_id) REFERENCES raw.etl_runs (dataset, run_id)
);

CREATE TABLE IF NOT EXISTS staging.municipalities (
    dataset text NOT NULL,
    cvegeo text NOT NULL,
    name text NOT NULL CHECK (btrim(name) <> ''),
    geometry public.geometry(MultiPolygon, 4326) NOT NULL,
    last_run_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (dataset, cvegeo),
    FOREIGN KEY (dataset, last_run_id) REFERENCES raw.etl_runs (dataset, run_id),
    CONSTRAINT municipalities_cvegeo_valid CHECK (
        cvegeo COLLATE "C" ~ '^(0[1-9]|[12][0-9]|3[0-2])[0-9]{3}$'
        AND right(cvegeo, 3) <> '000'
    ),
    CONSTRAINT municipalities_geometry_valid CHECK (
        NOT public.ST_IsEmpty(geometry)
        AND public.ST_IsValid(geometry)
        AND public.ST_XMin(public.Box2D(geometry)) >= -180
        AND public.ST_XMax(public.Box2D(geometry)) <= 180
        AND public.ST_YMin(public.Box2D(geometry)) >= -90
        AND public.ST_YMax(public.Box2D(geometry)) <= 90
    )
);

CREATE INDEX IF NOT EXISTS municipalities_geometry_idx
    ON staging.municipalities USING gist (geometry);

CREATE TABLE IF NOT EXISTS staging.indicators (
    dataset text NOT NULL,
    cvegeo text NOT NULL,
    periodo smallint NOT NULL,
    indicador text NOT NULL,
    valor numeric NOT NULL,
    unidad text NOT NULL CHECK (btrim(unidad) <> ''),
    fuente text NOT NULL CHECK (btrim(fuente) <> ''),
    last_run_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (dataset, cvegeo, periodo, indicador),
    CONSTRAINT indicators_municipality_fk FOREIGN KEY (dataset, cvegeo)
        REFERENCES staging.municipalities (dataset, cvegeo),
    FOREIGN KEY (dataset, last_run_id) REFERENCES raw.etl_runs (dataset, run_id),
    CONSTRAINT indicators_periodo_valid CHECK (periodo BETWEEN 1000 AND 9999),
    CONSTRAINT indicators_identifier_valid CHECK (
        indicador COLLATE "C" ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
    ),
    CONSTRAINT indicators_valor_finite CHECK (valor NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT indicators_population_value_valid CHECK (
        indicador NOT IN ('poblacion_total', 'poblacion_femenina', 'poblacion_masculina')
        OR (unidad = 'personas' AND valor >= 0 AND trunc(valor) = valor)
    )
);

CREATE INDEX IF NOT EXISTS indicators_last_run_idx
    ON staging.indicators (dataset, last_run_id);

CREATE OR REPLACE VIEW analytics.municipal_indicators AS
SELECT i.dataset, i.cvegeo, i.periodo, i.indicador, i.valor, i.unidad, i.fuente,
       m.name AS municipio, m.geometry
FROM staging.indicators AS i
JOIN staging.municipalities AS m USING (dataset, cvegeo);

CREATE OR REPLACE VIEW analytics.indicator_summary AS
SELECT dataset, periodo, indicador, unidad, fuente,
       count(*) AS municipality_count,
       min(valor) AS minimum_value,
       max(valor) AS maximum_value,
       avg(valor) AS average_value
FROM staging.indicators
GROUP BY dataset, periodo, indicador, unidad, fuente;
