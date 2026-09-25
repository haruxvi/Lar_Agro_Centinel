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
