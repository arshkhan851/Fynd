import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.database import check_database_connectivity

app = FastAPI(title=os.getenv("APP_NAME", "api"))

_default_origins = "http://localhost:3000,http://127.0.0.1:3000"
_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET"],
    allow_headers=[],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def database_health() -> dict:
    try:
        check_database_connectivity()
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "unavailable"}) from None
    return {"status": "ok"}
