"""HTTP surface of the analysis context.

Every route under ``/predios/{predio_id}`` declares ``require_predio_access``
(invariant 3, enforced by tests/architecture). Domain errors are translated
to HTTP here and nowhere else. Requesting an analysis only queues it: the
work happens in the arq worker, never inside the request.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.modules.analysis.availability import (
    analysis_available,
    ensure_analysis_available,
)
from app.modules.analysis.exceptions import (
    AnalysisAlreadyActiveError,
    AnalysisError,
    AnalysisNotFoundError,
    AnalysisUnavailableError,
    AnomalyNotFoundError,
    ArtifactNotAvailableError,
    InvalidDateRangeError,
    PredioTooLargeError,
    QueueUnavailableError,
)
from app.modules.analysis.models import Analysis, Anomaly
from app.modules.analysis.queue import AnalysisQueue, get_analysis_queue
from app.modules.analysis.repository import AnalysisFilters
from app.modules.analysis.schema import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisListItem,
    AnalysisPage,
    AnalysisRead,
    AnalysisStatusRead,
    AnomalyFeature,
    AnomalyFeatureCollection,
    AnomalyRead,
    AnomalyReviewUpdate,
    LoteHistoryPointRead,
    LoteHistoryRead,
    LoteStatsRead,
    PuUsageRead,
    SupersedeCandidate,
)
from app.modules.analysis.sentinel_client import (
    SentinelQuotaExceededError,
    read_pu_usage,
)
from app.modules.analysis.service import AnalysisService
from app.modules.predios.exceptions import (
    LoteNotFoundError,
    PredioError,
    PredioNotFoundError,
)
from app.modules.predios.service import Actor
from app.shared.cache import get_redis
from app.shared.config import Settings, get_settings
from app.shared.db import get_session
from app.shared.dependencies import (
    CurrentUser,
    client_ip,
    get_current_user,
    require_permission,
    require_predio_access,
    user_agent,
)
from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalySeverity,
)
from app.shared.exceptions import DomainError
from app.shared.geo import geometry_to_geojson, wkb_to_geometry
from app.shared.pagination import DEFAULT_PAGE_SIZE, PageParams
from app.shared.permissions import Permission
from app.shared.storage import StorageBackend, api_path_for, get_storage

router = APIRouter(tags=["analysis"])

API_PREFIX = "/api/v1"
MIN_HISTORY_POINTS = 3
RECOMPUTE_WARNING = (
    "El análisis existente tiene {count} anomalías revisadas. Recomputar genera "
    "anomalías nuevas sin revisar; las revisiones quedan asociadas al análisis "
    "anterior, que se conserva."
)
INSUFFICIENT_HISTORY_MESSAGE = (
    "Hay menos de 3 fechas medidas: todavía no alcanza para leer una tendencia."
)
PREVIEW_CACHE_CONTROL = "private, max-age=86400, immutable"

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageBackend, Depends(get_storage)]
QueueDep = Annotated[AnalysisQueue, Depends(get_analysis_queue)]

_VIEW = [Depends(require_predio_access(Permission.ANALYSIS_VIEW))]
# Requesting an analysis writes and spends quota. ANALYSIS_VIEW is a read
# permission held by the global-scope AUDITOR, so it cannot guard this: until
# the founder decides who may request analyses, only ANALYSIS_REPORT does.
_REQUEST = [Depends(require_predio_access(Permission.ANALYSIS_REPORT))]
_REPORT = [Depends(require_predio_access(Permission.ANALYSIS_REPORT))]

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    AnalysisUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    QueueUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    SentinelQuotaExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    AnalysisAlreadyActiveError: status.HTTP_409_CONFLICT,
    InvalidDateRangeError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PredioTooLargeError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    AnalysisNotFoundError: status.HTTP_404_NOT_FOUND,
    AnomalyNotFoundError: status.HTTP_404_NOT_FOUND,
    ArtifactNotAvailableError: status.HTTP_404_NOT_FOUND,
    PredioNotFoundError: status.HTTP_404_NOT_FOUND,
    LoteNotFoundError: status.HTTP_404_NOT_FOUND,
}


@contextmanager
def _domain_errors() -> Iterator[None]:
    """Translate the service's domain errors into HTTP responses."""
    try:
        yield
    except (AnalysisError, PredioError) as exc:
        code = _STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=code, detail=exc.as_dict()) from exc


def get_actor(
    request: Request, user: Annotated[CurrentUser, Depends(get_current_user)]
) -> Actor:
    """Build the acting identity for the audit trail."""
    request.state.user_id = user.id
    return Actor(
        user_id=user.id,
        role=user.active_role,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )


ActorDep = Annotated[Actor, Depends(get_actor)]


