"""Create development predios and lotes in Colchagua and Maipo.

The locations are real (Santa Cruz and Apalta in Colchagua, Pirque in Maipo)
but every boundary and name is invented: none of these is, or describes, a
real property. Names carry "(demo)" so they cannot be mistaken for one.

Everything goes through PredioService, so the seed obeys the same rules as
the API: validation, area bounds, containment, audit trail, and the
PROPIETARIO grant for the creator. It is idempotent: an existing predio (by
slug), lote (by name) or assignment is left alone.

One predio deliberately overlaps another of the same owner, to exercise the
OVERLAPS_OWN_PREDIO warning.

Requires the development users (run scripts/seed_dev_users.py first).

Usage:
    python scripts/seed_dev_predios.py [--dry-run]
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from sqlalchemy.orm import Session  # noqa: E402

from app.modules.predios.exceptions import AssignmentExistsError  # noqa: E402
from app.modules.predios.repository import PredioRepository  # noqa: E402
from app.modules.predios.service import Actor, PredioService  # noqa: E402
from app.modules.users.repository import UserRepository  # noqa: E402
from app.shared.config import get_settings  # noqa: E402
from app.shared.db import get_session_factory  # noqa: E402
from app.shared.enums import LoteType  # noqa: E402
from app.shared.roles import Role  # noqa: E402

OWNER_EMAIL = "propietario@lar.local"

# Metres per degree of latitude on WGS84, near enough for drawing demo shapes.
METRES_PER_DEGREE_LAT = 110_574.0
METRES_PER_DEGREE_LON_AT_EQUATOR = 111_320.0

Ring = list[tuple[float, float]]
"""A closed ring in metres, east and north of the predio's anchor."""


@dataclass(frozen=True)
class SeedLote:
    """A lote to create, drawn in metres from its predio's anchor."""

    name: str
    lote_type: LoteType
    ring: Ring
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SeedPredio:
    """A predio to create, anchored at a real (lon, lat) in its comuna."""

    slug: str
    name: str
    region: str
    comuna: str
    anchor: tuple[float, float]
    ring: Ring
    lotes: tuple[SeedLote, ...] = ()
    members: tuple[tuple[str, Role], ...] = ()


def _box(x0: float, y0: float, x1: float, y1: float) -> Ring:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def to_geojson(anchor: tuple[float, float], ring: Ring) -> dict[str, Any]:
    """Convert a ring in metres from ``anchor`` to a GeoJSON Polygon."""
    lon0, lat0 = anchor
    metres_per_degree_lon = METRES_PER_DEGREE_LON_AT_EQUATOR * math.cos(
        math.radians(lat0)
    )
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [
                    round(lon0 + east / metres_per_degree_lon, 7),
                    round(lat0 + north / METRES_PER_DEGREE_LAT, 7),
                ]
                for east, north in ring
            ]
        ],
    }


SANTA_CRUZ = (-71.3900, -34.6250)  # west of Santa Cruz, Colchagua
APALTA = (-71.2900, -34.6050)  # Apalta valley, Colchagua
PIRQUE = (-70.5800, -33.6600)  # Pirque, Maipo

