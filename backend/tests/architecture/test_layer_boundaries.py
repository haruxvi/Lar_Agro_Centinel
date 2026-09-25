"""Domain layers must not depend on the web framework.

Imports are read from the AST, not from the file text: a text search would
flag comments, docstrings and string literals that merely mention a module.
See invariant 1 in CLAUDE.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Resolved from this file rather than the working directory, so the test gives
# the same answer whether pytest runs from the repo root or from backend/.
MODULES_DIR = Path(__file__).resolve().parents[2] / "app" / "modules"

FORBIDDEN_IN_DOMAIN = {"fastapi", "starlette"}
DOMAIN_FILES = {"service.py", "repository.py", "models.py", "exceptions.py"}

# Módulos que legítimamente no definen excepciones de dominio.
# La razón es obligatoria: si no se puede escribir una, el módulo
# probablemente sí necesita su exceptions.py.
MODULES_WITHOUT_DOMAIN_EXCEPTIONS: dict[str, str] = {
    "audit": (
        "El servicio de audit es best-effort por diseño: un fallo "
        "al escribir un evento no debe interrumpir la operación "
        "que lo originó. Registra CRITICAL y continúa. "
        "Ver KL-002 en docs/KNOWN-LIMITATIONS.md."
    ),
    "health": (
        "Los health checks devuelven el estado de cada "
        "dependencia como booleano. Un health check que lanza "
        "excepciones no puede reportar degradación parcial."
    ),
}


def _imported_modules(path: Path) -> set[str]:
    """Return the top-level packages imported by a file, absolute imports only."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _has_logic(path: Path) -> bool:
    """Return whether a file defines any function or method.

    Placeholder services are a module docstring and nothing else; a service
    with real behaviour defines at least one function.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(tree)
    )


def test_the_modules_directory_is_where_the_test_expects() -> None:
    # Guards against a silent pass: a wrong path would find zero files.
    assert MODULES_DIR.is_dir(), MODULES_DIR
    assert any(MODULES_DIR.rglob("service.py"))


def test_domain_layer_does_not_import_web_framework() -> None:
    violations = []
    for path in sorted(MODULES_DIR.rglob("*.py")):
        if path.name not in DOMAIN_FILES:
            continue
        offending = _imported_modules(path) & FORBIDDEN_IN_DOMAIN
        if offending:
            violations.append(f"{path.relative_to(MODULES_DIR)}: {sorted(offending)}")
    assert not violations, (
        "Los servicios de dominio no pueden importar el framework "
        "web. Lanzá excepciones de dominio y traducilas en el "
        "router.\nViolaciones:\n" + "\n".join(violations)
    )


def test_every_module_with_service_has_exceptions_module() -> None:
    """A module with domain logic needs a place to define its exceptions.

    Without an ``exceptions.py``, the service is likely raising the web
    framework's exceptions, or defining its own somewhere harder to find.
    Placeholder services (no function definitions) are exempt.
    """
    missing = []
    for service in sorted(MODULES_DIR.rglob("service.py")):
        if not _has_logic(service):
            continue
        if service.parent.name in MODULES_WITHOUT_DOMAIN_EXCEPTIONS:
            continue
        if not (service.parent / "exceptions.py").is_file():
            missing.append(service.parent.name)
    assert not missing, (
        "Estos módulos tienen lógica de dominio pero no exceptions.py:\n"
        + "\n".join(missing)
    )


def test_every_exemption_states_its_reason() -> None:
    # An exemption without a written reason is just a way to silence the rule.
    unexplained = [
        name
        for name, reason in MODULES_WITHOUT_DOMAIN_EXCEPTIONS.items()
        if not reason or not reason.strip()
    ]
    assert not unexplained, f"Exenciones sin razón escrita: {unexplained}"


def test_every_exemption_names_an_existing_module() -> None:
    # A stale entry would exempt whatever module later takes that name.
    stale = [
        name
        for name in MODULES_WITHOUT_DOMAIN_EXCEPTIONS
        if not (MODULES_DIR / name).is_dir()
    ]
    assert not stale, f"Exenciones para módulos que no existen: {stale}"
