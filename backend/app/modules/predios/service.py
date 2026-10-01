"""Business rules for predios and their lotes.

The service is invoked from HTTP handlers, seed scripts and, later, background
jobs, so it knows nothing about the web layer: it raises the domain errors in
``exceptions.py`` and the router translates them.

Authorization is checked here as well as in ``require_predio_access``. The
dependency is the gate for HTTP; this check is what keeps a script or a job
from acting on a predio its actor has no grant on. Both fail closed.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

import shapely
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.service import AuditService
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
    PredioHasActiveLotesError,
    PredioNotFoundError,
    RoleNotAssignableError,
    SlugConflictError,
)
from app.modules.predios.models import Lote, Predio
from app.modules.predios.repository import LoteRepository, PredioRepository
from app.modules.users.models import User, UserPredioRole
from app.modules.users.repository import UserRepository, UserRoleRepository
from app.shared.config import get_settings
from app.shared.enums import AuditOutcome, LoteType
from app.shared.geo import (
    InvalidGeometryError,
    RepairAssessment,
    assess_repair,
    contains_geojson,
    geojson_to_wkb,
    geometry_to_geojson,
    inspect_geojson_geometry,
    is_within_chile_bbox,
    wkb_to_geometry,
)
from app.shared.pagination import Page, PageParams
from app.shared.permissions import (
    Permission,
    can_access_predio,
    cross_tenant_roles,
    has_permission,
    roles_on_predio,
)
from app.shared.roles import Role

SLUG_MAX_LENGTH: Final = 120
_SLUG_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_SLUG_CHARS: Final = re.compile(r"[^a-z0-9]+")

MAX_IMPORT_FEATURES: Final = 500

PREDIO_FIELDS: Final = frozenset(
    {"name", "slug", "description", "region", "comuna", "address", "rol_sii"}
)
LOTE_FIELDS: Final = frozenset(
    {
        "name",
        "code",
        "lote_type",
        "crop_type",
        "variety",
        "planting_year",
        "plant_count",
        "row_spacing_m",
        "plant_spacing_m",
        "notes",
    }
)

# Warning codes returned alongside an accepted geometry.
WARNING_OUTSIDE_CHILE: Final = "OUTSIDE_CHILE_BBOX"
WARNING_REPAIRED: Final = "GEOMETRY_REPAIRED"
WARNING_OVERLAPS_PREDIO: Final = "OVERLAPS_OWN_PREDIO"


# --- value objects ------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who is acting, for authorization and the audit trail."""

    user_id: uuid.UUID
    role: Role | None = None
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class GeometryWarning:
    """Something worth telling the user about a geometry that was accepted."""

    code: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GeometryReport:
    """A validated geometry with everything derived from it in the database."""

    geometry: BaseGeometry
    area_m2: float
    centroid: Point
    vertex_count: int
    warnings: list[GeometryWarning]
    repair: RepairAssessment | None = None
    """Set when the input was invalid and repaired; None for a valid input."""

    @property
    def bbox(self) -> list[float]:
        """Return (min_lon, min_lat, max_lon, max_lat), rounded for display."""
        return [round(value, 7) for value in self.geometry.bounds]


@dataclass(frozen=True)
class PredioResult:
    """A predio that was written, and the warnings its geometry raised."""

    predio: Predio
    warnings: list[GeometryWarning]


@dataclass(frozen=True)
class LoteResult:
    """A lote that was written, and the warnings its geometry raised."""

    lote: Lote
    warnings: list[GeometryWarning]


@dataclass(frozen=True)
class PredioSummary:
    """Headline figures of a predio and its lotes."""

    predio_id: uuid.UUID
    area_m2: float
    lotes_count: int
    lotes_area_m2: float
    unassigned_area_m2: float
    coverage_ratio: float
    lotes_by_type: dict[LoteType, int]
    centroid: Point
    bbox: list[float]


# --- helpers ------------------------------------------------------------------