PREDIOS: tuple[SeedPredio, ...] = (
    SeedPredio(
        slug="vina-los-aromos-demo",
        name="Viña Los Aromos (demo)",
        region="Libertador General Bernardo O'Higgins",
        comuna="Santa Cruz",
        anchor=SANTA_CRUZ,
        ring=[(0, 0), (820, -40), (900, 380), (640, 610), (60, 560), (0, 0)],
        lotes=(
            SeedLote(
                "Cuartel 1 Carménère",
                LoteType.CUARTEL,
                _box(40, 40, 380, 260),
                {
                    "crop_type": "Vid vinífera",
                    "variety": "Carménère",
                    "planting_year": 2011,
                },
            ),
            SeedLote(
                "Cuartel 2 Cabernet",
                LoteType.CUARTEL,
                _box(400, 20, 780, 300),
                {
                    "crop_type": "Vid vinífera",
                    "variety": "Cabernet Sauvignon",
                    "planting_year": 2008,
                    "row_spacing_m": 2.5,
                    "plant_spacing_m": 1.2,
                },
            ),
            SeedLote(
                "Cuartel 3 Syrah",
                LoteType.CUARTEL,
                _box(60, 290, 380, 520),
                {"crop_type": "Vid vinífera", "variety": "Syrah", "planting_year": 2015},
            ),
            SeedLote("Bodega de insumos", LoteType.BODEGA_AREA, _box(420, 330, 560, 450)),
        ),
        members=(
            ("admin_operaciones@lar.local", Role.ADMIN_OPERACIONES),
            ("agronomo@lar.local", Role.AGRONOMO),
            ("aplicador@lar.local", Role.APLICADOR),
            ("operador_drone@lar.local", Role.OPERADOR_DRONE),
        ),
    ),
    # Deliberately overlaps the north-east corner of Los Aromos: a boundary
    # dispute between neighbouring parcels, flagged as OVERLAPS_OWN_PREDIO.
    SeedPredio(
        slug="vina-los-aromos-norte-demo",
        name="Viña Los Aromos Norte (demo)",
        region="Libertador General Bernardo O'Higgins",
        comuna="Santa Cruz",
        anchor=SANTA_CRUZ,
        ring=[(560, 520), (1150, 470), (1200, 900), (580, 960), (560, 520)],
    ),
    SeedPredio(
        slug="fundo-el-litre-demo",
        name="Fundo El Litre (demo)",
        region="Libertador General Bernardo O'Higgins",
        comuna="Santa Cruz",
        anchor=APALTA,
        ring=[(0, 0), (1300, 100), (1250, 950), (200, 1000), (-100, 500), (0, 0)],
        lotes=(
            SeedLote("Potrero Bajo", LoteType.POTRERO, _box(100, 100, 700, 600)),
            SeedLote(
                "Parcela Olivos",
                LoteType.PARCELA,
                _box(750, 150, 1150, 550),
                {"crop_type": "Olivo", "variety": "Arbequina", "planting_year": 2017},
            ),
        ),
        members=(("agronomo@lar.local", Role.AGRONOMO),),
    ),
    SeedPredio(
        slug="parcela-las-vertientes-demo",
        name="Parcela Las Vertientes (demo)",
        region="Metropolitana de Santiago",
        comuna="Pirque",
        anchor=PIRQUE,
        ring=[(0, 0), (420, 0), (450, 300), (0, 320), (0, 0)],
        lotes=(
            SeedLote(
                "Invernadero 1",
                LoteType.INVERNADERO,
                _box(50, 50, 200, 150),
                {"crop_type": "Tomate"},
            ),
        ),
    ),
)


def seed(session: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Create what is missing and return how many of each thing was created."""
    created = {"predios": 0, "lotes": 0, "members": 0}
    users = UserRepository(session)
    owner = users.get_by_email(OWNER_EMAIL)
    if owner is None:
        raise LookupError(
            f"{OWNER_EMAIL} does not exist: run scripts/seed_dev_users.py first"
        )
    actor = Actor(user_id=owner.id, role=Role.PROPIETARIO)
    service = PredioService(session)
    predios = PredioRepository(session)

    for spec in PREDIOS:
        geojson = to_geojson(spec.anchor, spec.ring)
        predio = predios.get_by_slug(owner.id, spec.slug)
        if dry_run:
            report = service.inspect_geometry(geojson, owner_user_id=owner.id)
            codes = ", ".join(w.code for w in report.warnings) or "sin avisos"
            state = "existe" if predio else "se crearía"
            print(
                f"  {state:10}  {spec.name}: {report.area_m2 / 10_000:.1f} ha ({codes})"
            )
            continue

        if predio is None:
            result = service.create_predio(
                actor,
                geojson=geojson,
                fields={
                    "name": spec.name,
                    "slug": spec.slug,
                    "region": spec.region,
                    "comuna": spec.comuna,
                },
            )
            predio = result.predio
            created["predios"] += 1
            codes = ", ".join(w.code for w in result.warnings) or "sin avisos"
            print(f"  creado    {spec.name}: {predio.area_m2 / 10_000:.1f} ha ({codes})")
        else:
            print(f"  existe    {spec.name}")

        existing_lotes = {lote.name for lote in service.list_lotes(actor, predio.id)}
        for lote in spec.lotes:
            if lote.name in existing_lotes:
                continue
            service.create_lote(
                actor,
                predio.id,
                geojson=to_geojson(spec.anchor, lote.ring),
                fields={
                    "name": lote.name,
                    "lote_type": lote.lote_type,
                    **lote.attributes,
                },
            )
            created["lotes"] += 1
            print(f"    lote    {lote.name}")

        for email, role in spec.members:
            if users.get_by_email(email) is None:
                print(f"    omitido {email}: no existe")
                continue
            try:
                service.assign_member(actor, predio.id, email=email, role=role)
            except AssignmentExistsError:
                continue
            created["members"] += 1
            print(f"    asignado {email} [{role.value}]")
    return created


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and measure the geometries without writing anything",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.environment != "development":
        print(
            f"Refusing to seed: ENVIRONMENT is {settings.environment!r}, "
            "this script only runs in development.",
            file=sys.stderr,
        )
        return 1

    session = get_session_factory()()
    try:
        print("Sembrando predios de desarrollo (Colchagua y Maipo):")
        created = seed(session, dry_run=args.dry_run)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()

    print(
        f"\n{created['predios']} predio(s), {created['lotes']} lote(s) y "
        f"{created['members']} asignación(es) creados."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
