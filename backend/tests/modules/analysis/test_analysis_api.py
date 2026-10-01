"""HTTP surface of satellite analysis, end to end through the runtime role.

The queue is a fake and every completed analysis comes from the synthetic
FakeSentinelClient: nothing here reaches arq's worker or Sentinel Hub.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.analysis.exceptions import QueueUnavailableError
from app.modules.analysis.models import Analysis, Anomaly
from app.modules.analysis.queue import get_analysis_queue
from app.modules.analysis.router import RECOMPUTE_WARNING
from app.modules.analysis.sentinel_client import pu_month_key
from app.modules.analysis.service import AnalysisService
from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.config import Settings, get_settings
from app.shared.enums import AnalysisStatus, AnalysisType, AnomalyReviewStatus, LoteType
from app.shared.roles import Role
from app.shared.security import create_access_token
from app.shared.storage import LocalStorageBackend, get_storage
from tests.fixtures import geometries as g
from tests.fixtures.predios import create_lote, create_predio
from tests.fixtures.sentinel.fake_client import (
    STRESSED_NIR,
    STRESSED_RED,
    FakeSentinelClient,
    Zone,
    corner,
)

HALF = g.STEP / 2
SEPTEMBER = {"date_from": "2026-09-01", "date_to": "2026-09-30"}

Headers = dict[str, str]
MakeClientUser = Callable[..., tuple[User, Headers]]


class FakeQueue:
    """Records what would have been enqueued; can be told to fail."""

    def __init__(self) -> None:
        """Start empty and working."""
        self.enqueued: list[uuid.UUID] = []
        self.fail = False

    async def enqueue(self, analysis_id: uuid.UUID) -> None:
        """Record the id, or fail as an unreachable Redis would."""
        if self.fail:
            raise QueueUnavailableError
        self.enqueued.append(analysis_id)


@pytest.fixture
def settings() -> Settings:
    # Credentials only switch the module on; no client is ever built here.
    return get_settings().model_copy(
        update={
            "sentinel_client_id": SecretStr("sh-api-test"),
            "sentinel_client_secret": SecretStr("api-test-secret-never-used"),
        }
    )


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "rasters")


@pytest.fixture
def client(
    api_client: TestClient,
    settings: Settings,
    queue: FakeQueue,
    storage: LocalStorageBackend,
    redis_client: redis.Redis,
) -> Iterator[TestClient]:
    overrides = api_client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_settings] = lambda: settings
    overrides[get_analysis_queue] = lambda: queue
    overrides[get_storage] = lambda: storage
    # The suite's Redis lives for the whole session: start and end each test
    # with this month's PU counter empty.
    redis_client.delete(pu_month_key())
    yield api_client
    redis_client.delete(pu_month_key())


@pytest.fixture
def make_client_user(app_session: Session) -> MakeClientUser:
    def _make(
        global_roles: tuple[Role, ...] = (),
        predio_roles: tuple[tuple[uuid.UUID, Role], ...] = (),
    ) -> tuple[User, Headers]:
        user = User(
            email=f"analysis-{uuid.uuid4().hex[:8]}@example.cl",
            password_hash="$argon2id$placeholder",
            full_name="Analysis User",
        )
        app_session.add(user)
        app_session.flush()
        for role in global_roles:
            app_session.add(UserRole(user_id=user.id, role=role))
        for predio_id, role in predio_roles:
            app_session.add(
                UserPredioRole(user_id=user.id, predio_id=predio_id, role=role)
            )
        app_session.flush()
        roles = [*global_roles, *(role for _, role in predio_roles)]
        token = create_access_token(
            user.id,
            roles,
            roles[0] if roles else None,
            [predio_id for predio_id, _ in predio_roles],
            two_factor_verified=True,
        )
        return user, {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def owner(make_client_user: MakeClientUser) -> tuple[User, Headers]:
    return make_client_user((Role.PROPIETARIO,))


@pytest.fixture
def predio(app_session: Session, owner: tuple[User, Headers]) -> Predio:
    created = create_predio(app_session, owner[0].id)
    app_session.add(
        UserPredioRole(user_id=owner[0].id, predio_id=created.id, role=Role.PROPIETARIO)
    )
    app_session.flush()
    return created


@pytest.fixture
def cuartel(app_session: Session, predio: Predio) -> Lote:
    return create_lote(app_session, predio, name="Cuartel 1")


@pytest.fixture
def greenhouse(app_session: Session, predio: Predio) -> Lote:
    return create_lote(
        app_session,
        predio,
        geojson=g.square(lon=g.BASE_LON + HALF, size=HALF),
        name="Invernadero 1",
        lote_type=LoteType.INVERNADERO,
    )


@pytest.fixture
def agronomo(make_client_user: MakeClientUser, predio: Predio) -> tuple[User, Headers]:
    return make_client_user(predio_roles=((predio.id, Role.AGRONOMO),))


def _base(predio: Predio) -> str:
    return f"/api/v1/predios/{predio.id}"


def _stressed_corner() -> Zone:
    return Zone(corner(g_shape(), 0.2), red=STRESSED_RED, nir=STRESSED_NIR)


def g_shape() -> Any:
    from shapely.geometry import shape

    return shape(g.square(size=HALF))


async def _completed(
    session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    user: User,
    *,
    force: bool = False,
) -> Analysis:
    """Run a real pipeline on a synthetic scene, as the worker would."""
    service = AnalysisService(session, settings)
    analysis = service.create_pending(
        predio_id=predio.id,
        analysis_type=AnalysisType.NDVI,
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
        requested_by=user.id,
        force=force,
    )
    await service.run(
        analysis.id, FakeSentinelClient(zones=[_stressed_corner()]), storage
    )
    session.expire_all()
    stored = session.get(Analysis, analysis.id)
    assert stored is not None
    return stored


def _events(session: Session, event_type: str) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.event_type == event_type)
    return list(session.execute(statement).scalars())


# --- POST: request ------------------------------------------------------------


def test_a_request_is_queued_and_answered_at_once(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    agronomo: tuple[User, Headers],
    queue: FakeQueue,
) -> None:
    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    assert response.status_code == 202, response.text
    body = response.json()
    analysis_id = uuid.UUID(body["analysis_id"])
    assert body["status"] == "PENDING"
    assert body["poll_url"] == f"{_base(predio)}/analyses/{analysis_id}"
    assert response.headers["location"] == body["poll_url"]
    assert body["warning"] is None
    assert queue.enqueued == [analysis_id]
    stored = app_session.get(Analysis, analysis_id)
    assert stored is not None and stored.status is AnalysisStatus.PENDING


def test_missing_dates_default_to_the_last_month(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    agronomo: tuple[User, Headers],
    settings: Settings,
) -> None:
    response = client.post(f"{_base(predio)}/analyses", json={}, headers=agronomo[1])

    assert response.status_code == 202, response.text
    stored = app_session.get(Analysis, uuid.UUID(response.json()["analysis_id"]))
    assert stored is not None
    span = (stored.requested_date_to - stored.requested_date_from).days
    assert span == settings.analysis_default_date_range_days


def test_a_request_never_runs_the_analysis_in_the_request(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    stored = app_session.get(Analysis, uuid.UUID(response.json()["analysis_id"]))
    assert stored is not None
    assert stored.started_at is None and stored.scene_id is None


def test_a_request_while_one_is_in_flight_gets_409_with_its_id(
    client: TestClient, predio: Predio, agronomo: tuple[User, Headers]
) -> None:
    first = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])
    # No pre-check exists: this second insert hits the in-flight unique index,
    # exactly as the loser of two concurrent requests does.
    second = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])

    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["error"] == "AnalysisAlreadyActiveError"
    assert detail["analysis_id"] == first.json()["analysis_id"]


@pytest.mark.asyncio
async def test_the_in_flight_slot_frees_up_when_the_analysis_finishes(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    first = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])
    analysis_id = uuid.UUID(first.json()["analysis_id"])
    await AnalysisService(app_session, settings).run(
        analysis_id, FakeSentinelClient(), storage
    )

    again = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])

    assert again.status_code == 202, again.text


def test_a_failed_enqueue_answers_503_and_does_not_hold_the_slot(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    agronomo: tuple[User, Headers],
    queue: FakeQueue,
) -> None:
    queue.fail = True
    failed = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])

    assert failed.status_code == 503
    assert failed.json()["detail"]["error"] == "QueueUnavailableError"
    stored = app_session.execute(
        select(Analysis).where(Analysis.predio_id == predio.id)
    ).scalar_one()
    assert (stored.status, stored.error_code) == (
        AnalysisStatus.FAILED,
        "QUEUE_UNAVAILABLE",
    )
    queue.fail = False
    retry = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])
    assert retry.status_code == 202


def test_without_credentials_analysis_answers_503(
    client: TestClient,
    predio: Predio,
    agronomo: tuple[User, Headers],
    queue: FakeQueue,
) -> None:
    unconfigured = get_settings()  # the suite blanks the Sentinel variables
    client.app.dependency_overrides[get_settings] = lambda: unconfigured  # type: ignore[attr-defined]

    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "AnalysisUnavailableError"
    assert queue.enqueued == []


def test_a_spent_budget_answers_429_before_queueing(
    client: TestClient,
    predio: Predio,
    agronomo: tuple[User, Headers],
    queue: FakeQueue,
    settings: Settings,
    redis_client: redis.Redis,
) -> None:
    redis_client.set(pu_month_key(), settings.sentinel_monthly_pu_budget)

    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "SentinelQuotaExceededError"
    assert queue.enqueued == []


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "anterior"),
        ({"date_from": "2024-01-01", "date_to": "2026-09-01"}, "superar"),
        (
            {"date_to": (datetime.now(UTC).date() + timedelta(days=2)).isoformat()},
            "futuro",
        ),
    ],
)
def test_an_unusable_range_is_refused_before_queueing(
    client: TestClient,
    predio: Predio,
    agronomo: tuple[User, Headers],
    queue: FakeQueue,
    payload: dict[str, str],
    fragment: str,
) -> None:
    response = client.post(f"{_base(predio)}/analyses", json=payload, headers=agronomo[1])

    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]["message"]
    assert queue.enqueued == []


def test_a_predio_too_large_for_one_analysis_is_refused(
    client: TestClient,
    predio: Predio,
    agronomo: tuple[User, Headers],
    settings: Settings,
) -> None:
    small_limit = settings.model_copy(update={"analysis_max_predio_area_m2": 1.0})
    client.app.dependency_overrides[get_settings] = lambda: small_limit  # type: ignore[attr-defined]

    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "PredioTooLargeError"


def test_unknown_fields_are_refused(
    client: TestClient, predio: Predio, agronomo: tuple[User, Headers]
) -> None:
    response = client.post(
        f"{_base(predio)}/analyses",
        json={**SEPTEMBER, "scene_id": "S2A_chosen_by_the_client"},
        headers=agronomo[1],
    )

    assert response.status_code == 422


# --- POST: force --------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_over_reviewed_anomalies_warns_with_their_count(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    existing = await _completed(app_session, settings, storage, predio, agronomo[0])
    anomalies = list(
        app_session.execute(
            select(Anomaly).where(Anomaly.analysis_id == existing.id)
        ).scalars()
    )
    assert anomalies
    for anomaly in anomalies[:2]:
        client.patch(
            f"{_base(predio)}/anomalies/{anomaly.id}/review",
            json={"status": "CONFIRMED"},
            headers=agronomo[1],
        )
    reviewed = min(2, len(anomalies))

    response = client.post(
        f"{_base(predio)}/analyses",
        json={**SEPTEMBER, "force": True},
        headers=agronomo[1],
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["warning"] == RECOMPUTE_WARNING.format(count=reviewed)
    assert body["supersede_candidate"] == {
        "analysis_id": str(existing.id),
        "reviewed_anomalies": reviewed,
    }


@pytest.mark.asyncio
async def test_force_without_reviews_names_the_candidate_but_does_not_warn(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    existing = await _completed(app_session, settings, storage, predio, agronomo[0])

    response = client.post(
        f"{_base(predio)}/analyses",
        json={**SEPTEMBER, "force": True},
        headers=agronomo[1],
    )

    body = response.json()
    assert body["warning"] is None
    assert body["supersede_candidate"]["analysis_id"] == str(existing.id)


@pytest.mark.asyncio
async def test_a_plain_request_over_an_analysed_scene_is_still_accepted(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    # No pre-check in the POST: the worker resolves the duplicate later.
    await _completed(app_session, settings, storage, predio, agronomo[0])

    response = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )

    assert response.status_code == 202
    assert response.json()["supersede_candidate"] is None


# --- authorization ------------------------------------------------------------


def test_the_owner_cannot_request_until_the_permission_is_decided(
    client: TestClient, predio: Predio, owner: tuple[User, Headers]
) -> None:
    # PROPIETARIO holds ANALYSIS_VIEW only; requesting needs ANALYSIS_REPORT.
    response = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=owner[1])

    assert response.status_code == 403


def test_a_global_auditor_can_read_but_never_request(
    client: TestClient,
    predio: Predio,
    make_client_user: MakeClientUser,
    queue: FakeQueue,
) -> None:
    _, auditor = make_client_user((Role.AUDITOR,))

    listing = client.get(f"{_base(predio)}/analyses", headers=auditor)
    request = client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=auditor)

    assert listing.status_code == 200
    assert request.status_code == 403
    assert queue.enqueued == []


def test_a_user_of_another_predio_sees_nothing(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    make_client_user: MakeClientUser,
) -> None:
    other_owner, _ = make_client_user((Role.PROPIETARIO,))
    other = create_predio(app_session, other_owner.id, slug="otro-predio")
    _, outsider = make_client_user(predio_roles=((other.id, Role.AGRONOMO),))

    for response in (
        client.get(f"{_base(predio)}/analyses", headers=outsider),
        client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=outsider),
        client.get(f"{_base(predio)}/analyses/{uuid.uuid4()}", headers=outsider),
    ):
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_analysis_is_not_reachable_through_another_predio(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
    make_client_user: MakeClientUser,
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    other = create_predio(app_session, agronomo[0].id, slug="otro")
    _, both = make_client_user(
        predio_roles=((predio.id, Role.AGRONOMO), (other.id, Role.AGRONOMO))
    )
    elsewhere = f"/api/v1/predios/{other.id}/analyses/{analysis.id}"

    for suffix in ("", "/raster", "/preview", "/anomalies"):
        assert client.get(elsewhere + suffix, headers=both).status_code == 404


# --- reads --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_is_paginated_and_carries_no_geometry(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    await _completed(app_session, settings, storage, predio, agronomo[0])
    client.post(f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1])

    response = client.get(
        f"{_base(predio)}/analyses",
        params={"page_size": 1},
        headers=agronomo[1],
    )
    completed_only = client.get(
        f"{_base(predio)}/analyses", params={"status": "COMPLETED"}, headers=agronomo[1]
    )

    body = response.json()
    assert (body["total"], body["pages"], len(body["items"])) == (2, 2, 1)
    assert body["items"][0]["status"] == "PENDING"  # newest first
    assert "coordinates" not in response.text
    assert [item["status"] for item in completed_only.json()["items"]] == ["COMPLETED"]


@pytest.mark.asyncio
async def test_the_detail_shows_lote_rows_including_excluded_ones(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    greenhouse: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])

    response = client.get(f"{_base(predio)}/analyses/{analysis.id}", headers=agronomo[1])

    assert response.status_code == 200
    body = response.json()
    rows = {row["lote_id"]: row for row in body["lote_stats"]}
    assert rows[str(cuartel.id)]["mean"] is not None
    assert rows[str(greenhouse.id)]["excluded_reason"] == "NOT_APPLICABLE_GREENHOUSE"
    assert rows[str(greenhouse.id)]["mean"] is None
    assert set(body["anomaly_counts"]) == {"LOW", "MEDIUM", "HIGH"}
    assert sum(body["anomaly_counts"].values()) >= 1
    assert body["raster_url"] == f"{_base(predio)}/analyses/{analysis.id}/raster"
    assert body["preview_url"] == f"{_base(predio)}/analyses/{analysis.id}/preview"
    assert "coordinates" not in response.text


@pytest.mark.asyncio
async def test_the_detail_links_duplicates_and_superseded_analyses(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    first = await _completed(app_session, settings, storage, predio, agronomo[0])
    duplicate = await _completed(app_session, settings, storage, predio, agronomo[0])
    forced = await _completed(
        app_session, settings, storage, predio, agronomo[0], force=True
    )

    dup = client.get(f"{_base(predio)}/analyses/{duplicate.id}", headers=agronomo[1])
    old = client.get(f"{_base(predio)}/analyses/{first.id}", headers=agronomo[1])

    assert dup.json()["status"] == "DUPLICATE_SCENE"
    assert dup.json()["resolved_to_analysis_id"] == str(first.id)
    assert dup.json()["raster_url"] is None
    assert old.json()["superseded_by_analysis_id"] == str(forced.id)
    assert old.json()["raster_url"] is not None  # superseded, still served


def test_an_unknown_analysis_is_404(
    client: TestClient, predio: Predio, agronomo: tuple[User, Headers]
) -> None:
    response = client.get(f"{_base(predio)}/analyses/{uuid.uuid4()}", headers=agronomo[1])

    assert response.status_code == 404


# --- raster and preview -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_raster_streams_and_its_download_is_audited(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    assert analysis.raster_key is not None

    response = client.get(
        f"{_base(predio)}/analyses/{analysis.id}/raster", headers=agronomo[1]
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/tiff"
    assert response.content == await storage.get(analysis.raster_key)
    assert f"ndvi-{analysis.id}.tif" in response.headers["content-disposition"]
    downloads = _events(app_session, events.RASTER_DOWNLOADED)
    assert len(downloads) == 1
    assert downloads[0].actor_user_id == agronomo[0].id
    assert downloads[0].details == {"artifact": "ndvi.tif"}


def test_a_pending_analysis_has_no_raster(
    client: TestClient, predio: Predio, agronomo: tuple[User, Headers]
) -> None:
    created = client.post(
        f"{_base(predio)}/analyses", json=SEPTEMBER, headers=agronomo[1]
    )
    analysis_id = created.json()["analysis_id"]

    response = client.get(
        f"{_base(predio)}/analyses/{analysis_id}/raster", headers=agronomo[1]
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "ArtifactNotAvailableError"


@pytest.mark.asyncio
async def test_a_duplicate_points_to_where_the_raster_is(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    first = await _completed(app_session, settings, storage, predio, agronomo[0])
    duplicate = await _completed(app_session, settings, storage, predio, agronomo[0])

    response = client.get(
        f"{_base(predio)}/analyses/{duplicate.id}/raster", headers=agronomo[1]
    )

    assert response.status_code == 404
    assert response.json()["detail"]["resolved_to_analysis_id"] == str(first.id)


@pytest.mark.asyncio
async def test_a_raster_lost_from_storage_is_404_not_500(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    assert analysis.raster_key is not None
    await storage.delete(analysis.raster_key)

    response = client.get(
        f"{_base(predio)}/analyses/{analysis.id}/raster", headers=agronomo[1]
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_preview_is_cacheable_with_an_etag(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    url = f"{_base(predio)}/analyses/{analysis.id}/preview"

    first = client.get(url, headers=agronomo[1])
    again = client.get(
        url, headers={**agronomo[1], "If-None-Match": first.headers["etag"]}
    )

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.content.startswith(b"\x89PNG")
    assert "max-age" in first.headers["cache-control"]
    assert again.status_code == 304
    assert again.content == b""


# --- anomalies and reviews ----------------------------------------------------


@pytest.mark.asyncio
async def test_anomalies_come_as_a_feature_collection(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])

    response = client.get(
        f"{_base(predio)}/analyses/{analysis.id}/anomalies", headers=agronomo[1]
    )
    high = client.get(
        f"{_base(predio)}/analyses/{analysis.id}/anomalies",
        params={"severity": "HIGH"},
        headers=agronomo[1],
    )

    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"]
    feature = body["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["lote_id"] == str(cuartel.id)
    assert feature["properties"]["review_status"] is None
    assert all(f["properties"]["severity"] == "HIGH" for f in high.json()["features"])


@pytest.mark.asyncio
async def test_an_agronomist_reviews_an_anomaly_and_it_is_audited(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    anomaly = app_session.execute(
        select(Anomaly).where(Anomaly.analysis_id == analysis.id).limit(1)
    ).scalar_one()

    response = client.patch(
        f"{_base(predio)}/anomalies/{anomaly.id}/review",
        json={"status": "NEEDS_FIELD_CHECK", "notes": "Revisar goteros"},
        headers=agronomo[1],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "NEEDS_FIELD_CHECK"
    assert body["reviewed_by_user_id"] == str(agronomo[0].id)
    assert "geometry" not in body
    app_session.expire_all()
    stored = app_session.get(Anomaly, anomaly.id)
    assert stored is not None
    assert stored.review_status is AnomalyReviewStatus.NEEDS_FIELD_CHECK
    reviews = _events(app_session, events.ANOMALY_REVIEWED)
    assert len(reviews) == 1
    assert reviews[0].actor_user_id == agronomo[0].id
    assert reviews[0].details is not None
    assert reviews[0].details["review_status"] == "NEEDS_FIELD_CHECK"
    assert "Revisar goteros" not in str(reviews[0].details)  # notes stay off the log


@pytest.mark.asyncio
async def test_reviewing_needs_the_report_permission(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    owner: tuple[User, Headers],
    agronomo: tuple[User, Headers],
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    anomaly = app_session.execute(
        select(Anomaly).where(Anomaly.analysis_id == analysis.id).limit(1)
    ).scalar_one()

    response = client.patch(
        f"{_base(predio)}/anomalies/{anomaly.id}/review",
        json={"status": "DISMISSED"},
        headers=owner[1],
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_anomaly_of_another_predio_cannot_be_reviewed_through_this_one(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
    make_client_user: MakeClientUser,
) -> None:
    analysis = await _completed(app_session, settings, storage, predio, agronomo[0])
    anomaly = app_session.execute(
        select(Anomaly).where(Anomaly.analysis_id == analysis.id).limit(1)
    ).scalar_one()
    other = create_predio(app_session, agronomo[0].id, slug="ajeno")
    _, other_agronomo = make_client_user(predio_roles=((other.id, Role.AGRONOMO),))

    response = client.patch(
        f"/api/v1/predios/{other.id}/anomalies/{anomaly.id}/review",
        json={"status": "DISMISSED"},
        headers=other_agronomo,
    )

    assert response.status_code == 404


def test_a_review_with_an_unknown_status_is_refused(
    client: TestClient, predio: Predio, agronomo: tuple[User, Headers]
) -> None:
    response = client.patch(
        f"{_base(predio)}/anomalies/{uuid.uuid4()}/review",
        json={"status": "MAYBE"},
        headers=agronomo[1],
    )

    assert response.status_code == 422


# --- lote history -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_short_history_is_flagged_and_superseded_runs_count_once(
    client: TestClient,
    app_session: Session,
    settings: Settings,
    storage: LocalStorageBackend,
    predio: Predio,
    cuartel: Lote,
    agronomo: tuple[User, Headers],
) -> None:
    await _completed(app_session, settings, storage, predio, agronomo[0])
    forced = await _completed(
        app_session, settings, storage, predio, agronomo[0], force=True
    )

    response = client.get(
        f"{_base(predio)}/lotes/{cuartel.id}/history", headers=agronomo[1]
    )

    assert response.status_code == 200
    body = response.json()
    assert [point["analysis_id"] for point in body["points"]] == [str(forced.id)]
    assert body["insufficient_history"] is True
    assert body["message"]


def test_the_history_of_a_lote_of_another_predio_is_404(
    client: TestClient,
    app_session: Session,
    predio: Predio,
    agronomo: tuple[User, Headers],
) -> None:
    other = create_predio(app_session, agronomo[0].id, slug="vecino")
    foreign = create_lote(app_session, other, name="Cuartel ajeno")

    response = client.get(
        f"{_base(predio)}/lotes/{foreign.id}/history", headers=agronomo[1]
    )

    assert response.status_code == 404


# --- operations ---------------------------------------------------------------


def test_the_auditor_sees_quota_usage(
    client: TestClient,
    make_client_user: MakeClientUser,
    redis_client: redis.Redis,
    settings: Settings,
) -> None:
    redis_client.set(pu_month_key(), 123.5)
    _, auditor = make_client_user((Role.AUDITOR,))

    response = client.get("/api/v1/analysis/status", headers=auditor)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["processing_units"]["spent"] == 123.5
    assert body["processing_units"]["budget"] == settings.sentinel_monthly_pu_budget


def test_quota_usage_is_not_public(
    client: TestClient, owner: tuple[User, Headers]
) -> None:
    assert client.get("/api/v1/analysis/status").status_code == 401
    assert client.get("/api/v1/analysis/status", headers=owner[1]).status_code == 403
