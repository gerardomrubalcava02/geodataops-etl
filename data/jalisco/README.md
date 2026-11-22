# Muestra territorial de Jalisco

La muestra cubre Guadalajara (`14039`), San Pedro Tlaquepaque (`14098`) y Zapopan (`14120`). Los archivos versionados permiten repetir la carga sin conectarse a las fuentes.

## Población

`poblacion.csv` contiene 18 observaciones transcritas del cuadro «Municipios del Área Metropolitana de Guadalajara, Jalisco, 2010-2020». El IIEG elaboró el cuadro con datos de los Censos de Población y Vivienda 2010 y 2020 del INEGI. Se antepuso la clave estatal `14` a la clave municipal de tres dígitos, se omitieron los separadores de miles y se dispusieron las cifras de población total, femenina y masculina en filas por municipio y año. [IIEG, Principales resultados del Censo de Población y Vivienda 2020 en Jalisco, p. 16](https://www.iieg.gob.mx/ns/wp-content/uploads/2021/10/ResultadosCenso2020Jal.pdf#page=16).

El periodo identifica el año del censo. La cobertura de dos años permite comparar observaciones censales de estos tres municipios.

## Geometrías

`municipios.geojson` contiene tres áreas geoestadísticas municipales extraídas el 21 de noviembre de 2026 del servicio vectorial del Catálogo Único de Claves Geoestadísticas del INEGI. Se consultó cada clave, se combinaron las entidades en una colección GeoJSON y se conservaron las propiedades `cvegeo` y `nomgeo` junto con las geometrías `MultiPolygon` originales. Las coordenadas están en longitud y latitud. [INEGI, Servicio Web del Catálogo Único de Claves Geoestadísticas](https://www.inegi.org.mx/servicios/catalogounico.html).

| Clave | Vértices, incluidos los de cierre | SHA-256 de la respuesta original | Consulta |
| --- | ---: | --- | --- |
| `14039` | 1818 | `467fd8442ac0650908433c46bf14b3be6b65555ad06847923297b126b5918d78` | [Guadalajara](https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14039) |
| `14098` | 2886 | `2462f585f07cb9a88164d285075c27fe529f475b97ade3fb94a09b9f65968b8b` | [San Pedro Tlaquepaque](https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14098) |
| `14120` | 3299 | `b7b9207074bc0dbe807a43eceb6f02dafb6a8c1ede27484c05fec073e0d6fabe` | [Zapopan](https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14120) |

Las geometrías sirven como referencia estadística de la fecha de extracción. La carga las relaciona con las observaciones censales por clave municipal y utiliza la misma referencia espacial para los dos periodos. El servicio activo puede entregar una versión posterior. [INEGI, términos de libre uso y atribución](https://www.inegi.org.mx/inegi/terminos.html).

### Sistema de referencia

Las tres geometrías versionadas y las 125 de la consulta estatal del 22 de noviembre de 2026 declaran `EPSG:6365` (Mexico ITRF2008, longitud y latitud) en `geometry.crs`. Las geometrías coinciden exactamente para los tres municipios de la muestra. [INEGI, geometrías de Jalisco](https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgem/14) · [PROJ, catálogo de sistemas geográficos](https://crs-explorer.proj.org/?activeTypes=GEOGRAPHIC_2D_CRS).

El ETL conserva el CRS declarado y aplica `ST_Transform` a `EPSG:4326` antes de guardar en PostGIS. Acepta `EPSG:4326` y `EPSG:6365` mediante un CRS nombrado dentro de cada geometría. Una geometría sin declaración usa WGS 84 en orden longitud/latitud. Los CRS fuera de ese alcance se rechazan durante la validación. [RFC 7946, sistema de referencia](https://www.rfc-editor.org/rfc/rfc7946.html#section-4) · [PostGIS, ST_Transform](https://postgis.net/docs/ST_Transform.html).

Con PostGIS 3.7.0 y PROJ 9.8.1, `ST_GeomFromGeoJSON` leyó el SRID original `6365`. Comparé el EWKB de `ST_Transform(geom, 4326)` con el de `ST_SetSRID(geom, 4326)`. Fueron idénticos en los 8,003 vértices de la muestra y los 319,027 de la consulta estatal, incluidos los de cierre. La diferencia observada fue de 0 m en posición y 0 m² en área geodésica. La operación EPSG:6373 que registra PROJ para convertir de Mexico ITRF2008 a WGS 84 tiene una precisión declarada de 1 m. [PROJ 9.8.1, operaciones Helmert](https://github.com/OSGeo/PROJ/blob/9.8.1/data/sql/helmert_transformation.sql).
