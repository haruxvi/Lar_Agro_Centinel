# Limitaciones conocidas

Cada entrada documenta algo que el sistema deliberadamente no
resuelve, por qué, y qué implicaría resolverlo. Leer antes de tomar
decisiones de diseño que puedan agravar alguno de estos puntos.

---

## KL-001 — Las geometrías de predios y lotes no tienen versionado histórico

**Estado:** abierta
**Introducida en:** Fase 2
**Severidad:** media, creciente con el tiempo

### Qué pasa

`predios.geometry` y `lotes.geometry` se sobrescriben al editarse. El
sistema registra el evento `PREDIO_GEOMETRY_CHANGED` en el audit log
con el área anterior y el bounding box, pero no conserva la geometría
previa.

### Por qué importa

Todo dato asociado a un predio queda referido implícitamente a los
límites que ese predio tenía al momento de generarse: capturas de
drone, análisis NDVI, aplicaciones de productos, movimientos de
ganado.

Si alguien corrige el límite de un predio, los análisis anteriores
pasan a estar referidos a un polígono que ya no existe en la base.
Un mapa de anomalías calculado sobre 45 ha no se puede reinterpretar
correctamente si el predio ahora figura con 52 ha.

Para trazabilidad regulatoria esto es un problema concreto: si el SAG
pide demostrar dónde se aplicó un producto en una fecha dada, la
respuesta debe ser el polígono vigente en esa fecha, no el actual.

### Por qué no se resolvió

La solución correcta depende de casos de uso reales que todavía no
existen:

- ¿Hace falta versionar cada edición, o solo las que superan cierto
  umbral de cambio de área?
- ¿Los análisis históricos deben recalcularse contra la geometría
  nueva, o quedar congelados contra la anterior?
- ¿Cuánto tiempo hay que retener versiones antiguas?

Implementar un esquema de versionado sin esas respuestas lleva
casi con seguridad al esquema equivocado, y migrar un esquema de
versionado mal elegido es más caro que implementarlo tarde.

### Qué implicaría resolverlo

Tabla `predios_geometry_history` con `predio_id`, `geometry`,
`area_m2`, `valid_from`, `valid_to`, `changed_by_user_id`,
`change_reason`. Las entidades con referencia temporal (capturas,
análisis, aplicaciones) consultarían la versión vigente en su
`timestamp` en lugar de la actual.

Es un patrón de tabla temporal estándar. La complejidad no está en la
tabla sino en propagar la consulta temporal a todos los módulos que
hoy asumen una sola geometría.

### Mientras tanto, qué NO hacer

- No permitir edición masiva de geometrías sin registro
- No borrar ni editar los eventos `PREDIO_GEOMETRY_CHANGED` del audit
  log: hoy son el único rastro de que el cambio ocurrió
- No construir features que asuman que la geometría de un predio es
  inmutable, porque no lo es
- No asociar datos históricos a un predio sin guardar también su
  propio `timestamp`: sin eso, ni siquiera un versionado futuro
  podría reconstruir la correspondencia

### Revisar cuando

Aparezca el primer caso real de un predio cuyos límites cambien
teniendo datos históricos asociados, o cuando se prepare la primera
auditoría regulatoria del sistema.

---

## KL-002 — La escritura del audit log es best-effort y puede perderse silenciosamente

**Estado:** abierta
**Introducida en:** Fase 1
**Severidad:** baja hoy, alta en producción regulada

### Qué pasa

`AuditService.record()` no propaga errores. Si el INSERT en
`audit_log` falla —base caída, conexión agotada, constraint
violado—, el servicio registra un CRITICAL en el log de aplicación y
devuelve sin lanzar. La operación que originó el evento continúa y
termina con éxito.

### Por qué importa

Es deliberado y correcto para el caso general: un fallo al auditar un
login exitoso no justifica negarle el acceso al usuario.

Pero hay eventos donde la ausencia de registro es en sí misma el
problema. Si se aprueba una aplicación de fitosanitario, se despacha
producto restringido de bodega, se deniega un acceso o se cambia la
configuración del Modo Operación Responsable, y el registro se
pierde, el sistema queda en un estado donde la acción ocurrió y no
hay rastro. Para trazabilidad SAG y para Ley 21.719, eso es un hueco
real: no se puede demostrar lo que no quedó escrito.

Hoy el único indicio de una pérdida es una línea CRITICAL en el log
de aplicación, que nadie está observando porque todavía no hay
observabilidad configurada.

### Por qué no se resolvió