def slugify(text: str) -> str:
    """Return a URL-safe slug: ASCII lowercase words joined by hyphens."""
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    slug = _NON_SLUG_CHARS.sub("-", ascii_text.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-") or "predio"


def _ensure_valid_slug(slug: str) -> str:
    if len(slug) > SLUG_MAX_LENGTH or not _SLUG_PATTERN.fullmatch(slug):
        raise InvalidSlugError(slug)
    return slug


def _only(changes: Mapping[str, object], allowed: frozenset[str]) -> dict[str, object]:
    unknown = set(changes) - allowed
    if unknown:
        # A programming error, not user input: schemas only pass known fields.
        raise ValueError(f"fields not editable: {sorted(unknown)}")
    return dict(changes)


def _vertex_count(geometry: BaseGeometry) -> int:
    return int(shapely.get_num_coordinates(geometry))


def _geometry_summary(geometry: BaseGeometry, area_m2: float) -> dict[str, object]:
    """Describe a geometry for the audit trail without storing the geometry."""
    return {
        "area_m2": round(area_m2, 2),
        "bbox": [round(value, 7) for value in geometry.bounds],
        "vertex_count": _vertex_count(geometry),
        "geometry_type": geometry.geom_type,
    }


def _repair_columns(report: GeometryReport) -> dict[str, object]:
    """Traceability columns for a geometry about to be stored.

    A valid geometry resets them: the flag describes the boundary currently
    stored, not its history (which lives in the audit trail).
    """
    repair = report.repair
    return {
        "geometry_was_repaired": repair is not None,
        "geometry_repair_area_delta_m2": (
            round(repair.change_m2, 2)
            if repair is not None and repair.measurable
            else None
        ),
    }


def _repair_details(report: GeometryReport) -> dict[str, object]:
    """Audit details of a repair: figures and bbox, never the geometry."""
    if report.repair is None:
        raise ValueError("_repair_details needs a repaired geometry")
    return {
        **report.repair.as_details(),
        "bbox": report.bbox,
        "vertex_count": report.vertex_count,
    }


def _unassignable_reason(
    role: Role, expires_at: datetime | None, now: datetime
) -> str | None:
    """Return why ``role`` cannot be granted on a predio, or None if it can."""
    if role is Role.APICULTOR:
        return "external role, not scoped to a predio"
    if expires_at is not None:
        if expires_at.tzinfo is None:
            return "expires_at must include a timezone"
        if expires_at <= now:
            return "expires_at must be in the future"
        if role is Role.PROPIETARIO:
            # An expiring PROPIETARIO could leave the predio without one.
            return "PROPIETARIO grants do not expire"
    return None


class PredioService:
    """Creates, edits and deletes predios and lotes, enforcing their invariants."""

    def __init__(self, session: Session) -> None:
        """Bind the service to a database session."""
        self._session = session
        self._predios = PredioRepository(session)
        self._lotes = LoteRepository(session)
        self._roles = UserRoleRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditService(session)

    # --- authorization --------------------------------------------------------

    def _global_roles(self, user_id: uuid.UUID) -> list[Role]:
        return [grant.role for grant in self._roles.list_global_roles(user_id)]

    def authorize(
        self, actor: Actor, predio_id: uuid.UUID, permission: Permission
    ) -> None:
        """Raise unless ``actor`` may exercise ``permission`` on the predio."""
        grants = list(self._roles.list_predio_roles(actor.user_id))
        if not can_access_predio(
            grants,
            predio_id,
            permission,
            global_roles=self._global_roles(actor.user_id),
        ):
            raise PredioAccessDeniedError

    def _authorize_anywhere(self, actor: Actor, permission: Permission) -> None:
        """Raise unless any of the actor's roles grants ``permission``."""
        roles = set(self._global_roles(actor.user_id))
        roles |= {grant.role for grant in self._roles.list_predio_roles(actor.user_id)}
        if not has_permission(roles, permission):
            raise PredioAccessDeniedError

    def _load_predio(
        self, actor: Actor, predio_id: uuid.UUID, permission: Permission
    ) -> Predio:
        # Authorization first: an actor without a grant learns nothing, not
        # even whether the predio exists.
        self.authorize(actor, predio_id, permission)
        predio = self._predios.get_by_id(predio_id)
        if predio is None:
            raise PredioNotFoundError
        return predio

    def _load_lote(self, predio_id: uuid.UUID, lote_id: uuid.UUID) -> Lote:
        lote = self._lotes.get_in_predio(predio_id, lote_id)
        if lote is None:
            raise LoteNotFoundError
        return lote

    # --- audit ----------------------------------------------------------------

    def _record(
        self,
        actor: Actor,
        *,
        event_type: str,
        action: str,
        predio_id: uuid.UUID | None,
        target_resource_type: str,
        target_resource_id: uuid.UUID | None,
        details: dict[str, Any],
    ) -> None:
        """Append a catalogued event. Category and severity come from the catalogue.

        An event missing from ``events.PREDIO_EVENTS`` raises ``KeyError``, and
        details carrying a geometry raise ``ValueError``: both are programming
        errors, and both abort the operation before it commits rather than
        write an unclassified or oversized record.
        """
        spec = events.PREDIO_EVENTS[event_type]
        if contains_geojson(details):
            raise ValueError(f"{event_type}: audit details must not carry a geometry")
        self._audit.record(
            event_category=spec.category,
            event_type=event_type,
            severity=spec.severity,
            action=action,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=actor.user_id,
            actor_role=actor.role,
            actor_ip=actor.ip,
            actor_user_agent=actor.user_agent,
            target_resource_type=target_resource_type,
            target_resource_id=target_resource_id,
            predio_id=predio_id,
            details=details,
        )

    # --- geometry -------------------------------------------------------------

    def inspect_geometry(
        self,
        geojson: dict[str, Any],
        *,
        owner_user_id: uuid.UUID | None = None,
        exclude_predio_id: uuid.UUID | None = None,
    ) -> GeometryReport:
        """Validate a GeoJSON geometry and derive its area and centroid.

        Raises ``InvalidGeometryError`` for anything that cannot be stored.
        Overlaps are only reported against ``owner_user_id``'s own predios:
        the existence of another tenant's predio is not the caller's business.
        """
        validated = inspect_geojson_geometry(geojson)
        geometry = validated.geometry
        area_m2 = self._predios.compute_area_m2(geometry)
        warnings: list[GeometryWarning] = []

        repair: RepairAssessment | None = None
        if validated.reference_geometry is not None:
            settings = get_settings()
            repair = assess_repair(
                area_before_m2=self._predios.compute_area_m2(
                    validated.reference_geometry
                ),
                area_after_m2=area_m2,
                self_intersecting=validated.self_intersecting,
                max_ratio=settings.geo_repair_max_area_change_ratio,
                ignore_below_m2=settings.geo_repair_ignore_below_m2,
            )
            warnings.append(GeometryWarning(WARNING_REPAIRED, repair.as_details()))
        if not is_within_chile_bbox(geometry):
            warnings.append(GeometryWarning(WARNING_OUTSIDE_CHILE))
        if owner_user_id is not None:
            overlaps = self._predios.find_overlapping_predios(
                geometry,
                exclude_predio_id=exclude_predio_id,
                owner_user_id=owner_user_id,
            )
            warnings.extend(
                GeometryWarning(
                    WARNING_OVERLAPS_PREDIO,
                    {"predio_id": str(predio.id), "overlap_m2": round(area, 2)},
                )
                for predio, area in overlaps
            )

        return GeometryReport(
            geometry=geometry,
            area_m2=area_m2,
            centroid=self._predios.compute_centroid(geometry),
            vertex_count=validated.vertex_count,
            warnings=warnings,
            repair=repair,
        )

    def _require_repair_confirmation(
        self,
        actor: Actor,
        report: GeometryReport,
        *,
        accept_repair: bool,
        predio_id: uuid.UUID | None,
        target_resource_type: str,
        target_resource_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> None:
        """Raise unless the repair, if there was one, may be stored.

        A repair within the threshold passes; beyond it, or unmeasurable, it
        passes only with ``accept_repair``. Otherwise GEOMETRY_REPAIR_REJECTED
        is recorded and committed before raising: the operation fails, but the
        pattern of rejections is what reveals a badly calibrated threshold.
        """
        repair = report.repair
        if repair is None or repair.accepted_automatically or accept_repair:
            return
        self._record(
            actor,
            event_type=events.GEOMETRY_REPAIR_REJECTED,
            action="ask to confirm a geometry repair",
            predio_id=predio_id,
            target_resource_type=target_resource_type,
            target_resource_id=target_resource_id,
            details=_repair_details(report),
        )
        if commit:
            self._session.commit()
        raise GeometryRepairExceedsThresholdError(
            repair, geometry_to_geojson(report.geometry)
        )

    def _record_repair(
        self,
        actor: Actor,
        report: GeometryReport,
        *,
        predio_id: uuid.UUID,
        target_resource_type: str,
        target_resource_id: uuid.UUID,
    ) -> None:
        """Audit a repair that was stored, automatically or on confirmation."""
        repair = report.repair
        if repair is None:
            return
        # Past _require_repair_confirmation, a repair outside the threshold can
        # only have been stored because someone confirmed it explicitly.
        confirmed = not repair.accepted_automatically
        self._record(
            actor,
            event_type=(
                events.GEOMETRY_REPAIR_ACCEPTED if confirmed else events.GEOMETRY_REPAIRED
            ),
            action=(
                "store a geometry repair on explicit confirmation"
                if confirmed
                else "store an automatic geometry repair"
            ),
            predio_id=predio_id,
            target_resource_type=target_resource_type,
            target_resource_id=target_resource_id,
            details=_repair_details(report),
        )

    def _ensure_predio_area(self, area_m2: float) -> None:
        settings = get_settings()
        low, high = settings.geo_min_predio_area_m2, settings.geo_max_predio_area_m2
        if not low <= area_m2 <= high:
            raise AreaOutOfBoundsError(area_m2, low, high)

    def _ensure_lote_fits(
        self,
        predio: Predio,
        lote_geometry: BaseGeometry,
        *,
        exclude_lote_id: uuid.UUID | None = None,
    ) -> None:
        """Raise unless the lote lies in its predio and clear of its siblings."""
        tolerance = get_settings().geo_lote_containment_tolerance_m
        predio_geometry = wkb_to_geometry(predio.geometry)
        if not self._predios.lote_within_predio(
            lote_geometry, predio_geometry, tolerance
        ):
            raise LoteNotContainedError(
                self._predios.area_outside_m2(lote_geometry, predio_geometry),
                tolerance,
                lote_id=exclude_lote_id,
            )
        overlaps = self._lotes.find_overlapping_lotes(
            predio.id, lote_geometry, exclude_lote_id=exclude_lote_id
        )
        if overlaps:
            raise LotesOverlapError([(lote.id, area) for lote, area in overlaps])

    # --- predios --------------------------------------------------------------

    def _unique_slug(self, owner_user_id: uuid.UUID, base: str) -> str:
        candidate, suffix = base, 2
        while self._predios.slug_exists(owner_user_id, candidate):
            tail = f"-{suffix}"
            candidate = f"{base[: SLUG_MAX_LENGTH - len(tail)].rstrip('-')}{tail}"
            suffix += 1
        return candidate

    def create_predio(
        self,
        actor: Actor,
        *,
        geojson: dict[str, Any],
        fields: Mapping[str, object],
        accept_repair: bool = False,
    ) -> PredioResult:
        """Create a predio owned by ``actor``, who becomes its PROPIETARIO."""
        self._authorize_anywhere(actor, Permission.PREDIO_CREATE)
        data = _only(fields, PREDIO_FIELDS)
        name = str(data.pop("name")).strip()

        requested_slug = data.pop("slug", None)
        if requested_slug:
            slug = _ensure_valid_slug(str(requested_slug))
            if self._predios.slug_exists(actor.user_id, slug):
                raise SlugConflictError(slug)
        else:
            slug = self._unique_slug(actor.user_id, slugify(name))

        report = self.inspect_geometry(geojson, owner_user_id=actor.user_id)
        self._ensure_predio_area(report.area_m2)
        self._require_repair_confirmation(
            actor,
            report,
            accept_repair=accept_repair,
            predio_id=None,
            target_resource_type="predio",
        )

        predio = Predio(
            owner_user_id=actor.user_id,
            created_by_user_id=actor.user_id,
            name=name,
            slug=slug,
            geometry=geojson_to_wkb(report.geometry),
            centroid=from_shape(report.centroid, srid=get_settings().geo_srid),
            area_m2=report.area_m2,
            **_repair_columns(report),
            **data,
        )
        try:
            self._predios.create(predio)
            self._roles.grant_predio(
                UserPredioRole(
                    user_id=actor.user_id,
                    predio_id=predio.id,
                    role=Role.PROPIETARIO,
                    granted_by_user_id=actor.user_id,
                )
            )
        except IntegrityError as exc:
            # A concurrent request took the slug between the check and the insert.
            self._session.rollback()
            raise SlugConflictError(slug) from exc

        self._record(
            actor,
            event_type=events.PREDIO_CREATED,
            action="create predio",
            predio_id=predio.id,
            target_resource_type="predio",
            target_resource_id=predio.id,
            details={
                "name": predio.name,
                "slug": predio.slug,
                **_geometry_summary(report.geometry, report.area_m2),
                "warnings": [warning.code for warning in report.warnings],
            },
        )
        self._record_repair(
            actor,
            report,
            predio_id=predio.id,
            target_resource_type="predio",
            target_resource_id=predio.id,
        )
        self._session.commit()
        return PredioResult(predio=predio, warnings=report.warnings)

    def get_predio(self, actor: Actor, predio_id: uuid.UUID) -> Predio:
        """Return a live predio the actor may view."""
        return self._load_predio(actor, predio_id, Permission.PREDIO_VIEW)

    def list_predios(self, actor: Actor, params: PageParams) -> Page[Predio]:
        """Return one page of the live predios the actor may view."""
        cross_tenant = cross_tenant_roles(self._global_roles(actor.user_id))
        if has_permission(cross_tenant, Permission.PREDIO_VIEW):
            return self._predios.list_all(params)
        grants = list(self._roles.list_predio_roles(actor.user_id))
        visible = [
            predio_id
            for predio_id in {grant.predio_id for grant in grants}
            if has_permission(roles_on_predio(grants, predio_id), Permission.PREDIO_VIEW)
        ]
        return self._predios.list_by_ids(visible, params)

    def update_predio(
        self,
        actor: Actor,
        predio_id: uuid.UUID,
        *,
        fields: Mapping[str, object],
        geojson: dict[str, Any] | None = None,
        accept_repair: bool = False,
    ) -> PredioResult:
        """Edit a predio's attributes and, optionally, its boundary.

        A new boundary must still contain every live lote. The previous
        boundary is not kept (KL-001); the audit trail records its area and
        bounding box.
        """
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_UPDATE)
        changes = _only(fields, PREDIO_FIELDS)
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()
        if changes.get("slug") is not None and changes["slug"] != predio.slug:
            slug = _ensure_valid_slug(str(changes["slug"]))
            if self._predios.slug_exists(predio.owner_user_id, slug):
                raise SlugConflictError(slug)
        elif "slug" in changes:
            del changes["slug"]

        warnings: list[GeometryWarning] = []
        geometry_change: dict[str, object] | None = None
        if geojson is not None:
            report = self.inspect_geometry(
                geojson, owner_user_id=predio.owner_user_id, exclude_predio_id=predio.id
            )
            self._ensure_predio_area(report.area_m2)
            tolerance = get_settings().geo_lote_containment_tolerance_m
            for lote in self._lotes.list_for_predio(predio.id):
                lote_geometry = wkb_to_geometry(lote.geometry)
                if not self._predios.lote_within_predio(
                    lote_geometry, report.geometry, tolerance
                ):
                    raise LoteNotContainedError(
                        self._predios.area_outside_m2(lote_geometry, report.geometry),
                        tolerance,
                        lote_id=lote.id,
                    )
            self._require_repair_confirmation(
                actor,
                report,
                accept_repair=accept_repair,
                predio_id=predio.id,
                target_resource_type="predio",
                target_resource_id=predio.id,
            )
            previous = wkb_to_geometry(predio.geometry)
            geometry_change = {
                "previous": _geometry_summary(previous, predio.area_m2),
                "new": _geometry_summary(report.geometry, report.area_m2),
                "area_delta_m2": round(report.area_m2 - predio.area_m2, 2),
                "warnings": [warning.code for warning in report.warnings],
            }
            changes.update(
                geometry=geojson_to_wkb(report.geometry),
                centroid=from_shape(report.centroid, srid=get_settings().geo_srid),
                area_m2=report.area_m2,
                **_repair_columns(report),
            )
            warnings = report.warnings
            repaired_report = report if report.repair is not None else None
        else:
            repaired_report = None

        changed_fields = sorted(
            key
            for key, value in changes.items()
            if key in PREDIO_FIELDS and getattr(predio, key) != value
        )
        try:
            self._predios.update(predio, changes)
        except IntegrityError as exc:
            self._session.rollback()
            raise SlugConflictError(str(changes.get("slug"))) from exc

        if changed_fields:
            self._record(
                actor,
                event_type=events.PREDIO_UPDATED,
                action="update predio",
                predio_id=predio.id,
                target_resource_type="predio",
                target_resource_id=predio.id,
                details={"fields": changed_fields},
            )
        if geometry_change is not None:
            self._record(
                actor,
                event_type=events.PREDIO_GEOMETRY_CHANGED,
                action="change predio boundary",
                predio_id=predio.id,
                target_resource_type="predio",
                target_resource_id=predio.id,
                details=geometry_change,
            )
        if repaired_report is not None:
            self._record_repair(
                actor,
                repaired_report,
                predio_id=predio.id,
                target_resource_type="predio",
                target_resource_id=predio.id,
            )
        self._session.commit()
        return PredioResult(predio=predio, warnings=warnings)

    def delete_predio(self, actor: Actor, predio_id: uuid.UUID) -> None:
        """Soft-delete a predio that has no live lotes left."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_DELETE)
        active = self._lotes.count_active(predio.id)
        if active:
            raise PredioHasActiveLotesError(active)

        self._predios.soft_delete(predio, actor.user_id)
        self._record(
            actor,
            event_type=events.PREDIO_DELETED,
            action="delete predio",
            predio_id=predio.id,
            target_resource_type="predio",
            target_resource_id=predio.id,
            details={"name": predio.name, "slug": predio.slug},
        )
        self._session.commit()

    def summarize(self, actor: Actor, predio_id: uuid.UUID) -> PredioSummary:
        """Return the headline figures of a predio."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_VIEW)
        lotes = self._lotes.list_for_predio(predio.id)
        lotes_area = self._lotes.sum_lotes_area(predio.id)
        by_type: dict[LoteType, int] = {}
        for lote in lotes:
            by_type[lote.lote_type] = by_type.get(lote.lote_type, 0) + 1
        geometry = wkb_to_geometry(predio.geometry)
        centroid = cast(Point, wkb_to_geometry(predio.centroid))
        return PredioSummary(
            predio_id=predio.id,
            area_m2=predio.area_m2,
            lotes_count=len(lotes),
            lotes_area_m2=lotes_area,
            # Lotes may exceed the boundary by the tolerance, so clamp.
            unassigned_area_m2=max(0.0, predio.area_m2 - lotes_area),
            coverage_ratio=min(1.0, lotes_area / predio.area_m2),
            lotes_by_type=by_type,
            centroid=centroid,
            bbox=[round(value, 7) for value in geometry.bounds],
        )

    # --- lotes ----------------------------------------------------------------

    def list_lotes(self, actor: Actor, predio_id: uuid.UUID) -> list[Lote]:
        """Return the live lotes of a predio the actor may view."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_VIEW)
        return self._lotes.list_for_predio(predio.id)

    def get_lote(self, actor: Actor, predio_id: uuid.UUID, lote_id: uuid.UUID) -> Lote:
        """Return a live lote, only through the predio it belongs to."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_VIEW)
        return self._load_lote(predio.id, lote_id)

    def _new_lote(
        self,
        actor: Actor,
        predio: Predio,
        data: dict[str, object],
        report: GeometryReport,
    ) -> Lote:
        return Lote(
            predio_id=predio.id,
            created_by_user_id=actor.user_id,
            name=str(data.pop("name")).strip(),
            lote_type=LoteType(str(data.pop("lote_type", LoteType.OTRO))),
            geometry=geojson_to_wkb(report.geometry),
            centroid=from_shape(report.centroid, srid=get_settings().geo_srid),
            area_m2=report.area_m2,
            **_repair_columns(report),
            **data,
        )

    def create_lote(
        self,
        actor: Actor,
        predio_id: uuid.UUID,
        *,
        geojson: dict[str, Any],
        fields: Mapping[str, object],
        accept_repair: bool = False,
    ) -> LoteResult:
        """Create a lote inside a predio, clear of its siblings."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_UPDATE)
        data = _only(fields, LOTE_FIELDS)
        name = str(data["name"]).strip()
        if name in {lote.name for lote in self._lotes.list_for_predio(predio.id)}:
            raise LoteNameConflictError(name)

        report = self.inspect_geometry(geojson)
        self._ensure_lote_fits(predio, report.geometry)
        self._require_repair_confirmation(
            actor,
            report,
            accept_repair=accept_repair,
            predio_id=predio.id,
            target_resource_type="lote",
        )

        lote = self._new_lote(actor, predio, data, report)
        try:
            self._lotes.create(lote)
        except IntegrityError as exc:
            self._session.rollback()
            raise LoteNameConflictError(name) from exc

        self._record(
            actor,
            event_type=events.LOTE_CREATED,
            action="create lote",
            predio_id=predio.id,
            target_resource_type="lote",
            target_resource_id=lote.id,
            details={
                "name": lote.name,
                "lote_type": lote.lote_type.value,
                **_geometry_summary(report.geometry, report.area_m2),
            },
        )
        self._record_repair(
            actor,
            report,
            predio_id=predio.id,
            target_resource_type="lote",
            target_resource_id=lote.id,
        )
        self._session.commit()
        return LoteResult(lote=lote, warnings=report.warnings)

    def update_lote(
        self,
        actor: Actor,
        predio_id: uuid.UUID,
        lote_id: uuid.UUID,
        *,
        fields: Mapping[str, object],
        geojson: dict[str, Any] | None = None,
        accept_repair: bool = False,
    ) -> LoteResult:
        """Edit a lote's attributes and, optionally, its boundary."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_UPDATE)
        lote = self._load_lote(predio.id, lote_id)
        changes = _only(fields, LOTE_FIELDS)
        if "name" in changes:
            name = str(changes["name"]).strip()
            changes["name"] = name
            siblings = self._lotes.list_for_predio(predio.id)
            if any(other.name == name and other.id != lote.id for other in siblings):
                raise LoteNameConflictError(name)
        if "lote_type" in changes:
            changes["lote_type"] = LoteType(str(changes["lote_type"]))

        warnings: list[GeometryWarning] = []
        geometry_change: dict[str, object] | None = None
        if geojson is not None:
            report = self.inspect_geometry(geojson)
            self._ensure_lote_fits(predio, report.geometry, exclude_lote_id=lote.id)
            self._require_repair_confirmation(
                actor,
                report,
                accept_repair=accept_repair,
                predio_id=predio.id,
                target_resource_type="lote",
                target_resource_id=lote.id,
            )
            geometry_change = {
                "previous": _geometry_summary(
                    wkb_to_geometry(lote.geometry), lote.area_m2
                ),
                "new": _geometry_summary(report.geometry, report.area_m2),
                "area_delta_m2": round(report.area_m2 - lote.area_m2, 2),
            }
            changes.update(
                geometry=geojson_to_wkb(report.geometry),
                centroid=from_shape(report.centroid, srid=get_settings().geo_srid),
                area_m2=report.area_m2,
                **_repair_columns(report),
            )
            warnings = report.warnings
            repaired_report = report if report.repair is not None else None
        else:
            repaired_report = None

        changed_fields = sorted(
            key
            for key, value in changes.items()
            if key in LOTE_FIELDS and getattr(lote, key) != value
        )
        try:
            self._lotes.update(lote, changes)
        except IntegrityError as exc:
            self._session.rollback()
            raise LoteNameConflictError(str(changes.get("name"))) from exc

        if changed_fields:
            self._record(
                actor,
                event_type=events.LOTE_UPDATED,
                action="update lote",
                predio_id=predio.id,
                target_resource_type="lote",
                target_resource_id=lote.id,
                details={"fields": changed_fields},
            )
        if geometry_change is not None:
            self._record(
                actor,
                event_type=events.LOTE_GEOMETRY_CHANGED,
                action="change lote boundary",
                predio_id=predio.id,
                target_resource_type="lote",
                target_resource_id=lote.id,
                details=geometry_change,
            )
        if repaired_report is not None:
            self._record_repair(
                actor,
                repaired_report,
                predio_id=predio.id,
                target_resource_type="lote",
                target_resource_id=lote.id,
            )
        self._session.commit()
        return LoteResult(lote=lote, warnings=warnings)

    def delete_lote(self, actor: Actor, predio_id: uuid.UUID, lote_id: uuid.UUID) -> None:
        """Soft-delete a lote."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_UPDATE)
        lote = self._load_lote(predio.id, lote_id)
        self._lotes.soft_delete(lote, actor.user_id)
        self._record(
            actor,
            event_type=events.LOTE_DELETED,
            action="delete lote",
            predio_id=predio.id,
            target_resource_type="lote",
            target_resource_id=lote.id,
            details={"name": lote.name, "area_m2": round(lote.area_m2, 2)},
        )
        self._session.commit()

    def import_lotes(
        self,
        actor: Actor,
        predio_id: uuid.UUID,
        feature_collection: dict[str, Any],
        *,
        accept_repair: bool = False,
    ) -> list[LoteResult]:
        """Create every lote of a GeoJSON FeatureCollection, or none of them.

        Each feature is checked on its own, against the predio's existing lotes
        and against the other features. Any error rejects the whole import and
        reports every failing feature by index. ``accept_repair`` applies to the
        whole import: without it, a single feature whose repair needs
        confirmation aborts everything.
        """
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_UPDATE)
        features = feature_collection.get("features")
        if feature_collection.get("type") != "FeatureCollection" or not isinstance(
            features, list
        ):
            raise GeoJSONImportError(
                [{"index": None, "error": "not a FeatureCollection"}]
            )
        if not features:
            raise GeoJSONImportError([{"index": None, "error": "no features"}])
        if len(features) > MAX_IMPORT_FEATURES:
            raise GeoJSONImportError(
                [
                    {
                        "index": None,
                        "error": "too many features",
                        "limit": MAX_IMPORT_FEATURES,
                    }
                ]
            )

        existing_names = {lote.name for lote in self._lotes.list_for_predio(predio.id)}
        accepted: list[tuple[int, dict[str, object], GeometryReport]] = []
        errors: list[dict[str, object]] = []

        for index, feature in enumerate(features):
            taken = existing_names | {str(data["name"]) for _, data, _ in accepted}
            try:
                data, report = self._check_feature(
                    actor, predio, feature, taken, accept_repair=accept_repair
                )
            except (
                InvalidGeometryError,
                LoteNotContainedError,
                LoteNameConflictError,
                LotesOverlapError,
                GeometryRepairExceedsThresholdError,
            ) as exc:
                errors.append({"index": index, **exc.as_dict()})
                continue
            except ValueError as exc:
                errors.append(
                    {"index": index, "error": "InvalidFeature", "reason": str(exc)}
                )
                continue

            clash = next(
                (
                    other_index
                    for other_index, _, other in accepted
                    if report.geometry.intersection(other.geometry).area > 0
                ),
                None,
            )
            if clash is not None:
                errors.append(
                    {
                        "index": index,
                        "error": "LotesOverlapError",
                        "overlaps_feature": clash,
                    }
                )
                continue
            accepted.append((index, data, report))

        if errors:
            # Nothing was written except GEOMETRY_REPAIR_REJECTED entries, which
            # are kept on purpose (see _require_repair_confirmation).
            self._session.commit()
            raise GeoJSONImportError(errors)

        results: list[LoteResult] = []
        try:
            for _, data, report in accepted:
                lote = self._new_lote(actor, predio, data, report)
                self._lotes.create(lote)
                results.append(LoteResult(lote=lote, warnings=report.warnings))
        except IntegrityError as exc:
            self._session.rollback()
            raise GeoJSONImportError(
                [{"index": None, "error": "conflicting concurrent change"}]
            ) from exc

        self._record(
            actor,
            event_type=events.LOTES_IMPORTED,
            action="import lotes from GeoJSON",
            predio_id=predio.id,
            target_resource_type="predio",
            target_resource_id=predio.id,
            details={
                "count": len(results),
                "total_area_m2": round(sum(r.lote.area_m2 for r in results), 2),
                "lote_ids": [str(r.lote.id) for r in results],
            },
        )
        for (_, _, report), result in zip(accepted, results, strict=True):
            self._record_repair(
                actor,
                report,
                predio_id=predio.id,
                target_resource_type="lote",
                target_resource_id=result.lote.id,
            )
        self._session.commit()
        return results

    # --- membership -----------------------------------------------------------

    def list_members(
        self, actor: Actor, predio_id: uuid.UUID
    ) -> list[tuple[UserPredioRole, User]]:
        """Return who holds which role on a predio, expired grants excluded."""
        predio = self._load_predio(actor, predio_id, Permission.USER_VIEW)
        return list(self._roles.list_for_predio(predio.id))

    def assign_member(
        self,
        actor: Actor,
        predio_id: uuid.UUID,
        *,
        email: str,
        role: Role,
        expires_at: datetime | None = None,
    ) -> tuple[UserPredioRole, User]:
        """Grant ``role`` on the predio to the account registered as ``email``."""
        predio = self._load_predio(actor, predio_id, Permission.PREDIO_ASSIGN_USERS)
        now = datetime.now(UTC)
        reason = _unassignable_reason(role, expires_at, now)
        if reason is not None:
            raise RoleNotAssignableError(role.value, reason)

        user = self._users.get_by_email(email)
        if user is None or not user.is_active:
            raise MemberNotFoundError

        existing = self._roles.get_predio_grant(user.id, predio.id, role)
        renewed = existing is not None
        try:
            if existing is not None:
                if existing.expires_at is None or existing.expires_at > now:
                    raise AssignmentExistsError(role.value)
                # An expired grant is renewed in place: the triple is unique.
                existing.expires_at = expires_at
                existing.granted_by_user_id = actor.user_id
                existing.granted_at = now
                self._session.flush()
                grant = existing
            else:
                grant = self._roles.grant_predio(
                    UserPredioRole(
                        user_id=user.id,
                        predio_id=predio.id,
                        role=role,
                        granted_by_user_id=actor.user_id,
                        expires_at=expires_at,
                    )
                )
        except IntegrityError as exc:
            # A concurrent request granted the same role first.
            self._session.rollback()
            raise AssignmentExistsError(role.value) from exc

        self._record(
            actor,
            event_type=events.PREDIO_USER_ASSIGNED,
            action="assign user to predio",
            predio_id=predio.id,
            target_resource_type="user",
            target_resource_id=user.id,
            details={
                "user_id": str(user.id),
                "role": role.value,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "renewed": renewed,
            },
        )
        self._session.commit()
        return grant, user

    def unassign_member(
        self, actor: Actor, predio_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None:
        """Revoke ``role`` on the predio from a user.

        The predio row is locked first, so two PROPIETARIOs removing each
        other at the same time cannot both succeed and leave it ownerless.
        The owner's own PROPIETARIO role is never revocable here: a co-owner
        must not be able to lock the owner out of their predio.
        """
        self.authorize(actor, predio_id, Permission.PREDIO_ASSIGN_USERS)
        predio = self._predios.lock(predio_id)
        if predio is None:
            raise PredioNotFoundError
        grant = self._roles.get_predio_grant(user_id, predio.id, role)
        if grant is None:
            raise AssignmentNotFoundError

        live = grant.expires_at is None or grant.expires_at > datetime.now(UTC)
        if (
            role is Role.PROPIETARIO
            and live
            and self._roles.count_live_predio_role(predio.id, Role.PROPIETARIO) <= 1
        ):
            raise LastPropietarioError
        if role is Role.PROPIETARIO and user_id == predio.owner_user_id:
            raise OwnerRoleProtectedError

        self._roles.revoke_predio(grant)
        self._record(
            actor,
            event_type=events.PREDIO_USER_UNASSIGNED,
            action="unassign user from predio",
            predio_id=predio.id,
            target_resource_type="user",
            target_resource_id=user_id,
            details={
                "user_id": str(user_id),
                "role": role.value,
                "self_removal": user_id == actor.user_id,
            },
        )
        self._session.commit()

    def _check_feature(
        self,
        actor: Actor,
        predio: Predio,
        feature: object,
        taken_names: set[str],
        *,
        accept_repair: bool,
    ) -> tuple[dict[str, object], GeometryReport]:
        """Validate one import feature; raise the domain error that rejects it."""
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("not a Feature")
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise ValueError("properties must be an object")
        data = {key: value for key, value in properties.items() if key in LOTE_FIELDS}
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("a lote needs a name")
        if name in taken_names:
            raise LoteNameConflictError(name)
        data["name"] = name
        data["lote_type"] = LoteType(str(data.get("lote_type") or LoteType.OTRO))

        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise InvalidGeometryError("feature has no geometry")
        report = self.inspect_geometry(geometry)
        self._ensure_lote_fits(predio, report.geometry)
        # Committed by import_lotes once every feature has been checked.
        self._require_repair_confirmation(
            actor,
            report,
            accept_repair=accept_repair,
            predio_id=predio.id,
            target_resource_type="lote",
            commit=False,
        )
        return data, report
