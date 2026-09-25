"""Two PROPIETARIOs revoking each other at the same instant, on real connections.

The rest of the suite runs inside one rolled-back transaction per test, where
concurrency cannot happen. These tests commit for real, run two sessions in
two threads, and clean up after themselves (audit rows stay: the log is
append-only by design).

The owner holds no PROPIETARIO grant here on purpose. The owner's own role is
protected separately (OwnerRoleProtectedError), which on its own keeps one
PROPIETARIO in place; the row lock is the defence for when that does not apply,
such as data corrected by hand. This isolates the lock.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from app.modules.predios.exceptions import PredioError
from app.modules.predios.models import Predio
from app.modules.predios.repository import PredioRepository
from app.modules.predios.service import Actor, PredioService
from app.modules.users.models import User, UserPredioRole
from app.modules.users.repository import UserRoleRepository
from app.shared.roles import Role
from tests.fixtures.predios import create_predio

# Long enough that the second session reaches the count while the first one
# is still between counting and committing.
PAUSE_BETWEEN_COUNT_AND_WRITE = 0.5


@dataclass(frozen=True)
class Scenario:
    """Ids of the committed predio and the two co-owners racing on it."""

    predio_id: uuid.UUID
    first: uuid.UUID
    second: uuid.UUID


def _user(session: Session, label: str) -> User:
    user = User(
        email=f"race-{label}-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name=f"Race {label}",
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def scenario(app_engine: Engine, db_engine: Engine) -> Iterator[Scenario]:
    """Commit a predio with two co-PROPIETARIOs and an owner holding no grant."""
    with Session(app_engine) as session:
        owner, first, second = (_user(session, name) for name in ("o", "a", "b"))
        predio = create_predio(session, owner.id)
        for user in (first, second):
            session.add(
                UserPredioRole(
                    user_id=user.id, predio_id=predio.id, role=Role.PROPIETARIO
                )
            )
        session.commit()
        ids = Scenario(predio.id, first.id, second.id)
        user_ids = [owner.id, first.id, second.id]
    try:
        yield ids
    finally:
        with Session(db_engine) as session:
            session.execute(
                delete(UserPredioRole).where(UserPredioRole.predio_id == ids.predio_id)
            )
            session.execute(delete(Predio).where(Predio.id == ids.predio_id))
            session.execute(delete(User).where(User.id.in_(user_ids)))
            session.commit()


@pytest.fixture
def slow_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the window between counting PROPIETARIOs and revoking one."""
    original = UserRoleRepository.count_live_predio_role

    def counted_then_paused(
        self: UserRoleRepository, predio_id: uuid.UUID, role: Role
    ) -> int:
        result = original(self, predio_id, role)
        time.sleep(PAUSE_BETWEEN_COUNT_AND_WRITE)
        return result

    monkeypatch.setattr(UserRoleRepository, "count_live_predio_role", counted_then_paused)


def _revoke_each_other(engine: Engine, scenario: Scenario) -> dict[str, str]:
    """Run both revocations at once; return each actor's outcome."""
    start = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def revoke(
        label: str, actor_id: uuid.UUID, target_id: uuid.UUID
    ) -> Callable[[], None]:
        def run() -> None:
            with Session(engine) as session:
                service = PredioService(session)
                start.wait()
                try:
                    service.unassign_member(
                        Actor(user_id=actor_id),
                        scenario.predio_id,
                        target_id,
                        Role.PROPIETARIO,
                    )
                    outcomes[label] = "revoked"
                except PredioError as exc:
                    outcomes[label] = type(exc).__name__

        return run

    threads = [
        threading.Thread(target=revoke("first", scenario.first, scenario.second)),
        threading.Thread(target=revoke("second", scenario.second, scenario.first)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "a revocation hung"
    return outcomes


def _live_propietarios(engine: Engine, predio_id: uuid.UUID) -> int:
    with Session(engine) as session:
        return int(
            session.execute(
                select(func.count()).where(
                    UserPredioRole.predio_id == predio_id,
                    UserPredioRole.role == Role.PROPIETARIO,
                )
            ).scalar_one()
        )


@pytest.mark.usefixtures("slow_count")
def test_simultaneous_revocations_leave_one_propietario(
    app_engine: Engine, scenario: Scenario
) -> None:
    outcomes = _revoke_each_other(app_engine, scenario)

    assert sorted(outcomes.values()) == ["LastPropietarioError", "revoked"], outcomes
    assert _live_propietarios(app_engine, scenario.predio_id) == 1


@pytest.mark.usefixtures("slow_count")
def test_without_the_lock_the_race_empties_the_predio(
    app_engine: Engine, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the test above can fail: remove the lock and the race wins.

    If this ever stops reproducing the race, the test above proves nothing.
    """

    def unlocked(self: PredioRepository, predio_id: uuid.UUID) -> Predio | None:
        return self.get_by_id(predio_id)

    monkeypatch.setattr(PredioRepository, "lock", unlocked)
    outcomes = _revoke_each_other(app_engine, scenario)

    assert outcomes == {"first": "revoked", "second": "revoked"}
    assert _live_propietarios(app_engine, scenario.predio_id) == 0
