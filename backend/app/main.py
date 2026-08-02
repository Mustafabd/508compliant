from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .remediation.pipeline import RemediationError, run as run_pipeline

logger = logging.getLogger("uvicorn.error")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
JOB_TTL_SECONDS = 30 * 60  # jobs are deleted this long after creation

BASE_DIR = Path(__file__).resolve().parent.parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR.parent / "frontend"

app = FastAPI(title="508 PDF Accessibility Converter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
        except OSError:
            pass


def _delete_job_files(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@app.post("/api/convert")
async def convert(file: UploadFile, background_tasks: BackgroundTasks):
    _cleanup_stale_jobs()

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

    download_name = f"{_safe_stem(file.filename)}-accessible.pdf"
    return JSONResponse({
        "job_id": job_id,
        "download_name": download_name,
        "report": report,
    })


@app.get("/api/download/{job_id}")
async def download(job_id: str, name: str = "document-accessible.pdf"):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Not found.")
    output_path = JOBS_DIR / f"{job_id}_out.pdf"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="This download has expired. Please convert the file again.")
    safe_name = re.sub(r"[^A-Za-z0-9 _.\-]", "", name) or "document-accessible.pdf"
    return FileResponse(output_path, media_type="application/pdf", filename=safe_name)


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
