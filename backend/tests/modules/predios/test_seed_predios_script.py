"""The development predios seed: safe to run, correct data, idempotent."""

from __future__ import annotations

import importlib.util
import sys
from itertools import combinations
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from shapely.geometry import shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.geo import is_within_chile_bbox, validate_geojson_geometry
from app.shared.roles import Role

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed_dev_predios.py"


@pytest.fixture(scope="module")
def seeder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_dev_predios", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings_stub(environment: str) -> Any:
    class _Stub:
        def __init__(self) -> None:
            self.environment = environment

    return lambda: _Stub()


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_it_refuses_to_run_outside_development(
    seeder: ModuleType, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setattr(seeder, "get_settings", _settings_stub(environment))
    assert seeder.main([]) == 1


# --- the data itself, without a database ----------------------------------------


def test_every_name_is_marked_as_a_demo(seeder: ModuleType) -> None:
    # The places are real; the predios are not, and must not look real.
    assert all(spec.name.endswith("(demo)") for spec in seeder.PREDIOS)
    assert all(spec.slug.endswith("-demo") for spec in seeder.PREDIOS)


def test_every_geometry_is_valid_and_in_chile(seeder: ModuleType) -> None:
    for spec in seeder.PREDIOS:
        geometry = validate_geojson_geometry(seeder.to_geojson(spec.anchor, spec.ring))
        assert shape(seeder.to_geojson(spec.anchor, spec.ring)).is_valid, spec.name
        assert is_within_chile_bbox(geometry), spec.name


def test_lotes_fit_their_predio_and_do_not_overlap(seeder: ModuleType) -> None:
    for spec in seeder.PREDIOS:
        predio = shape(seeder.to_geojson(spec.anchor, spec.ring))
        lotes = {
            lote.name: shape(seeder.to_geojson(spec.anchor, lote.ring))
            for lote in spec.lotes
        }
        for name, lote in lotes.items():
            assert lote.within(predio), f"{spec.name}: {name}"
        for (a, geom_a), (b, geom_b) in combinations(lotes.items(), 2):
            assert geom_a.intersection(geom_b).area == 0, f"{a} / {b}"


def test_the_overlap_case_is_present(seeder: ModuleType) -> None:
    geometries = {
        spec.slug: shape(seeder.to_geojson(spec.anchor, spec.ring))
        for spec in seeder.PREDIOS
    }
    shared = geometries["vina-los-aromos-demo"].intersection(
        geometries["vina-los-aromos-norte-demo"]
    )
    assert shared.area > 0


def test_it_covers_both_valleys(seeder: ModuleType) -> None:
    assert {spec.comuna for spec in seeder.PREDIOS} >= {"Santa Cruz", "Pirque"}


# --- against the database -----------------------------------------------------------


def _dev_user(session: Session, email: str, role: Role) -> User:
    user = User(email=email, password_hash="$argon2id$placeholder", full_name=email)
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role=role))
    session.flush()
    return user


def test_it_requires_the_development_owner(
    seeder: ModuleType, db_session: Session
) -> None:
    with pytest.raises(LookupError, match="seed_dev_users"):
        seeder.seed(db_session)


def test_seeding_creates_everything_once(
    seeder: ModuleType, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = _dev_user(db_session, seeder.OWNER_EMAIL, Role.PROPIETARIO)
    _dev_user(db_session, "agronomo@lar.local", Role.AGRONOMO)

    first = seeder.seed(db_session)
    output = capsys.readouterr().out

    expected_lotes = sum(len(spec.lotes) for spec in seeder.PREDIOS)
    assert first == {
        "predios": len(seeder.PREDIOS),
        "lotes": expected_lotes,
        "members": 2,
    }
    assert "OVERLAPS_OWN_PREDIO" in output
    # Missing development users are reported, not fatal.
    assert "omitido aplicador@lar.local" in output

    owned = db_session.execute(
        select(func.count()).where(Predio.owner_user_id == owner.id)
    ).scalar_one()
    assert owned == len(seeder.PREDIOS)
    propietario_grants = db_session.execute(
        select(func.count()).where(
            UserPredioRole.user_id == owner.id,
            UserPredioRole.role == Role.PROPIETARIO,
        )
    ).scalar_one()
    assert propietario_grants == len(seeder.PREDIOS)

    # Idempotent: a second run finds everything in place.
    assert seeder.seed(db_session) == {"predios": 0, "lotes": 0, "members": 0}
    lotes = db_session.execute(
        select(func.count())
        .select_from(Lote)
        .join(Predio, Predio.id == Lote.predio_id)
        .where(Predio.owner_user_id == owner.id)
    ).scalar_one()
    assert lotes == expected_lotes


def test_a_dry_run_writes_nothing(seeder: ModuleType, db_session: Session) -> None:
    owner = _dev_user(db_session, seeder.OWNER_EMAIL, Role.PROPIETARIO)
    assert seeder.seed(db_session, dry_run=True) == {
        "predios": 0,
        "lotes": 0,
        "members": 0,
    }
    owned = db_session.execute(
        select(func.count()).where(Predio.owner_user_id == owner.id)
    ).scalar_one()
    assert owned == 0