Requiere clasificar los tipos de evento en dos categorías, y esa
clasificación depende de qué eventos van a existir al final. En Fase
1 solo existen los de seguridad; los de dominio con peso regulatorio
llegan en Fases 5, 6 y 7.

Decidir ahora la clasificación significaría decidirla sobre un
catálogo incompleto.

### Qué implicaría resolverlo

Dos modos de escritura:

- **Best-effort**: el actual. Para eventos informativos —lecturas,
  logins exitosos, cambios de rol activo.
- **Bloqueante**: la escritura del evento ocurre en la misma
  transacción que la operación. Si el audit falla, la transacción
  entera hace rollback y la operación no se concreta.

El modo se declara por tipo de evento, no por llamada, para que no
dependa de que quien escribe el código se acuerde.

Complemento necesario: alerta real sobre los CRITICAL de audit
fallido, para que una pérdida en modo best-effort se entere alguien.

### Mientras tanto, qué NO hacer

- No agregar tipos de evento con peso regulatorio asumiendo que el
  registro está garantizado: hoy no lo está
- No quitar el CRITICAL del log de aplicación: es el único rastro que
  queda de una pérdida
- No hacer que el servicio lance excepciones "para algunos casos" de
  forma ad hoc: la clasificación tiene que ser explícita y declarada,
  no decidida caso por caso en el call site

### Revisar cuando

Fase 7 (audit completo con hash chain). Es el momento natural: ahí se
revisa el catálogo entero de eventos y se implementa la cadena de
integridad, que además da la infraestructura para detectar huecos en
la secuencia.

---

## KL-003 — El audit log no tiene orden total dentro de una transacción

**Estado:** abierta
**Introducida en:** Fase 1 (detectada en Fase 2)
**Severidad:** baja hoy, bloqueante para la Fase 7

### Qué pasa

`audit_log.timestamp` usa `now()`, que en PostgreSQL devuelve la hora
de **inicio de la transacción**, no la del INSERT. Todos los eventos
que una operación escribe en una misma transacción comparten el mismo
timestamp, y el `id` es un UUID aleatorio. No hay ninguna columna que
diga en qué orden ocurrieron.

Ejemplo: al cambiar el límite de un predio con una reparación
confirmada se escriben `PREDIO_UPDATED`, `PREDIO_GEOMETRY_CHANGED` y
`GEOMETRY_REPAIR_ACCEPTED` en la misma transacción, con el mismo
timestamp y sin orden recuperable.

### Por qué importa

Entre transacciones distintas el orden sí se conserva, y dentro de una
misma operación los eventos describen un único acto, así que hoy no se
pierde información de auditoría.

Pero la cadena de hashes de la Fase 7 necesita un orden total y
estable: cada registro encadena el hash del anterior, y "el anterior"
tiene que estar definido sin ambigüedad.

Se detectó porque un test end to end que asumía ese orden pasaba o
fallaba según el azar de los UUID.

### Por qué no se resolvió

Cambiar el esquema de `audit_log` es terreno del ADR-002 y de la Fase
7, que va a redefinir la tabla para la cadena de integridad. Agregar
ahora una secuencia obligaría a decidir su semántica (global o por
predio, con o sin huecos) antes de diseñar la cadena que la usa.

### Qué implicaría resolverlo

Una columna `sequence BIGINT GENERATED ALWAYS AS IDENTITY`, que da
orden total de inserción y sirve de base para `hash_prev`, y
opcionalmente `clock_timestamp()` en lugar de `now()` para registrar
el instante real de cada evento.

### Mientras tanto, qué NO hacer

- No escribir código ni tests que dependan del orden de eventos de una
  misma transacción
- No ordenar el audit log solo por `timestamp` esperando un orden
  estable

### Revisar cuando

Al diseñar la cadena de hashes (Fase 7).

---

## KL-004 — En desarrollo, `lar_owner` es superusuario

**Estado:** abierta
**Introducida en:** Fase 0
**Severidad:** baja en desarrollo, alta si llega a producción

### Qué pasa

La imagen oficial de PostgreSQL convierte en superusuario al usuario
definido en `POSTGRES_USER`, que en `docker-compose.yml` es
`lar_owner`. El dueño del esquema, que corre las migraciones, tiene
entonces todos los privilegios del servidor.

### Por qué importa

- Un superusuario no tiene límites dentro del servidor: puede
  desactivar triggers, incluidos los que hacen append-only al audit
  log (ADR-002).
- Las migraciones pasan en desarrollo aunque necesiten privilegios que
  un dueño normal no tendría. La de la Fase 2 ejecuta
  `CREATE EXTENSION IF NOT EXISTS postgis`, que exige superusuario si
  la extensión no existe todavía. En un entorno sin superusuario, esa
  migración falla a menos que la extensión ya esté creada.
