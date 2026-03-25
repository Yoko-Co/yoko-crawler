"""
FastAPI crawl service — accepts crawl requests, spawns Scrapy subprocesses,
tracks progress, and streams results as NDJSON.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager

import aiofiles
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from auth import verify_api_key
from domain_validator import DomainValidationError, validate_domain
from job_manager import (
    ConcurrencyLimitError,
    DomainAlreadyCrawlingError,
    JobManager,
    JobNotFoundError,
    RESULTS_DIR,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup validation.
    api_key = os.environ.get("YOKO_CRAWL_API_KEY", "")
    if len(api_key) < 32:
        raise RuntimeError(
            "YOKO_CRAWL_API_KEY must be set and at least 32 characters"
        )

    job_manager = JobManager(max_concurrent=3)
    job_manager.startup_sweep()

    app.state.job_manager = job_manager
    app.state.start_time = time.time()

    cleanup_task = asyncio.create_task(job_manager.periodic_cleanup())

    yield

    # Shutdown.
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    try:
        await job_manager.shutdown_all()
    except Exception:
        logger.exception("Error during shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    """Flatten Pydantic validation errors to match WordPress plugin's expected format."""
    errors = exc.errors()
    msg = errors[0]["msg"] if errors else "Validation error"
    return JSONResponse(status_code=422, content={"detail": msg})


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Authenticated routes.
crawl_router = APIRouter(dependencies=[Depends(verify_api_key)])

# Public routes (no auth).
public_router = APIRouter()

# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

_JOB_ID_PATTERN = r"^[a-f0-9]{16}$"


def valid_job_id(
    job_id: str = Path(pattern=_JOB_ID_PATTERN),
) -> str:
    return job_id


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class CrawlRequest(BaseModel):
    domain: str = Field(max_length=253)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@crawl_router.post("/crawl", status_code=202)
async def start_crawl(request: CrawlRequest):
    """Start a new crawl job."""
    try:
        domain = await validate_domain(request.domain)
    except DomainValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    jm: JobManager = app.state.job_manager

    try:
        job = await jm.start_job(domain)
    except ConcurrencyLimitError:
        raise HTTPException(
            status_code=429,
            detail="Concurrency limit reached. Please try again in a few minutes.",
        )
    except DomainAlreadyCrawlingError:
        raise HTTPException(
            status_code=409, detail="This domain is already being crawled."
        )

    # If subprocess failed to spawn, return 500 instead of 202.
    if job.status == "failed":
        raise HTTPException(status_code=500, detail=job.error or "Failed to start crawl")

    structlog.contextvars.bind_contextvars(job_id=job.job_id)
    logger.info("Crawl started", domain=domain)

    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": f"Crawl queued for {domain}",
    }


@crawl_router.get("/crawl/{job_id}")
async def get_status(job_id: str = Depends(valid_job_id)):
    """Check crawl job status."""
    jm: JobManager = app.state.job_manager

    try:
        job = jm.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")

    return await jm.get_status_response(job)


@crawl_router.get("/crawl/{job_id}/results")
async def get_results(job_id: str = Depends(valid_job_id)):
    """Stream crawl results as NDJSON."""
    jm: JobManager = app.state.job_manager

    try:
        job = jm.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Results not available. Job status: {job.status}",
        )

    if not job.result_file.exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    async def stream():
        job.active_readers += 1
        try:
            async with aiofiles.open(job.result_file, "r") as f:
                while True:
                    chunk = await f.read(65536)  # 64KB chunks
                    if not chunk:
                        break
                    yield chunk
        finally:
            job.active_readers -= 1

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename={job_id}.jsonl",
            "X-Accel-Buffering": "no",
        },
    )


@crawl_router.delete("/crawl/{job_id}")
async def delete_job(job_id: str = Depends(valid_job_id)):
    """Cancel/delete a crawl job."""
    jm: JobManager = app.state.job_manager
    await jm.delete_job(job_id)
    logger.info("Job deleted", job_id=job_id)
    return {"deleted": True}


@public_router.get("/health")
async def health():
    """Health check endpoint. No auth required."""
    jm: JobManager = app.state.job_manager
    return {
        "status": "ok",
        "active_jobs": jm.active_job_count,
        "uptime_seconds": int(time.time() - app.state.start_time),
    }


# ---------------------------------------------------------------------------
# Register routers
# ---------------------------------------------------------------------------

app.include_router(crawl_router)
app.include_router(public_router)
