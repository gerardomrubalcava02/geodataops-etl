# Soporte L2 y recuperación

`docker compose ps db` indica si PostgreSQL acepta conexiones. `docker compose run --rm etl health` verifica además la extensión PostGIS y las tablas y vistas requeridas. Devuelve código 1 si detecta un fallo. Consulta `docker compose logs --tail=50 db` si falla la conectividad.

`docker compose run --rm etl diagnose` devuelve las ejecuciones más recientes con UUID, conjunto, estado, fase de error, SQLSTATE, restricción y reporte, según lo que esté disponible. Correlaciona el UUID con los eventos JSON en stderr. `src/geodataops/queries.sql` incluye una consulta SQL al historial de fallas.

| Evidencia | Acción |
|---|---|
| `23503`, `indicators_municipality_fk` | Verifica el campo `cvegeo` y la cobertura municipal del conjunto correcto. |
| `23514` | Revisa la restricción indicada y el valor o geometría de origen. |
| `ConsistencyError` | Verifica los componentes de población y corrige las observaciones relacionadas del municipio y año en el mismo lote. |
| `db_committed: true`, `csv_published: false` | Corrige la ruta, permisos o bloqueo del archivo de salida y repite el lote. |
| `db_committed: null`, `interrupted_run` o desconexión al confirmar | Verifica conectividad y repite el mismo lote. El ETL registra ejecuciones interrumpidas al tomar el bloqueo para la ejecución siguiente. |

## Caso reproducible en PowerShell

Con el `db` saludable y la imagen `etl` construida, usa datos sintéticos. La ruta del lote queda dentro del directorio montado `output/`.

```powershell
docker compose run --rm etl run --dataset demo
$before = (Get-FileHash output/municipios_indicadores_demo.csv).Hash
$header = 'cvegeo,periodo,indicador,valor,unidad,fuente'
$valid = '14039,2035,poblacion_total,123,personas,DEMO sintético (GeoDataOps)'
$bad = '14999,2035,poblacion_total,456,personas,DEMO sintético (GeoDataOps)'
@($header, $valid, $bad) | Set-Content -Encoding utf8 output/lote_l2.csv
docker compose run --rm etl run --dataset demo --csv output/lote_l2.csv
$LASTEXITCODE
docker compose run --rm etl diagnose --limit 1
$before -eq (Get-FileHash output/municipios_indicadores_demo.csv).Hash
```

El lote falla con código 1 y SQLSTATE `23503`. La clave `14999` cumple el formato, pero no existe en `staging.municipalities` para `demo`. PostgreSQL revierte todo el lote, incluida la fila válida procesada en la misma transacción. El historial conserva el fallo y el hash del CSV debe coincidir.

```powershell
$fixed = $bad.Replace('14999', '14098')
@($header, $valid, $fixed) | Set-Content -Encoding utf8 output/lote_l2.csv
docker compose run --rm etl run --dataset demo --csv output/lote_l2.csv
Import-Csv output/municipios_indicadores_demo.csv | Where-Object periodo -eq '2035'
docker compose run --rm etl run --dataset demo --csv output/lote_l2.csv
docker compose run --rm etl diagnose --limit 3
```

La exportación debe incluir las dos claves corregidas para 2035. En una base recién inicializada, la primera recuperación añade dos indicadores. La última ejecución informa `indicators_upserted: 0`. `test_sql_failure_rollback_and_corrected_replay` verifica automáticamente el código SQL, el rollback, el CSV previo y la recuperación. Otro caso fuerza una falla de escritura después del commit y comprueba la publicación durante una reejecución idempotente.
