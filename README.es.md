# GeoDataOps ETL

[English version](README.md)

Pipeline reproducible de indicadores municipales con Python, PostgreSQL/PostGIS y SQL. La ejecución predeterminada publica 18 observaciones censales de Guadalajara, San Pedro Tlaquepaque y Zapopan para 2010 y 2020. Incluye los archivos de origen necesarios para repetir esa ejecución sin consultar servicios externos. [Procedencia y cobertura](data/jalisco/README.md).

## Ejecución con Docker Compose

Desde `geodataops-etl/`, con Docker y Compose para contenedores Linux `amd64`:

```sh
docker compose up -d --wait db
docker compose build etl
docker compose run --rm etl run
docker compose run --rm etl health
docker compose run --rm etl diagnose --limit 5
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U geodataops -d geodataops < src/geodataops/queries.sql
```

En PowerShell, sustituye el último comando por `Get-Content -Raw src/geodataops/queries.sql | docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U geodataops -d geodataops`. El volumen `pgdata` conserva los datos tras `docker compose down`. El servicio de base de datos se mantiene dentro de la red de Compose. La contraseña predeterminada está destinada a la demostración local. `POSTGRES_PASSWORD` configura la contraseña en la primera inicialización del volumen.

Con Python 3.12 y una instancia existente de PostgreSQL con PostGIS disponible, ejecuta `python -m pip install -e '.[test]'` dentro de un entorno virtual. Configura `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` y la autenticación libpq para una base dedicada, y ejecuta `geodataops run` desde este directorio. En la primera ejecución la cuenta necesita permisos para activar PostGIS y crear los esquemas. `geodataops init` también permite inicializarlos por adelantado.

## Flujo y garantías

| Capa | Comportamiento |
|---|---|
| Entrada | CSV normalizado o CSV/ZIP del ITER Jalisco 2020. GeoJSON municipal local o API HTTPS. Se validan formato, claves, duplicados, decimales finitos, conteos y geometrías. |
| `raw` | Lotes auditados por UUID. Registros de origen con deduplicación por conjunto, fuente y SHA-256 de su representación canónica. |
| `staging` | Municipios con geometría PostGIS `MultiPolygon` en EPSG:4326 e índice GiST. Indicadores con llave foránea municipal, controles SQL y verificación de ternas de población al consolidar cambios. |
| `analytics` | Vistas de indicadores por municipio y agregaciones por año, indicador, unidad y fuente. `queries.sql` muestra cobertura, población cargada y áreas geodésicas. |
| Publicación | Transacción para datos crudos y normalizados. Bloqueo asesor de sesión entre la carga y la publicación. CSV ordenado, escrito en un temporal del mismo directorio y reemplazado en una operación. |

Las ejecuciones idénticas preservan el contenido y no vuelven a escribir filas de datos. Un lote posterior añade claves o corrige valores en la misma clave. Las filas ausentes se conservan. Demo y Jalisco tienen claves independientes en SQL. `geodataops run --dataset demo` publica `output/municipios_indicadores_demo.csv`. Los logs JSON se escriben en stderr con UUID, fase y contadores. `diagnose` consulta estados y diagnósticos persistidos.

### Fuentes de todo Jalisco

Guarda el [ZIP oficial del ITER Jalisco 2020](https://www.inegi.org.mx/contenidos/programas/ccpv/2020/datosabiertos/iter/iter_14_cpv2020_csv.zip) en `output/iter_jalisco_2020.zip`. Usa una base dedicada si quieres conservar por separado la muestra de tres municipios. El adaptador filtra los totales municipales e importa tres indicadores por municipio. La opción `--municipios-url` consulta el [servicio vectorial de INEGI](https://www.inegi.org.mx/servicios/catalogounico.html) en vivo. Usa `--geojson` con una captura local para fijar la versión de las geometrías.

```sh
docker compose run --rm etl run --csv output/iter_jalisco_2020.zip \
  --municipios-url https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14 \
  --output output/municipios_indicadores_jalisco.csv
```

## Integración con ML

`output/municipios_indicadores.csv` se entrega con la exportación real generada. Tiene cabecera, formato UTF-8, coma como delimitador y punto decimal. Los seis campos son obligatorios:

| Campo | Tipo | Significado |
|---|---|---|
| `cvegeo` | Cadena de cinco dígitos ASCII | Clave municipal INEGI. Preservar ceros a la izquierda. |
| `periodo` | Año `YYYY` | Año de referencia. |
| `indicador` | Identificador de texto | Clave de la medida. |
| `valor` | Decimal finito | Magnitud en la unidad indicada. |
| `unidad` | Texto | Unidad de medida. |
| `fuente` | Texto | Procedencia. `DEMO sintético` identifica datos de prueba. |

La clave es `(cvegeo, periodo, indicador)`. El contrato compartido vive en `../geodataops-sync/contrato_datos.md`.

## Pruebas y soporte

```sh
docker compose --profile test build tests
docker compose --profile test run --rm tests
python -m pytest -q -m 'not integration'
```

La última línea ejecuta las pruebas unitarias en un entorno con `.[test]`. Para todas las pruebas fuera de Docker, usa `GEODATAOPS_TEST_DSN` con el DSN de una instancia de pruebas que disponga de PostGIS y permita crear bases temporales e instalar la extensión. Las pruebas de integración crean y eliminan bases con nombres aleatorios. [Guía L2 y reproducción de una falla](docs/soporte-l2.md).

### Alcance

La muestra predeterminada cubre tres municipios y dos cortes censales. Los dos años se relacionan con las geometrías capturadas en una misma fecha. El MVP procesa lotes pequeños, limita el tamaño por archivo y valida en memoria. El volumen, la serialización y las credenciales de Compose están dimensionados para desarrollo local.

El proyecto demuestra diseño de esquemas SQL y espaciales, normalización reproducible, transacciones y cargas incrementales, pruebas de integración, observabilidad y diagnóstico L2.
