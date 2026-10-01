"""Registry of every module that declares tables.

Importing this module puts all tables on the shared metadata. Foreign keys are
declared by table name across bounded contexts (``user_predio_roles`` points
at ``predios``), and SQLAlchemy can only resolve them when both tables are
registered. The app factory, Alembic and the test suite import this module so
none of them depends on which router happened to load a model first.
"""

from __future__ import annotations

from app.modules.analysis import models as analysis_models
from app.modules.audit import models as audit_models
from app.modules.auth import models as auth_models
from app.modules.predios import models as predios_models
from app.modules.users import models as users_models

__all__ = [
    "analysis_models",
    "audit_models",
    "auth_models",
    "predios_models",
    "users_models",
]
