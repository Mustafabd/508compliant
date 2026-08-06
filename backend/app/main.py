from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import usage
from .auth import get_current_user
from .db import get_db, init_db
from .models import User
from .remediation.pipeline import RemediationError, run as run_pipeline
from .routes_auth import router as auth_router
from .routes_billing import router as billing_router, webhook_router

logger = logging.getLogger("uvicorn.error")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
JOB_TTL_SECONDS = 30 * 60  # jobs are deleted this long after creation

BASE_DIR = Path(__file__).resolve().parent.parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR.parent / "frontend"

# job_id -> owning user id, so a download link can't be used by anyone but
# the user who created it. In-memory and process-local, same as the rest of
# this app's single-instance assumptions (see auth.py).
_job_owners: dict[str, int] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="508 PDF Accessibility Converter", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(webhook_router)


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9 _.\-]", "", stem).strip() or "document"
    return stem


def _cleanup_stale_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for path in JOBS_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                _job_owners.pop(path.stem.split("_")[0], None)
        except OSError:
            pass


def _delete_job_files(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@app.post("/api/convert")
async def convert(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _cleanup_stale_jobs()
    usage.enforce_limit_or_raise(db, user)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf file.")

    job_id = uuid.uuid4().hex
    input_path = JOBS_DIR / f"{job_id}_in.pdf"
    output_path = JOBS_DIR / f"{job_id}_out.pdf"

    size = 0
    try:
        with open(input_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit.",
                    )
                f.write(chunk)
    except HTTPException:
        _delete_job_files(input_path)
        raise
    finally:
        await file.close()

    try:
        # PDF tagging/OCR-alt-text work is CPU- and I/O-bound and synchronous;
        # run it off the event loop so one conversion can't stall every other
        # request being served by this process.
        report = await run_in_threadpool(run_pipeline, str(input_path), str(output_path), file.filename)
    except RemediationError as exc:
        _delete_job_files(input_path, output_path)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Remediation pipeline failed for job %s", job_id)
        _delete_job_files(input_path, output_path)
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while processing this PDF. It may be malformed or use an "
                   "unsupported feature.",
        ) from None

    background_tasks.add_task(_delete_job_files, input_path)
    _job_owners[job_id] = user.id
    usage.record_usage(db, user, file.filename)

    download_name = f"{_safe_stem(file.filename)}-accessible.pdf"
    return JSONResponse({
        "job_id": job_id,
        "download_name": download_name,
        "report": report,
    })


@app.get("/api/download/{job_id}")
async def download(job_id: str, name: str = "document-accessible.pdf", user: User = Depends(get_current_user)):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Not found.")
    if _job_owners.get(job_id) != user.id:
        raise HTTPException(status_code=404, detail="This download has expired. Please convert the file again.")
    output_path = JOBS_DIR / f"{job_id}_out.pdf"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="This download has expired. Please convert the file again.")
    safe_name = re.sub(r"[^A-Za-z0-9 _.\-]", "", name) or "document-accessible.pdf"
    return FileResponse(output_path, media_type="application/pdf", filename=safe_name)


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
