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
