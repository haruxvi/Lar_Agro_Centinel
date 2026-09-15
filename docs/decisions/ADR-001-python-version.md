# ADR-001: Python runtime version

- **Status:** Accepted
- **Date:** 2026-09-15
- **Deciders:** haruxvi

## Context

Lar Agro Centinel depends on a geospatial and computer-vision stack (rasterio,
GeoPandas, Shapely, Fiona, OpenCV, NumPy) whose native extensions are shipped as
prebuilt wheels. When a wheel is missing for the interpreter version, pip falls back
to compiling from source, which for GDAL-based packages means provisioning native
toolchains and libraries on every developer machine, the CI runner and the Docker
image.

The scaffold was initially created on Python 3.14.4, the interpreter installed on the
development machine. Before adding the domain dependencies we verified wheel
availability with `pip install --only-binary=:all:` (Windows, real install) and
`pip download --only-binary=:all: --platform manylinux_* --implementation cp`
(Linux x86_64, the target of Docker and CI).

Two version sets were evaluated on each interpreter: the pins requested for Phase 0
and the latest stable release of each library at the time of evaluation.

## Evidence

| Library       | Phase 0 pin  | 3.14 Win | 3.14 Linux | 3.12 Win | 3.12 Linux |
|---------------|--------------|:--------:|:----------:|:--------:|:----------:|
| rasterio      | 1.4.3        | ❌       | ❌         | ✅       | ✅         |
| geopandas     | 1.1.2        | ✅ ¹     | ✅ ¹       | ✅       | ✅         |
| shapely       | 2.1.0        | ❌       | ❌         | ✅       | ✅         |
| fiona         | 1.10.1       | ❌       | ❌         | ✅       | ✅         |
| opencv-python | 4.10.0.84    | ✅ ²     | ✅ ²       | ✅       | ✅         |
| numpy         | 2.1.3        | ❌       | ❌         | ✅       | ✅         |

| Library       | Latest stable | 3.14 Win | 3.14 Linux | 3.12 Win | 3.12 Linux |
|---------------|---------------|:--------:|:----------:|:--------:|:----------:|
| rasterio      | 1.5.1         | ✅       | ✅         | ✅       | ✅         |
| geopandas     | 1.1.4         | ✅       | ✅         | ✅       | ✅         |
| shapely       | 2.1.2         | ✅       | ✅         | ✅       | ✅         |
| fiona         | 1.10.1        | ❌ ³     | ❌ ³       | ✅       | ✅         |
| opencv-python | 5.0.0.93      | ✅       | ✅         | ✅       | ✅         |
| numpy         | 2.5.3         | ✅       | ✅         | ✅       | ✅         |

1. GeoPandas is a pure-Python wheel (`py3-none-any`); it installs on any interpreter
   and pulls compatible native dependencies (it resolved Shapely 2.1.2, NumPy 2.5.3,
   pandas 3.0.5 and pyogrio 0.13.0 on 3.14).
2. OpenCV ships stable-ABI wheels (`cp37-abi3`), valid for every CPython >= 3.7.
3. PyPI lists no Fiona distribution installable on CPython 3.14 for any version
   (`from versions: none`) on either platform.

The Phase 0 pins for rasterio, Shapely and NumPy predate Python 3.14, so no 3.14
wheel can exist for them; newer releases of those three do provide 3.14 wheels.
Fiona is the only library with no 3.14 wheel in any release.

## Decision

Use **Python 3.12** (3.12.13 at the time of writing) for local development, CI and the
backend Docker image.

- Local interpreter installed at user level with `uv python install 3.12`; the project
  virtual environment `.venv` is built from it.
- `pyproject.toml` keeps `[tool.mypy] python_version = "3.12"` and
  `[tool.ruff] target-version = "py312"`.
- `requirements.txt` states that pins are verified on Python 3.12.
- The backend Dockerfile uses a `python:3.12-slim` base image and CI uses
  `actions/setup-python` with `python-version: '3.12'`.

## Alternatives considered

1. **Stay on Python 3.14 with the Phase 0 pins.** Rejected: four of six critical
   libraries have no wheel, forcing source builds of GDAL-linked packages.
2. **Stay on Python 3.14 with the latest releases and drop Fiona.** GeoPandas 1.x uses
   pyogrio as its default I/O engine, so Fiona is not strictly required for vector
   I/O. Rejected for now: the Phase 0 dependency list requires Fiona explicitly, and
   removing a requested dependency is a product decision outside this evaluation.
   This is the most likely path back to 3.14 (see re-evaluation criteria).
3. **Python 3.13.** Not evaluated; the decision rule compared the installed
   interpreter (3.14) against the fallback requested for Phase 0 (3.12).

## Consequences

- Positive: every critical library installs from wheels on Windows and Linux, with
  both the requested pins and the latest releases. No native toolchain is needed for
  Python packages.
- Positive: Python 3.12 receives security fixes until October 2028.
- Negative: language and standard-library features added in 3.13 and 3.14 are not
  available; code must stay 3.12-compatible (enforced by ruff and mypy targets).
- Negative: the system interpreter on the development machine (3.14) must not be used
  for the project; always use `.venv`.

## Re-evaluation criteria

Revisit this decision when **any** of the following holds:

- Fiona publishes CPython 3.14 wheels for Windows and manylinux x86_64, or
- the project decides to replace Fiona with pyogrio, and
- every dependency in `requirements.txt` (including those added in later phases,
  notably PyTorch in Phase 10) installs with `--only-binary=:all:` on the candidate
  version for both Windows and manylinux x86_64.

The probe used for this ADR can be re-run with the same `pip install
--only-binary=:all:` and `pip download --platform manylinux_2_28_x86_64
--python-version <X.Y>` commands against the full requirement set.
