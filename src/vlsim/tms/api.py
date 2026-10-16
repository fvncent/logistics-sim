"""FastAPI app for the TMS service. MVP: health plus read-only endpoints."""

from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, Response, status
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from vlsim.tms.settings import tms_dsn

app = FastAPI(title="VietLink TMS", version="0.1.0")


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_engine(tms_dsn(), pool_pre_ping=True, connect_args={"connect_timeout": 2})


@app.get("/health")
def health(response: Response) -> dict[str, str]:
    try:
        with _engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": "unreachable"}
    return {"status": "ok", "db": "ok"}
