import os
import redis
from pydantic import BaseModel, HttpUrl
import json
from uuid import uuid4
from fastapi import FastAPI, status, HTTPException
from typing import List, Literal, Optional
from pathlib import Path
from fastapi.responses import FileResponse
from datetime import datetime, timezone
from dotenv import load_dotenv
from minio import Minio
from minio.error import MinioException
from urllib3.exceptions import HTTPError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
QUEUE_NAME = os.getenv("REDIS_QUEUE", "transcode_jobs")
RECENT_JOBS_KEY = "recent_jobs"
MAX_RECENT_JOBS = 10
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "127.0.0.1:8333")
S3_PUBLIC_URL = os.getenv("S3_PUBLIC_URL", f"http://{S3_ENDPOINT}")
S3_BUCKET = os.getenv("S3_BUCKET", "hls-output")
S3_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
S3_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")


redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

app = FastAPI(title = "Servicio de Transcodificación")

@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    frontend_index = Path(__file__).resolve().parent.parent / "Frontend" / "index.html"
    return FileResponse(frontend_index)

class CreateJobRequest(BaseModel):
    video_url: HttpUrl

class CreateJobResponse(BaseModel):
    job_id: str
    status: Literal["queued"]    

class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    progress: int
    playlist_url: Optional[str] = None

class RecentJobResponse(BaseModel):
    job_id: str
    video_url: str
    status: Literal["queued", "processing", "completed", "failed"]
    progress: int
    created_at: str
    playlist_url: Optional[str] = None

class StoredVideoResponse(BaseModel):
    job_id: str
    playlist_url: str
    created_at: Optional[datetime] = None

@app.post(
    "/transcode",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateJobResponse,
)
def create_job(request: CreateJobRequest) -> CreateJobResponse:
    job_id = str(uuid4())
    video_url = str(request.video_url)
    created_at = datetime.now(timezone.utc).isoformat()
    job = {"job_id": job_id, "video_url": video_url}

    try:
        with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(
                f"job:{job_id}",
                mapping={
                    "job_id": job_id,
                    "video_url": video_url,
                    "status": "queued",
                    "progress": 0,
                    "created_at": created_at,
                },
            )
            pipe.lpush(RECENT_JOBS_KEY, job_id)
            pipe.ltrim(RECENT_JOBS_KEY, 0, MAX_RECENT_JOBS - 1)
            pipe.rpush(QUEUE_NAME, json.dumps(job))
            pipe.execute()
    except redis.exceptions.RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis no está disponible.",
        ) from error

    return CreateJobResponse(job_id=job_id, status="queued")    
@app.get(
    "/jobs",
    response_model=List[RecentJobResponse],
    response_model_exclude_none=True,
)
def list_recent_jobs() -> List[RecentJobResponse]:
    try:
        job_ids = redis_client.lrange(
            RECENT_JOBS_KEY,
            0,
            MAX_RECENT_JOBS - 1,
        )
        if not job_ids:
            return []

        with redis_client.pipeline() as pipe:
            for job_id in job_ids:
                pipe.hgetall(f"job:{job_id}")
            jobs = pipe.execute()
    except redis.exceptions.RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis no está disponible.",
        ) from error

    if any(not job for job in jobs):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="El historial contiene trabajos sin datos.",
        )

    return [
        RecentJobResponse(
            job_id=job["job_id"],
            video_url=job["video_url"],
            status=job["status"],
            progress=int(job.get("progress", "0")),
            created_at=job["created_at"],
            playlist_url=job.get("playlist_url"),
        )
        for job in jobs
    ]
@app.get(
    "/videos",
    response_model=List[StoredVideoResponse],
)
def list_stored_videos() -> List[StoredVideoResponse]:
    if not S3_ACCESS_KEY or not S3_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configura AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY en .env.",
        )

    s3_client = Minio(
        S3_ENDPOINT,
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        secure=False,
    )
    videos = []

    try:
        for stored_object in s3_client.list_objects(
            S3_BUCKET,
            prefix="jobs/",
            recursive=True,
        ):
            object_name = stored_object.object_name
            if not object_name.endswith("/playlist.m3u8"):
                continue

            path_parts = object_name.split("/")
            if len(path_parts) != 3 or path_parts[0] != "jobs":
                continue

            videos.append(
                StoredVideoResponse(
                    job_id=path_parts[1],
                    playlist_url=(
                        f"{S3_PUBLIC_URL.rstrip('/')}/"
                        f"{S3_BUCKET}/{object_name}"
                    ),
                    created_at=stored_object.last_modified,
                )
            )
    except (MinioException, HTTPError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo consultar SeaweedFS.",
        ) from error

    videos.sort(
        key=lambda video: video.created_at
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return videos
@app.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
)
def get_job(job_id: str) -> JobStatusResponse:
    try:
        job = redis_client.hgetall(f"job:{job_id}")
    except redis.exceptions.RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis no está disponible.",
        ) from error

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró ese trabajo.",
        )

    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        progress=int(job.get("progress", "0")),
        playlist_url=job.get("playlist_url"),
    )