async def _call[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking (database) call off the event loop."""
    return await run_in_threadpool(function, *args, **kwargs)


# --- presentation -------------------------------------------------------------


def _analysis_path(analysis: Analysis) -> str:
    return f"{API_PREFIX}/predios/{analysis.predio_id}/analyses/{analysis.id}"


def _is_served(analysis: Analysis) -> bool:
    return analysis.status is AnalysisStatus.COMPLETED


def _anomaly_read(anomaly: Anomaly) -> AnomalyRead:
    return AnomalyRead.model_validate(anomaly)


# --- request ------------------------------------------------------------------


@router.post(
    "/predios/{predio_id}/analyses",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalysisAccepted,
    dependencies=_REQUEST,
)
async def request_analysis(
    predio_id: uuid.UUID,
    body: AnalysisCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
    queue: QueueDep,
) -> AnalysisAccepted:
    """Queue an analysis of the predio and return at once (202).

    Whether the scene was already analysed is only known once the worker
    picks it; a duplicate then resolves to the existing result at no
    download cost. With ``force`` it is recomputed instead, and the answer
    warns when that leaves reviewed anomalies on the analysis it replaces.
    """
    service = AnalysisService(session, settings)
    with _domain_errors():
        ensure_analysis_available(settings)
        date_from, date_to = await _call(
            service.validate_request,
            predio_id,
            body.date_from,
            body.date_to,
            datetime.now(UTC).date(),
        )
        try:
            usage = await _call(read_pu_usage, get_redis(), settings)
        except RedisError as exc:
            raise QueueUnavailableError from exc
        service.check_budget(usage)

        risk = None
        if body.force:
            risk = await _call(
                service.recompute_risk, predio_id, body.analysis_type, date_from, date_to
            )
        analysis = await _call(
            service.create_pending,
            predio_id=predio_id,
            analysis_type=body.analysis_type,
            date_from=date_from,
            date_to=date_to,
            requested_by=actor.user_id,
            force=body.force,
            actor=actor,
        )
        try:
            await queue.enqueue(analysis.id)
        except QueueUnavailableError as exc:
            # Never leave a PENDING nobody will run: it would hold the
            # predio's one-in-flight slot until the sweeper expires it.
            await _call(
                service.fail_pending, analysis.id, "QUEUE_UNAVAILABLE", exc.as_dict()
            )
            raise

    poll_url = _analysis_path(analysis)
    response.headers["Location"] = poll_url
    warning = None
    if risk is not None and risk.reviewed_anomalies > 0:
        warning = RECOMPUTE_WARNING.format(count=risk.reviewed_anomalies)
    return AnalysisAccepted(
        analysis_id=analysis.id,
        status=AnalysisStatus.PENDING,
        poll_url=poll_url,
        warning=warning,
        supersede_candidate=(
            SupersedeCandidate(
                analysis_id=risk.analysis_id,
                reviewed_anomalies=risk.reviewed_anomalies,
            )
            if risk is not None
            else None
        ),
    )


# --- reads --------------------------------------------------------------------


@router.get(
    "/predios/{predio_id}/analyses",
    response_model=AnalysisPage,
    dependencies=_VIEW,
)
def list_analyses(
    predio_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    status_filter: Annotated[AnalysisStatus | None, Query(alias="status")] = None,
    analysis_type: AnalysisType | None = None,
    scene_date_from: date | None = None,
    scene_date_to: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1)] = DEFAULT_PAGE_SIZE,
) -> AnalysisPage:
    """List the predio's analyses, newest first. No geometries."""
    filters = AnalysisFilters(
        status=status_filter,
        analysis_type=analysis_type,
        scene_date_from=scene_date_from,
        scene_date_to=scene_date_to,
    )
    result = AnalysisService(session, settings).list_analyses(
        predio_id, filters, PageParams(page=page, page_size=page_size)
    )
    return AnalysisPage(
        items=[AnalysisListItem.model_validate(item) for item in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        pages=result.pages,
    )


@router.get(
    "/predios/{predio_id}/analyses/{analysis_id}",
    response_model=AnalysisRead,
    dependencies=_VIEW,
)
def read_analysis(
    predio_id: uuid.UUID,
    analysis_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
) -> AnalysisRead:
    """Return one analysis: outcome, per-lote rows (excluded ones too), counts."""
    with _domain_errors():
        detail = AnalysisService(session, settings).detail(predio_id, analysis_id)
    analysis = detail.analysis
    served = _is_served(analysis)
    return AnalysisRead(
        **AnalysisListItem.model_validate(analysis).model_dump(),
        scene_id=analysis.scene_id,
        started_at=analysis.started_at,
        error_detail=analysis.error_detail,
        raster_url=(
            api_path_for(analysis.raster_key) if served and analysis.raster_key else None
        ),
        preview_url=(
            api_path_for(analysis.preview_key)
            if served and analysis.preview_key
            else None
        ),
        anomalies_url=f"{_analysis_path(analysis)}/anomalies" if served else None,
        anomaly_counts={
            severity: detail.anomaly_counts.get(severity, 0)
            for severity in AnomalySeverity
        },
        lote_stats=[LoteStatsRead.model_validate(row) for row in detail.lote_stats],
    )


async def _artifact(
    service: AnalysisService,
    storage: StorageBackend,
    predio_id: uuid.UUID,
    analysis_id: uuid.UUID,
    attribute: str,
) -> tuple[Analysis, str]:
    analysis = await _call(service.get, predio_id, analysis_id)
    key = getattr(analysis, attribute)
    if not _is_served(analysis) or key is None:
        raise ArtifactNotAvailableError(analysis.resolved_to_analysis_id)
    if not await storage.exists(key):
        # The row says it exists and storage disagrees: a lost file, not a
        # client error, but there is still nothing to serve.
        raise ArtifactNotAvailableError(None)
    return analysis, key


@router.get("/predios/{predio_id}/analyses/{analysis_id}/raster", dependencies=_VIEW)
async def download_raster(
    predio_id: uuid.UUID,
    analysis_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    actor: ActorDep,
) -> StreamingResponse:
    """Stream the NDVI GeoTIFF; never loaded whole into memory."""
    service = AnalysisService(session, settings)
    with _domain_errors():
        analysis, key = await _artifact(
            service, storage, predio_id, analysis_id, "raster_key"
        )
    await _call(service.record_raster_download, analysis, "ndvi.tif", actor)
    return StreamingResponse(
        storage.get_stream(key),
        media_type="image/tiff",
        headers={"Content-Disposition": f'attachment; filename="ndvi-{analysis.id}.tif"'},
    )


@router.get(
    "/predios/{predio_id}/analyses/{analysis_id}/preview",
    dependencies=_VIEW,
    response_model=None,
)
async def read_preview(
    predio_id: uuid.UUID,
    analysis_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> Response:
    """Serve the coloured PNG preview, cacheable: an analysis never changes."""
    service = AnalysisService(session, settings)
    with _domain_errors():
        analysis, key = await _artifact(
            service, storage, predio_id, analysis_id, "preview_key"
        )
    # Artifacts are written once per analysis id, so the id is a strong ETag.
    etag = f'"{analysis.id}"'
    headers = {"ETag": etag, "Cache-Control": PREVIEW_CACHE_CONTROL}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return StreamingResponse(
        storage.get_stream(key), media_type="image/png", headers=headers
    )


@router.get(
    "/predios/{predio_id}/analyses/{analysis_id}/anomalies",
    response_model=AnomalyFeatureCollection,
    dependencies=_VIEW,
)
def read_anomalies(
    predio_id: uuid.UUID,
    analysis_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    severity: AnomalySeverity | None = None,
) -> AnomalyFeatureCollection:
    """Return the analysis's anomalies as a GeoJSON FeatureCollection."""
    with _domain_errors():
        anomalies = AnalysisService(session, settings).anomalies(
            predio_id, analysis_id, severity
        )
    return AnomalyFeatureCollection(
        features=[
            AnomalyFeature(
                id=anomaly.id,
                geometry=geometry_to_geojson(wkb_to_geometry(anomaly.geometry)),
                properties=_anomaly_read(anomaly),
            )
            for anomaly in anomalies
        ]
    )


@router.patch(
    "/predios/{predio_id}/anomalies/{anomaly_id}/review",
    response_model=AnomalyRead,
    dependencies=_REPORT,
)
def review_anomaly(
    predio_id: uuid.UUID,
    anomaly_id: uuid.UUID,
    body: AnomalyReviewUpdate,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> AnomalyRead:
    """Record an agronomist's verdict on an anomaly (audited)."""
    with _domain_errors():
        anomaly = AnalysisService(session, settings).review_anomaly(
            predio_id, anomaly_id, body.status, body.notes, actor
        )
    return _anomaly_read(anomaly)


@router.get(
    "/predios/{predio_id}/lotes/{lote_id}/history",
    response_model=LoteHistoryRead,
    dependencies=_VIEW,
)
def read_lote_history(
    predio_id: uuid.UUID,
    lote_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    analysis_type: AnalysisType = AnalysisType.NDVI,
    date_from: date | None = None,
    date_to: date | None = None,
) -> LoteHistoryRead:
    """Return a lote's index over time; flags when there are too few dates."""
    with _domain_errors():
        points = AnalysisService(session, settings).lote_history(
            predio_id, lote_id, analysis_type, date_from, date_to
        )
    insufficient = len(points) < MIN_HISTORY_POINTS
    return LoteHistoryRead(
        lote_id=lote_id,
        analysis_type=analysis_type,
        points=[LoteHistoryPointRead.model_validate(point) for point in points],
        insufficient_history=insufficient,
        message=INSUFFICIENT_HISTORY_MESSAGE if insufficient else None,
    )


# --- operations ---------------------------------------------------------------


@router.get(
    "/analysis/status",
    response_model=AnalysisStatusRead,
    dependencies=[Depends(require_permission(Permission.AUDIT_VIEW_ALL))],
)
def read_analysis_status(settings: SettingsDep) -> AnalysisStatusRead:
    """Report availability and this month's PU spending (not public /health)."""
    available = analysis_available(settings)
    try:
        usage = read_pu_usage(get_redis(), settings)
    except RedisError:
        return AnalysisStatusRead(available=available, processing_units=None)
    return AnalysisStatusRead(
        available=available,
        processing_units=PuUsageRead(
            month=usage.month,
            spent=round(usage.spent, 4),
            budget=usage.budget,
            remaining=round(usage.remaining, 4),
        ),
    )
