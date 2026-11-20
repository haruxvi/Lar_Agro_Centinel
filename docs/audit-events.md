# Catálogo de eventos de auditoría

Qué eventos escribe el sistema en `audit_log`, con qué categoría y
severidad, y qué lleva `details`. La fuente de verdad es
`backend/app/modules/audit/events.py`: la categoría y la severidad se
declaran **una vez por tipo de evento**, no en cada llamada. Un test
verifica que esta tabla y el catálogo del código coinciden.

Los eventos de seguridad de la Fase 1 (login, 2FA, tokens, roles) están
en el mismo archivo y se revisarán completos en la Fase 7 (ver KL-002).

## Reglas para `details`

- **Nunca una geometría completa.** Ni GeoJSON ni WKT. El servicio
  rechaza (`ValueError`, antes del commit) cualquier `details` que
  contenga una clave `coordinates`, `geometries`, `geometry` o
  `features`, a cualquier profundidad.
- En lugar de la geometría se registra su resumen: `area_m2`, `bbox`
  (`[min_lon, min_lat, max_lon, max_lat]`), `vertex_count` y
  `geometry_type`.
- Sin datos sensibles: `AuditService` además redacta claves como
  `password` o `token`.

## Predios y lotes

| Evento | Categoría | Severidad | Cuándo | `details` |
|---|---|---|---|---|
| `PREDIO_CREATED` | DOMAIN | INFO | Se crea un predio | `name`, `slug`, resumen de geometría, `warnings` (códigos) |
| `PREDIO_UPDATED` | DOMAIN | INFO | Cambian atributos no geométricos | `fields`: nombres de los campos que cambiaron (no los valores) |
| `PREDIO_GEOMETRY_CHANGED` | DOMAIN | WARNING | Se reemplaza el límite de un predio | `previous` y `new` (resúmenes), `area_delta_m2`, `warnings` |
| `PREDIO_DELETED` | DOMAIN | WARNING | Soft delete de un predio | `name`, `slug` |
| `PREDIO_USER_ASSIGNED` | SECURITY | WARNING | Se asigna (o renueva) un rol a un usuario en un predio | `user_id`, `role`, `expires_at`, `renewed`. Nunca el email |
| `PREDIO_USER_UNASSIGNED` | SECURITY | WARNING | Se quita un rol a un usuario en un predio | `user_id`, `role`, `self_removal` |
| `LOTE_CREATED` | DOMAIN | INFO | Se crea un lote | `name`, `lote_type`, resumen de geometría |
| `LOTE_UPDATED` | DOMAIN | INFO | Cambian atributos no geométricos del lote | `fields` |
| `LOTE_GEOMETRY_CHANGED` | DOMAIN | WARNING | Se reemplaza el límite de un lote | `previous`, `new`, `area_delta_m2` |
| `LOTE_DELETED` | DOMAIN | WARNING | Soft delete de un lote | `name`, `area_m2` |
| `LOTES_IMPORTED` | DOMAIN | INFO | Importación GeoJSON exitosa (todo o nada) | `count`, `total_area_m2`, `lote_ids` |
| `GEOMETRY_REPAIRED` | DOMAIN | INFO | Se guarda una reparación automática dentro del umbral | cifras de reparación (abajo), `bbox`, `vertex_count` |
| `GEOMETRY_REPAIR_ACCEPTED` | DOMAIN | WARNING | Se guarda con `accept_repair: true` una reparación fuera del umbral o no medible | cifras de reparación, `bbox`, `vertex_count` |
| `GEOMETRY_REPAIR_REJECTED` | DOMAIN | INFO | Se devuelve 422 pidiendo confirmar una reparación | cifras de reparación, `bbox`, `vertex_count` |

Las cifras de reparación son `area_before_m2`, `area_after_m2`,
`area_change_m2`, `area_change_ratio` (null si no es medible),
`threshold_ratio`, `ignore_below_m2` y `reason`
(`WITHIN_THRESHOLD`, `AREA_CHANGE_ABOVE_THRESHOLD`,
`NOT_MEASURABLE_ZERO_AREA` o `NOT_MEASURABLE_SELF_INTERSECTION`).
Nunca la geometría, ni la original ni la reparada.

`GEOMETRY_REPAIR_ACCEPTED` es un evento aparte a propósito: alguien
decidió guardar una geometría que el sistema consideró sospechosa, y
eso tiene que poder buscarse sin mezclarse con las reparaciones
triviales. `GEOMETRY_REPAIR_REJECTED` se registra aunque la operación
falle: muchos seguidos del mismo usuario indican un umbral mal
calibrado. Un rechazo al crear un predio no tiene `predio_id`, porque
el predio todavía no existe.

Todos llevan `predio_id`, `actor_user_id`, `actor_role`, `actor_ip`,
`actor_user_agent`, `target_resource_type` (`predio`, `lote` o, en
las asignaciones, `user`) y
`target_resource_id`.

## Análisis satelital (Fase 3)

