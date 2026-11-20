import os
import redis
from pydantic import BaseModel, HttpUrl
import json
from uuid import uuid4
from fastapi import FastAPI, status, HTTPException
from typing import Literal, Optional


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
QUEUE_NAME = os.getenv("REDIS_QUEUE", "transcode_jobs")


redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

app = FastAPI(title = "Servicio de Transcodificación")

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

@app.post(
    "/transcode",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateJobResponse,
)
def create_job(request: CreateJobRequest) -> CreateJobResponse:
    job_id = str(uuid4())
    video_url = str(request.video_url)
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
                },
            )
            pipe.rpush(QUEUE_NAME, json.dumps(job))
            pipe.execute()
    except redis.exceptions.RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis no está disponible.",
        ) from error

    return CreateJobResponse(job_id=job_id, status="queued")    

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