import os

from fastapi import FastAPI

app = FastAPI(title=os.getenv("APP_NAME", "api"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
