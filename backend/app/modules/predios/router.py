"""HTTP surface of the predios context.

Every route under ``/{predio_id}`` declares ``require_predio_access``; a test
in ``tests/architecture`` fails if one does not. Domain errors are translated
to HTTP here and nowhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, cast

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from geoalchemy2 import WKBElement
from shapely.geometry import Point
from sqlalchemy.orm import Session

from app.modules.predios.exceptions import (
    AreaOutOfBoundsError,
    AssignmentExistsError,
    AssignmentNotFoundError,
    GeoJSONImportError,
    GeometryRepairExceedsThresholdError,
    InvalidSlugError,
    LastPropietarioError,
    LoteNameConflictError,
    LoteNotContainedError,
    LoteNotFoundError,
    LotesOverlapError,
    MemberNotFoundError,
    OwnerRoleProtectedError,
    PredioAccessDeniedError,
    PredioError,
    PredioHasActiveLotesError,
    PredioNotFoundError,
    RoleNotAssignableError,
    SlugConflictError,
)
from app.modules.predios.models import Lote, Predio
from app.modules.predios.schema import (
    GeometryRepairRead,
    GeometryValidationRead,
    GeometryValidationRequest,
    GeometryWarningRead,
    LoteCreate,
    LoteImportResponse,
    LoteListItem,
    LoteRead,
    LoteUpdate,
    LoteWriteResponse,
    PredioCreate,
    PredioListItem,
    PredioMemberCreate,
    PredioMemberRead,
    PredioPage,
    PredioRead,
    PredioSummaryRead,
    PredioUpdate,
    PredioWriteResponse,
)
from app.modules.predios.service import Actor, GeometryWarning, PredioService
from app.modules.users.models import User, UserPredioRole
from app.shared.db import get_session
from app.shared.dependencies import (
    CurrentUser,
    client_ip,
    get_current_user,
    require_permission,
    require_predio_access,
    user_agent,
)
from app.shared.exceptions import DomainError
from app.shared.geo import (
    GeoJSONTooLargeError,
    InvalidGeometryError,
    geometry_to_geojson,
    wkb_to_geometry,
)
from app.shared.pagination import PageParams
from app.shared.permissions import Permission
from app.shared.rate_limit import (
    geojson_import_limit,
    geometry_validation_limit,
    geometry_write_limit,
    limiter,
    membership_write_limit,
)
from app.shared.roles import Role

router = APIRouter(prefix="/predios", tags=["predios"])

SessionDep = Annotated[Session, Depends(get_session)]

_VIEW = [Depends(require_predio_access(Permission.PREDIO_VIEW))]
_UPDATE = [Depends(require_predio_access(Permission.PREDIO_UPDATE))]
_DELETE = [Depends(require_predio_access(Permission.PREDIO_DELETE))]
_VIEW_USERS = [Depends(require_predio_access(Permission.USER_VIEW))]
_ASSIGN = [Depends(require_predio_access(Permission.PREDIO_ASSIGN_USERS))]

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    PredioNotFoundError: status.HTTP_404_NOT_FOUND,
    LoteNotFoundError: status.HTTP_404_NOT_FOUND,
    PredioAccessDeniedError: status.HTTP_403_FORBIDDEN,
    SlugConflictError: status.HTTP_409_CONFLICT,
    LoteNameConflictError: status.HTTP_409_CONFLICT,
    PredioHasActiveLotesError: status.HTTP_409_CONFLICT,
    AssignmentExistsError: status.HTTP_409_CONFLICT,
    LastPropietarioError: status.HTTP_409_CONFLICT,
    OwnerRoleProtectedError: status.HTTP_409_CONFLICT,
    MemberNotFoundError: status.HTTP_404_NOT_FOUND,
    AssignmentNotFoundError: status.HTTP_404_NOT_FOUND,
    RoleNotAssignableError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidSlugError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidGeometryError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    AreaOutOfBoundsError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    LoteNotContainedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    LotesOverlapError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    GeoJSONImportError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    GeoJSONTooLargeError: status.HTTP_413_CONTENT_TOO_LARGE,
    GeometryRepairExceedsThresholdError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


@contextmanager
def _domain_errors() -> Iterator[None]:
    """Translate the service's domain errors into HTTP responses."""
    try:
        yield
    except (PredioError, InvalidGeometryError, GeoJSONTooLargeError) as exc:
        code = _STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=code, detail=exc.as_dict()) from exc