- El rol de runtime (`lar_app`) sí es de mínimo privilegio, así que la
  aplicación en ejecución no hereda el problema.

### Por qué no se resolvió

Corregirlo en desarrollo requiere un usuario administrador aparte para
el contenedor y recrear el volumen de PostgreSQL
(`docker compose down -v`), lo que borra los datos locales. Es una
decisión del equipo, no un cambio para hacer de pasada.

### Qué implicaría resolverlo

- `POSTGRES_USER` pasa a ser un administrador (`postgres`), y
  `init-db.sql` crea `lar_owner` como rol normal con `CREATEDB`.
- La extensión PostGIS se crea como administrador en `init-db.sql` (y
  en la plantilla que usan las bases de test), de modo que el
  `IF NOT EXISTS` de la migración no haga nada.
- En producción, un DBA crea la extensión antes de la primera
  migración.

### Mientras tanto, qué NO hacer

- No reproducir esta configuración en staging ni en producción: allí
  `lar_owner` no debe ser superusuario
- No agregar migraciones que dependan de privilegios de superusuario
  sin documentar el paso manual que requieren en producción

### Revisar cuando

Antes del primer despliegue fuera de desarrollo.

---

## KL-005 — El almacenamiento local de rasters no es apto para producción

**Estado:** abierta
**Introducida en:** Fase 3
**Severidad:** nula en desarrollo, crítica en un despliegue con filesystem efímero

### Qué pasa

Los GeoTIFF y previews de cada análisis se guardan a través de
`StorageBackend` (`backend/app/shared/storage.py`). La única
implementación hoy es `LocalStorageBackend`, que escribe en
`STORAGE_LOCAL_PATH` (por defecto `./data/rasters`).
`S3StorageBackend` existe solo como stub: con `STORAGE_BACKEND=s3` la
aplicación no arranca.

### Por qué importa

En plataformas con filesystem efímero (Railway, Render, Heroku, o
cualquier contenedor sin volumen persistente) el disco se pierde en
cada deploy o reinicio. Los registros de `analyses` sobreviven en la
base, pero los rasters a los que apuntan desaparecen: los análisis
históricos quedan irrecuperables, y recalcularlos cuesta Processing
Units de una cuota finita.

### Por qué no se resolvió

La fase entrega el pipeline de análisis; el destino de almacenamiento
de producción (proveedor, bucket, región, retención) todavía no está
decidido. La abstracción existe desde el primer día justamente para que
esa decisión no obligue a tocar los módulos que leen rasters.

### Qué implicaría resolverlo

Implementar `S3StorageBackend` con la misma interfaz: `put`/`get` sobre
objetos, `get_stream` por rangos, `delete` idempotente y `get_url` como
URL prefirmada de corta duración. Las keys ya son jerárquicas por
predio (`rasters/{predio_id}/...`), así que las lifecycle policies se
definen por prefijo. Cambiar `STORAGE_BACKEND` a `s3` y configurar
bucket y región.

### Mientras tanto, qué NO hacer

- No desplegar con `STORAGE_BACKEND=local` en un entorno efímero que
  vaya a tener datos reales
- No montar `STORAGE_LOCAL_PATH` en un directorio que no esté respaldado
- No leer ni escribir rasters sin pasar por `StorageBackend`: eso es lo
  que hace que la migración sea un cambio de configuración

### Revisar cuando

Antes del primer despliegue con datos reales.

---

## KL-006 — La detección de anomalías asume una distribución unimodal dentro del lote

**Estado:** abierta
**Introducida en:** Fase 3
**Severidad:** media; mitigada por la revisión humana obligatoria

### Qué pasa

`backend/app/modules/analysis/anomaly.py` marca como anómalos los
píxeles cuyo z-score dentro de su lote cae bajo
`ANOMALY_ZSCORE_THRESHOLD`. El z-score supone que el NDVI del lote se
distribuye de forma aproximadamente normal y con una sola moda.

Dos consecuencias:

1. **Lotes bimodales.** Un lote con dos zonas claramente distintas
   (una parte joven y otra madura, dos variedades, un sector replantado)
   tiene un NDVI bimodal. El método marca la zona baja entera como
   anómala. No es un error de cálculo: es el método aplicado fuera de
   su supuesto.
