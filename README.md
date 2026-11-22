# GeoDataOps ETL

[Versión en español](README.es.md)

GeoDataOps ETL loads municipal indicators and boundaries into PostgreSQL/PostGIS using Python and SQL. In a fresh database, the bundled sample publishes 18 census observations across Guadalajara, San Pedro Tlaquepaque, and Zapopan for 2010 and 2020. The sample runs from versioned inputs and needs no connection to INEGI. [Data provenance and geographic coverage](data/jalisco/README.md).

## Run with Docker Compose

Use Docker Engine with Compose v2 and Linux amd64 containers. Run the commands from the repository root.

```sh
docker compose up -d --wait db
docker compose build etl
docker compose run --rm etl run
docker compose run --rm etl health
docker compose run --rm etl diagnose --limit 5
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U geodataops -d geodataops < src/geodataops/queries.sql
```

In PowerShell, pipe the SQL file into `psql`.

```powershell
Get-Content -Raw src/geodataops/queries.sql | docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U geodataops -d geodataops
```

The database is reachable at `db:5432` within the Compose network. The `pgdata` volume preserves data after `docker compose down`. The default password is for local demonstrations. Set `POSTGRES_PASSWORD` before the first database initialization to choose a different password.

## Run with an existing PostgreSQL server

Use Python 3.12 or newer and a dedicated PostgreSQL database with PostGIS available. The database account needs permission to create the application schemas and, when the extension is absent, to enable PostGIS.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

In PowerShell, activate the environment with `.\.venv\Scripts\Activate.ps1`. Set `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and libpq authentication for your database. Run from the repository root so the CLI can find the bundled data.

```sh
geodataops run
geodataops health
geodataops diagnose --limit 5
```

The first run initializes the schemas. Run `geodataops init` to initialize them separately.

## Architecture

| Step | Behavior |
| --- | --- |
| Extract and validate | Read a normalized CSV or the Jalisco ITER 2020 CSV/ZIP, plus a local GeoJSON file or an HTTPS GeoJSON endpoint. Validate identifiers, duplicate keys, numeric values, and geometry structure. |
| `raw` | Record each run and deduplicate source records by dataset, source, and SHA-256. |
| `staging` | Store municipal `MultiPolygon` geometry in EPSG:4326 with a GiST index. Enforce municipal foreign keys and population consistency on upsert. |
| `analytics` | Expose municipal indicators and aggregates by year, indicator, unit, and source. The example SQL also computes geodetic areas and lists recent failures. |
| Publish | Commit database changes in a transaction and serialize runs with a session lock. Write the ordered CSV to a temporary file in its destination directory and replace the published file atomically. |

A new batch inserts missing keys and updates changed values. Omitted keys remain available. Repeating the same batch leaves the stored data and CSV content unchanged.

## Example output

A clean run uses the versioned census sample and produces the CSV at `output/municipios_indicadores.csv`. [Published sample](output/municipios_indicadores.csv).

```csv
cvegeo,periodo,indicador,valor,unidad,fuente
14039,2010,poblacion_femenina,777785,personas,IIEG con datos de INEGI (Censo de Población y Vivienda 2010)
14039,2010,poblacion_masculina,717404,personas,IIEG con datos de INEGI (Censo de Población y Vivienda 2010)
14039,2010,poblacion_total,1495189,personas,IIEG con datos de INEGI (Censo de Población y Vivienda 2010)
```

The `health` output below was captured with a local PostgreSQL/PostGIS instance after loading the sample.

```json
{"status": "ok", "database": "geodataops", "postgresql": "17.12", "postgis": "3.7.0", "schemas": {"raw": true, "staging": true, "analytics": true}}
```

The CSV uses UTF-8 without a BOM, a comma delimiter, and a decimal point. All six fields are required. Read `cvegeo` as a string of five ASCII digits and `periodo` as a four-digit reference year. The key is `(cvegeo, periodo, indicador)`. `valor` is a finite decimal measured in `unidad`, and `fuente` identifies its origin. The shared v1 contract is at `../geodataops-sync/contrato_datos.md` in the coordinated workspace.

## Data sources

The bundled observations come from an IIEG table based on the INEGI 2010 and 2020 censuses. The three boundaries come from the INEGI municipal geostatistical service. The same boundary snapshot is used for both census years. [Source links, attribution, and transformations](data/jalisco/README.md).

For a statewide load, save the official ITER archive as `output/iter_jalisco_2020.zip`. Use a dedicated database to keep statewide results separate from the sample. The live boundary service is optional. To reproduce a fixed boundary version, pass `--geojson` with a local snapshot in place of `--municipios-url`. [INEGI ITER Jalisco 2020 archive](https://www.inegi.org.mx/contenidos/programas/ccpv/2020/datosabiertos/iter/iter_14_cpv2020_csv.zip) · [INEGI geostatistical service](https://www.inegi.org.mx/servicios/catalogounico.html).

```sh
docker compose run --rm etl run --csv output/iter_jalisco_2020.zip \
  --municipios-url https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14 \
  --output output/municipios_indicadores_jalisco.csv
```

## Tests and CI

Run unit tests after installing the `test` extra.

```sh
python -m pytest -q -m 'not integration'
```

Run the full suite with Docker Compose and the test profile.

```sh
docker compose --profile test build tests
docker compose --profile test run --rm tests
```

For local integration tests, set `GEODATAOPS_TEST_DSN` to a PostgreSQL test instance with PostGIS available and permission to create temporary databases and enable the extension. Set `GEODATAOPS_REQUIRE_INTEGRATION=1` to require a working database when running `python -m pytest -q -m integration`.

GitHub Actions defines separate jobs for unit tests, required PostGIS integration tests, and a Compose smoke test. The Compose job checks the 18 observations, health, CSV identity, and an idempotent replay using bundled data. [Workflow definition](.github/workflows/ci.yml).

## L2 support

The CLI logs structured JSON events to stderr. Match the run ID from the logs to a recent execution in `diagnose` to inspect its failure phase, SQLSTATE, and constraint name. The runbook walks through a foreign-key failure, verifies that the transaction rolls back and the prior CSV stays intact, then replays the corrected batch. [L2 runbook in Spanish](docs/soporte-l2.md).

## Scope

The default sample covers three municipalities and two census years. The ETL reads and validates small batches in memory, with a 64 MiB limit per CSV or GeoJSON payload. The CSV limit applies to the decompressed member of an ITER ZIP. The Compose database, volume, and credentials are intended for local development.

The separate `demo` dataset uses synthetic values for tests and incident reproduction. `geodataops run --dataset demo` publishes `output/municipios_indicadores_demo.csv` and labels its source `DEMO sintético`.
