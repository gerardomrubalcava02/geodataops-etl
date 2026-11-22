-- Coverage and distributions by dataset, year, indicator, unit, and source.
SELECT dataset, periodo, indicador, unidad, fuente, municipality_count,
       minimum_value, maximum_value, round(average_value, 2) AS average_value
FROM analytics.indicator_summary
ORDER BY dataset, periodo, indicador, unidad, fuente;

-- Population is additive across municipalities. When coverage is partial, the
-- result is the total for the loaded municipalities.
SELECT dataset, periodo, unidad, fuente, count(*) AS municipality_count,
       sum(valor) AS loaded_population
FROM analytics.municipal_indicators
WHERE indicador = 'poblacion_total'
GROUP BY dataset, periodo, unidad, fuente
ORDER BY dataset, periodo, unidad, fuente;

-- Calculate geodetic areas in square kilometers from the stored boundaries.
SELECT dataset, cvegeo, name,
       round((public.ST_Area(geometry::public.geography) / 1000000)::numeric, 2)
           AS area_km2
FROM staging.municipalities
ORDER BY dataset, cvegeo;

-- An L2 operator can filter failed runs directly in SQL.
SELECT run_id, dataset, started_at, finished_at,
       error ->> 'phase' AS phase,
       error ->> 'sqlstate' AS sqlstate,
       error ->> 'constraint' AS constraint_name,
       error ->> 'message' AS message,
       error ->> 'hint' AS hint
FROM raw.etl_runs
WHERE status = 'failed'
ORDER BY started_at DESC
LIMIT 10;