2. **La anomalía infla la dispersión contra la que se mide.** Una zona
   que ocupa una fracción `p` del lote tiene un z-score medio acotado
   por |z| ≤ √((1−p)/p), sean cuales sean los valores de NDVI. Para
   llegar a z ≤ −3 hace falta p ≤ 10 %, y para el umbral de detección
   (−2), p ≤ 20 %. Una zona dañada que cubre un cuarto del lote **no
   se detecta**: con este método, lo que falla en grande se vuelve "lo
   normal" del lote. Con los cortes por defecto, además, la condición
   de severidad "z ≤ −3 y al menos 10 % del lote" solo se cumple en el
   caso límite exacto, así que HIGH llega en la práctica por el área
   absoluta.

### Por qué importa

Un agrónomo que ve una anomalía grande en un lote bimodal puede
interpretarla como estrés cuando es la estructura del lote. Y al revés,
un daño extendido (una helada, una plaga generalizada en el lote) puede
no aparecer porque arrastra la media y la desviación del propio lote.

### Por qué no se resolvió

La alternativa correcta (comparar cada lote con su propia historia, o
modelar mezclas de distribuciones) necesita escenas acumuladas y casos
revisados que todavía no existen. El módulo ya está estructurado para
sumar un método temporal (`AnomalyMethod`) sin reescribir el espacial.

### Qué implicaría resolverlo

- Método temporal: z-score del píxel contra el historial del mismo
  lote en la misma época, cuando haya escenas suficientes.
- Estimación robusta de la dispersión (mediana y MAD en lugar de media
  y desviación), que es mucho menos sensible a la propia zona anómala.
- Detección de bimodalidad por lote antes de aplicar el z-score.
- Recalibrar los cortes de severidad con anomalías revisadas
  (`review_status`), como prevé el ADR-004.

### Mientras tanto, qué NO hacer

- No presentar una anomalía como diagnóstico: es una hipótesis hasta
  que alguien la revisa
- No ocultar ni automatizar decisiones de campo sobre la base de la
  severidad sin revisión
- No interpretar "sin anomalías" como "lote sano": un daño extendido
  puede no detectarse

### Revisar cuando

Haya al menos unas centenas de anomalías revisadas, o escenas
suficientes por lote para el método temporal.

---

## KL-007 — Los análisis reemplazados por un recómputo forzado se acumulan, y sus revisiones quedan dispersas

**Estado:** abierta
**Introducida en:** Fase 3
**Severidad:** baja hoy; crece con el uso de `force`

### Qué pasa

Los análisis superseded se conservan íntegros, con su raster en storage
y sus anomalías revisadas. No hay política de retención: un predio que
se force-recomputa muchas veces acumula un raster por corrida. Las
revisiones de anomalías no se transfieren entre corridas, por decisión
de diseño, así que el conocimiento de campo queda disperso en la cadena
de análisis superseded en vez de estar en el vigente.

En el modelo: un análisis forzado que termina COMPLETED sobre una escena
que ya tenía resultado marca al anterior con
`superseded_by_analysis_id`. El índice
`uq_analyses_completed_scene` solo exige unicidad entre los vigentes
(`superseded_by_analysis_id IS NULL`), así que ambos conviven. El
historial por lote (`/lotes/{id}/history`) usa solo los vigentes; el
anterior sigue accesible por su id, con su raster y sus anomalías.

### Por qué importa

- Storage crece sin límite con cada recómputo forzado (un GeoTIFF, un
  PNG y un JSON por corrida).
- Quien abre el análisis vigente ve anomalías sin revisar aunque la
  misma zona ya haya sido confirmada o descartada en una corrida
  anterior. Para encontrar ese juicio hay que recorrer la cadena
  `superseded_by_analysis_id` hacia atrás.

### Por qué no se resolvió

Transferir revisiones exige decidir cuándo dos anomalías de corridas
distintas son "la misma" (solapamiento mínimo, tolerancia de forma), y
una revisión heredada por error es peor que una revisión ausente. El
POST con `force` avisa cuántas anomalías revisadas tiene el análisis que
probablemente se reemplace, y el evento `ANALYSIS_FORCED_RECOMPUTE`
(WARNING) registra el id reemplazado y el conteo exacto.

### Qué implicaría resolverlo

- Una política de retención de rasters de análisis superseded (por
  antigüedad o por cantidad por escena), con borrado auditado.
- Opcionalmente, mostrar en el análisis vigente las revisiones de la
  cadena anterior como referencia, sin copiarlas.

### Mientras tanto, qué NO hacer

- No borrar análisis superseded ni sus rasters a mano: las revisiones
  y su auditoría apuntan a ellos.
- No copiar revisiones al análisis nuevo en una migración o un script.

### Revisar cuando

- Exista un caso real de recómputo frecuente, o
- se defina la retención de rasters (KL-005 cubre el storage local).