def get_actor(
    request: Request, user: Annotated[CurrentUser, Depends(get_current_user)]
) -> Actor:
    """Build the acting identity, and key rate limits on the user."""
    request.state.user_id = user.id
    return Actor(
        user_id=user.id,
        role=user.active_role,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )


ActorDep = Annotated[Actor, Depends(get_actor)]


# --- presentation -------------------------------------------------------------


def _hectares(area_m2: float) -> float:
    return round(area_m2 / 10_000, 4)


def _lon_lat(element: WKBElement) -> tuple[float, float]:
    point = cast(Point, wkb_to_geometry(element))
    return (round(point.x, 7), round(point.y, 7))


def _bbox(element: WKBElement) -> tuple[float, float, float, float]:
    min_lon, min_lat, max_lon, max_lat = wkb_to_geometry(element).bounds
    return (round(min_lon, 7), round(min_lat, 7), round(max_lon, 7), round(max_lat, 7))


def _warnings(warnings: list[GeometryWarning]) -> list[GeometryWarningRead]:
    return [GeometryWarningRead(code=w.code, detail=w.detail) for w in warnings]


def _predio_item(predio: Predio) -> PredioListItem:
    return PredioListItem(
        id=predio.id,
        name=predio.name,
        slug=predio.slug,
        area_m2=round(predio.area_m2, 2),
        area_ha=_hectares(predio.area_m2),
        region=predio.region,
        comuna=predio.comuna,
        centroid=_lon_lat(predio.centroid),
        created_at=predio.created_at,
    )


def _predio_read(predio: Predio) -> PredioRead:
    return PredioRead(
        **_predio_item(predio).model_dump(),
        owner_user_id=predio.owner_user_id,
        description=predio.description,
        address=predio.address,
        rol_sii=predio.rol_sii,
        bbox=_bbox(predio.geometry),
        geometry=geometry_to_geojson(wkb_to_geometry(predio.geometry)),
        geometry_was_repaired=predio.geometry_was_repaired,
        geometry_repair_area_delta_m2=predio.geometry_repair_area_delta_m2,
        updated_at=predio.updated_at,
    )


def _lote_item(lote: Lote) -> LoteListItem:
    return LoteListItem(
        id=lote.id,
        predio_id=lote.predio_id,
        name=lote.name,
        code=lote.code,
        lote_type=lote.lote_type,
        area_m2=round(lote.area_m2, 2),
        area_ha=_hectares(lote.area_m2),
        crop_type=lote.crop_type,
        variety=lote.variety,
        centroid=_lon_lat(lote.centroid),
    )


def _lote_read(lote: Lote) -> LoteRead:
    return LoteRead(
        **_lote_item(lote).model_dump(),
        planting_year=lote.planting_year,
        plant_count=lote.plant_count,
        row_spacing_m=lote.row_spacing_m,
        plant_spacing_m=lote.plant_spacing_m,
        notes=lote.notes,
        geometry=geometry_to_geojson(wkb_to_geometry(lote.geometry)),
        geometry_was_repaired=lote.geometry_was_repaired,
        geometry_repair_area_delta_m2=lote.geometry_repair_area_delta_m2,
        created_at=lote.created_at,
        updated_at=lote.updated_at,
    )


