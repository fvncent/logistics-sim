"""Source scans for rules that ruff cannot enforce."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from tests.compose_info import ROOT, pg_image

# Database-side clock functions. The TMS sets created_at/updated_at from the sim clock, so none
# of these may appear in migrations, the seed path or the TMS layer (incl. as server defaults).
DB_CLOCK = re.compile(
    r"\b(now|clock_timestamp|statement_timestamp|transaction_timestamp|timeofday)\s*\("
    r"|\b(current_timestamp|current_date|current_time|localtimestamp|localtime)\b"
    r"|func\s*\.\s*(now|current_timestamp|localtimestamp|clock_timestamp)\b",
    re.IGNORECASE,
)
SCANNED = [
    *sorted((ROOT / "migrations").rglob("*.py")),
    *sorted((ROOT / "migrations").rglob("*.mako")),
    *sorted((ROOT / "src/vlsim/tms").rglob("*.py")),
    *sorted((ROOT / "src/vlsim/world").rglob("*.py")),
]


def test_scan_covers_migrations_and_seed() -> None:
    names = {p.name for p in SCANNED}
    assert {"0001_initial_schema.py", "seed.py", "schema.py", "build.py", "env.py"} <= names


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_database_clock_functions(path: Path) -> None:
    hits = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DB_CLOCK.search(line)
    ]
    assert hits == []


@pytest.mark.parametrize(
    "line",
    [
        "sa.Column('x', server_default=sa.text('now()'))",
        "server_default=func.now()",
        "DEFAULT CURRENT_TIMESTAMP",
        "SELECT clock_timestamp ()",
        "statement_timestamp()",
        "transaction_timestamp()",
    ],
)
def test_clock_pattern_catches(line: str) -> None:
    assert DB_CLOCK.search(line)


# ADR 0002: the engine side must never touch the database layer directly.
ENGINE_SIDE = ["engine", "models", "emitters", "faults", "world"]
FORBIDDEN_IMPORTS = ("sqlalchemy", "psycopg", "alembic", "vlsim.tms")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("pkg", ENGINE_SIDE)
def test_engine_side_does_not_import_db_layer(pkg: str) -> None:
    bad = [
        f"{p.relative_to(ROOT)}: {name}"
        for p in sorted((ROOT / "src/vlsim" / pkg).rglob("*.py"))
        for name in _imports(p)
        if name.split(".")[0] in FORBIDDEN_IMPORTS or name.startswith(FORBIDDEN_IMPORTS)
    ]
    assert bad == []


def test_ci_postgres_image_matches_compose() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    services = ci["jobs"]["lint-test"]["services"]
    assert services["tms-postgres"]["image"] == pg_image()