Diseño en [ADR-004](decisions/ADR-004-satellite-analysis.md). Los eventos
del worker no tienen request HTTP: se atribuyen a quien pidió el análisis
(`actor_user_id`), sin `actor_role` ni `actor_ip`. `target_resource_type`
es `analysis` y `target_resource_id` el id del análisis, salvo en los dos
eventos de Sentinel Hub, que no son de un análisis en particular y no
tienen actor ni recurso.

| Evento | Categoría | Severidad | Cuándo | `details` |
|---|---|---|---|---|
| `ANALYSIS_REQUESTED` | DOMAIN | INFO | Se crea un análisis PENDING y se encola | `analysis_type`, `date_from`, `date_to`, `force` |
| `ANALYSIS_STARTED` | DOMAIN | INFO | El worker toma el análisis y lo pasa a RUNNING | vacío |
| `ANALYSIS_COMPLETED` | DOMAIN | INFO | Termina COMPLETED | `scene_id`, `scene_date`, `cloud_coverage`, `valid_pixels_ratio`, `processing_units_spent`, `reflectance_percentiles` (p1/p50/p99 de B04 y B08), `lotes_measured`, `lotes_excluded`, `anomalies_by_severity`, `duration_s` |
| `ANALYSIS_FAILED` | DOMAIN | WARNING | Termina FAILED, incluido el barrido de zombies (`TIMEOUT`, `QUEUE_TIMEOUT`) | `error_code`, `duration_s` o `timeout_s` |
| `ANALYSIS_NO_SUITABLE_SCENE` | DOMAIN | INFO | Ninguna fecha bajo el umbral de nubes, o escena con pocos píxeles válidos | `scenes_evaluated`, `best_cloud_fraction`, `valid_pixels_ratio`, `processing_units_spent`, `duration_s` |
| `ANALYSIS_RESOLVED_TO_EXISTING` | DOMAIN | INFO | La escena ya tenía un resultado vigente: termina DUPLICATE_SCENE sin descargar bandas | `resolved_to_analysis_id`, `scene_id`, `scene_date`, `processing_units_spent`, `duration_s` |
| `ANALYSIS_FORCED_RECOMPUTE` | DOMAIN | WARNING | Un recómputo con `force` reemplaza al resultado vigente de la escena | `superseded_analysis_id`, `scene_id`, `reviewed_anomalies` |
| `ANOMALY_DETECTED` | DOMAIN | INFO | Un análisis COMPLETED tiene al menos una anomalía (uno por análisis, no por anomalía) | `count`, `by_severity` |
| `ANOMALY_REVIEWED` | DOMAIN | INFO | Un agrónomo revisa una anomalía | `anomaly_id`, `review_status`, `previous_review_status`, `severity`, `has_notes` (nunca el texto de la nota) |
| `RASTER_DOWNLOADED` | DOMAIN | INFO | Se descarga el GeoTIFF de un análisis | `artifact` |
| `SENTINEL_QUOTA_EXCEEDED` | SYSTEM | ERROR | Se agotó uno de los dos presupuestos mensuales: Processing Units o requests | `quota` (`PROCESSING_UNITS` o `REQUESTS`) y, según cuál, `spent_pu` y `budget_pu`, o `requests_made` y `budget_requests` |
| `SENTINEL_CIRCUIT_OPEN` | SYSTEM | WARNING | El circuit breaker se abre tras fallos consecutivos | `consecutive_failures`, `reset_s`, `last_error` |

Nunca un raster, nunca la geometría de una anomalía ni del predio, nunca el
token ni las credenciales de Sentinel Hub.

`ANALYSIS_FAILED` es WARNING porque es una falla del sistema, a diferencia de
`ANALYSIS_NO_SUITABLE_SCENE`, que es el cielo nublado. `ANALYSIS_FORCED_RECOMPUTE`
es WARNING porque gasta cuota dos veces sobre la misma escena y deja atrás
las revisiones del análisis anterior (KL-007). `SENTINEL_QUOTA_EXCEEDED` es
ERROR: hasta el mes siguiente no se puede correr ningún análisis. El de
requests suele agotarse primero (unas 3 por análisis, ver ADR-004).

## Por qué WARNING

Es WARNING lo que no se puede reconstruir desde el propio audit log:

- **Cambios de geometría.** El límite anterior se sobrescribe y no se
  conserva (KL-001). El evento, con el área y el bbox anteriores, es el
  único rastro de que el predio tuvo otra forma.
- **Borrados.** Aunque son soft delete, sacan el predio o lote de todas
  las vistas.
- **Asignaciones de usuarios.** Cambian quién puede actuar sobre un
  predio.

Los rechazos por falta de permiso se registran aparte como
`AUTHORIZATION_DENIED` (SECURITY, WARNING) desde
`require_predio_access`.

## Limitaciones

La escritura es best-effort (KL-002): si el INSERT en `audit_log`
falla, la operación continúa y queda un CRITICAL en el log de la
aplicación. Ninguno de estos eventos es todavía bloqueante.