# --- predios ------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.PREDIO_CREATE))],
)
@limiter.limit(geometry_write_limit)
def create_predio(
    payload: PredioCreate,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> PredioWriteResponse:
    """Create a predio. The caller becomes its owner and PROPIETARIO."""
    fields = payload.model_dump(exclude={"geometry", "accept_repair"}, exclude_none=True)
    with _domain_errors():
        result = PredioService(session).create_predio(
            actor,
            geojson=payload.geometry,
            fields=fields,
            accept_repair=payload.accept_repair,
        )
    session.refresh(result.predio)
    return PredioWriteResponse(
        predio=_predio_read(result.predio), warnings=_warnings(result.warnings)
    )


@router.get("")
def list_predios(
    actor: ActorDep,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1)] = 20,
) -> PredioPage:
    """List the predios the caller may view. No geometries."""
    result = PredioService(session).list_predios(
        actor, PageParams(page=page, page_size=page_size)
    )
    return PredioPage(
        items=[_predio_item(predio) for predio in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        pages=result.pages,
    )


@router.post(
    "/validate-geometry",
    dependencies=[Depends(require_permission(Permission.PREDIO_CREATE))],
)
@limiter.limit(geometry_validation_limit)
def validate_geometry(
    payload: GeometryValidationRequest,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> GeometryValidationRead:
    """Check a geometry without storing it.

    A geometry that cannot be stored at all answers 422. One that can, even if
    only after a repair that needs confirmation, answers 200 with the stored
    form and, when repaired, both areas and the delta, whatever the threshold.
    """
    with _domain_errors():
        report = PredioService(session).inspect_geometry(
            payload.geometry, owner_user_id=actor.user_id
        )
    min_lon, min_lat, max_lon, max_lat = report.bbox
    repair = report.repair
    return GeometryValidationRead(
        valid=True,
        geometry=geometry_to_geojson(report.geometry),
        repaired=repair is not None,
        repair=None
        if repair is None
        else GeometryRepairRead.model_validate(
            {
                **repair.as_details(),
                "requires_confirmation": not repair.accepted_automatically,
            }
        ),
        area_m2=round(report.area_m2, 2),
        area_ha=_hectares(report.area_m2),
        vertex_count=report.vertex_count,
        centroid=(round(report.centroid.x, 7), round(report.centroid.y, 7)),
        bbox=(min_lon, min_lat, max_lon, max_lat),
        warnings=_warnings(report.warnings),
    )


@router.get("/{predio_id}", dependencies=_VIEW)
def get_predio(predio_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> PredioRead:
    """Return one predio with its boundary."""
    with _domain_errors():
        predio = PredioService(session).get_predio(actor, predio_id)
    return _predio_read(predio)


@router.patch("/{predio_id}", dependencies=_UPDATE)
@limiter.limit(geometry_write_limit)
def update_predio(
    predio_id: uuid.UUID,
    payload: PredioUpdate,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> PredioWriteResponse:
    """Edit a predio. Sending ``geometry`` replaces its boundary."""
    fields = payload.model_dump(exclude={"geometry", "accept_repair"}, exclude_unset=True)
    with _domain_errors():
        result = PredioService(session).update_predio(
            actor,
            predio_id,
            fields=fields,
            geojson=payload.geometry,
            accept_repair=payload.accept_repair,
        )
    session.refresh(result.predio)
    return PredioWriteResponse(
        predio=_predio_read(result.predio), warnings=_warnings(result.warnings)
    )


@router.delete(
    "/{predio_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_DELETE
)
def delete_predio(predio_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> None:
    """Soft-delete a predio. Refused while it has live lotes."""
    with _domain_errors():
        PredioService(session).delete_predio(actor, predio_id)


@router.get("/{predio_id}/summary", dependencies=_VIEW)
def predio_summary(
    predio_id: uuid.UUID, actor: ActorDep, session: SessionDep
) -> PredioSummaryRead:
    """Return the headline figures of a predio."""
    with _domain_errors():
        summary = PredioService(session).summarize(actor, predio_id)
    min_lon, min_lat, max_lon, max_lat = summary.bbox
    return PredioSummaryRead(
        predio_id=summary.predio_id,
        area_m2=round(summary.area_m2, 2),
        area_ha=_hectares(summary.area_m2),
        lotes_count=summary.lotes_count,
        lotes_area_m2=round(summary.lotes_area_m2, 2),
        unassigned_area_m2=round(summary.unassigned_area_m2, 2),
        coverage_ratio=round(summary.coverage_ratio, 4),
        lotes_by_type=summary.lotes_by_type,
        centroid=(round(summary.centroid.x, 7), round(summary.centroid.y, 7)),
        bbox=(min_lon, min_lat, max_lon, max_lat),
    )


# --- lotes --------------------------------------------------------------------


@router.get("/{predio_id}/lotes", dependencies=_VIEW)
def list_lotes(
    predio_id: uuid.UUID, actor: ActorDep, session: SessionDep
) -> list[LoteListItem]:
    """List a predio's lotes. No geometries."""
    with _domain_errors():
        lotes = PredioService(session).list_lotes(actor, predio_id)
    return [_lote_item(lote) for lote in lotes]


@router.post(
    "/{predio_id}/lotes", status_code=status.HTTP_201_CREATED, dependencies=_UPDATE
)
@limiter.limit(geometry_write_limit)
def create_lote(
    predio_id: uuid.UUID,
    payload: LoteCreate,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> LoteWriteResponse:
    """Create a lote inside the predio, clear of its siblings."""
    fields = payload.model_dump(exclude={"geometry", "accept_repair"}, exclude_none=True)
    with _domain_errors():
        result = PredioService(session).create_lote(
            actor,
            predio_id,
            geojson=payload.geometry,
            fields=fields,
            accept_repair=payload.accept_repair,
        )
    session.refresh(result.lote)
    return LoteWriteResponse(
        lote=_lote_read(result.lote), warnings=_warnings(result.warnings)
    )


@router.post(
    "/{predio_id}/lotes/import-geojson",
    status_code=status.HTTP_201_CREATED,
    dependencies=_UPDATE,
)
@limiter.limit(geojson_import_limit)
def import_lotes(
    predio_id: uuid.UUID,
    payload: Annotated[dict[str, Any], Body()],
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> LoteImportResponse:
    """Create lotes from a FeatureCollection: all of them, or none.

    A top-level ``accept_repair: true`` (a GeoJSON foreign member) confirms
    every repair of the import at once.
    """
    accept_repair = payload.pop("accept_repair", False)
    if not isinstance(accept_repair, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "InvalidFlag", "field": "accept_repair"},
        )
    with _domain_errors():
        results = PredioService(session).import_lotes(
            actor, predio_id, payload, accept_repair=accept_repair
        )
    for result in results:
        session.refresh(result.lote)
    return LoteImportResponse(
        created=[_lote_item(result.lote) for result in results],
        warnings={
            index: _warnings(result.warnings)
            for index, result in enumerate(results)
            if result.warnings
        },
    )


@router.get("/{predio_id}/lotes/{lote_id}", dependencies=_VIEW)
def get_lote(
    predio_id: uuid.UUID, lote_id: uuid.UUID, actor: ActorDep, session: SessionDep
) -> LoteRead:
    """Return one lote with its boundary. A lote of another predio is a 404."""
    with _domain_errors():
        lote = PredioService(session).get_lote(actor, predio_id, lote_id)
    return _lote_read(lote)


@router.patch("/{predio_id}/lotes/{lote_id}", dependencies=_UPDATE)
@limiter.limit(geometry_write_limit)
def update_lote(
    predio_id: uuid.UUID,
    lote_id: uuid.UUID,
    payload: LoteUpdate,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> LoteWriteResponse:
    """Edit a lote. Sending ``geometry`` replaces its boundary."""
    fields = payload.model_dump(exclude={"geometry", "accept_repair"}, exclude_unset=True)
    with _domain_errors():
        result = PredioService(session).update_lote(
            actor,
            predio_id,
            lote_id,
            fields=fields,
            geojson=payload.geometry,
            accept_repair=payload.accept_repair,
        )
    session.refresh(result.lote)
    return LoteWriteResponse(
        lote=_lote_read(result.lote), warnings=_warnings(result.warnings)
    )


@router.delete(
    "/{predio_id}/lotes/{lote_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_UPDATE,
)
def delete_lote(
    predio_id: uuid.UUID, lote_id: uuid.UUID, actor: ActorDep, session: SessionDep
) -> None:
    """Soft-delete a lote."""
    with _domain_errors():
        PredioService(session).delete_lote(actor, predio_id, lote_id)


# --- membership ---------------------------------------------------------------


def _member_read(grant: UserPredioRole, user: User) -> PredioMemberRead:
    return PredioMemberRead(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=grant.role,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        granted_by_user_id=grant.granted_by_user_id,
    )


@router.get("/{predio_id}/users", dependencies=_VIEW_USERS)
def list_members(
    predio_id: uuid.UUID, actor: ActorDep, session: SessionDep
) -> list[PredioMemberRead]:
    """List who holds which role on the predio."""
    with _domain_errors():
        members = PredioService(session).list_members(actor, predio_id)
    return [_member_read(grant, user) for grant, user in members]


@router.post(
    "/{predio_id}/users", status_code=status.HTTP_201_CREATED, dependencies=_ASSIGN
)
@limiter.limit(membership_write_limit)
def assign_member(
    predio_id: uuid.UUID,
    payload: PredioMemberCreate,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> PredioMemberRead:
    """Grant a role on the predio to a registered user."""
    with _domain_errors():
        grant, user = PredioService(session).assign_member(
            actor,
            predio_id,
            email=payload.email,
            role=payload.role,
            expires_at=payload.expires_at,
        )
    session.refresh(grant)
    return _member_read(grant, user)


@router.delete(
    "/{predio_id}/users/{user_id}/roles/{role}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_ASSIGN,
)
@limiter.limit(membership_write_limit)
def unassign_member(
    predio_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    request: Request,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> None:
    """Revoke a role on the predio. The last PROPIETARIO cannot be removed."""
    with _domain_errors():
        PredioService(session).unassign_member(actor, predio_id, user_id, role)
