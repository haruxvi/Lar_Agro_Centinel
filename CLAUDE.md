# Lar Agro Centinel — Contexto para agentes

## Reglas de colaboración

- NO ejecutar `git commit` ni `git push`. El fundador (haruxvi)
  commitea con su identidad. Los agentes pueden crear y modificar
  archivos, correr tests y hacer `git add`. Nada más.
- NO firmar código ni commits como generado por IA.
- NO aparecer como contributor del repositorio.
- Ante ambigüedad en una instrucción: preguntar, no asumir.
- Si un test falla: reportar el traceback. Nunca modificar el test
  para que pase.

## Invariantes arquitectónicos

Estas reglas están verificadas por tests en
`backend/tests/architecture/`. Si un cambio las rompe, el CI falla.

### 1. Los servicios de dominio no conocen el framework web

Ningún módulo bajo `backend/app/modules/*/service.py`,
`repository.py` o `models.py` puede importar `fastapi` ni `starlette`.

Los servicios lanzan excepciones de dominio definidas en el
`exceptions.py` de su propio módulo. El `router.py` es el único que
traduce esas excepciones a códigos HTTP.

Razón: los servicios se invocan desde contextos sin request HTTP —
jobs en background, scripts de importación, tareas programadas,
comandos de mantenimiento. Un servicio que lanza `HTTPException` no
es reutilizable fuera de un endpoint y obliga a duplicar la lógica.

Ejemplo correcto:

    # modules/predios/exceptions.py
    class LoteNotContained(DomainError): ...

    # modules/predios/service.py
    if not lote_within_predio(...):
        raise LoteNotContained(predio_id=..., overflow_m2=...)

    # modules/predios/router.py
    @router.post("/{predio_id}/lotes")
    async def create_lote(...):
        try:
            return await service.create_lote(...)
        except LoteNotContained as e:
            raise HTTPException(422, detail=e.as_dict())

### 2. El audit log es append-only

`audit_log` no admite UPDATE, DELETE ni TRUNCATE. Está protegido por
triggers a nivel de base y por privilegios acotados del rol
`lar_app`. El servicio de audit expone únicamente escritura de
eventos nuevos.

No agregar métodos de modificación ni "corrección" de eventos. Un
evento erróneo se corrige escribiendo un evento nuevo que lo
contradiga, nunca editando el original.

### 3. Todo endpoint con `predio_id` usa `require_predio_access`

Sin excepción. Es el punto donde el aislamiento multitenancy se hace
efectivo. Un endpoint que recibe `predio_id` y no valida acceso es
una vulnerabilidad IDOR, no un descuido de estilo.

Un rol **global** solo alcanza todos los predios si está en
`GLOBAL_SCOPE_ROLES` (`app/shared/permissions.py`), hoy solo AUDITOR,
y esos roles no pueden tener permisos de escritura. Cualquier otro rol
global (p. ej. un PROPIETARIO global, que existe para poder crear su
primer predio) no da acceso a predios sobre los que no tiene grant.
Ampliar `GLOBAL_SCOPE_ROLES` es una decisión de seguridad, no un
ajuste.

Un predio nunca queda sin PROPIETARIO, y el rol PROPIETARIO de su
dueño (`owner_user_id`) no se puede revocar: si no, un co-propietario
invitado por el dueño podría dejarlo fuera de su propio predio. Eso
cambia solo cuando exista una transferencia de propiedad como
operación propia y auditada.

### 4. Constantes compartidas entre backend y frontend se generan

`frontend/src/config/*.generated.ts` se produce desde el backend con
los scripts de `scripts/`. No editarlos a mano. El CI verifica
sincronización con `--check`.

## Deuda conocida

Ver `docs/KNOWN-LIMITATIONS.md`. Leerlo antes de tomar decisiones de
diseño que puedan agravar alguno de los puntos listados ahí.
