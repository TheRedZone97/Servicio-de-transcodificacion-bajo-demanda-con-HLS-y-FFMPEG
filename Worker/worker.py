import json
import os
import time
import redis
import ipaddress
import socket
from urllib.parse import urlsplit
from pathlib import Path
import requests
from tempfile import TemporaryDirectory
import docker
from minio import Minio
from minio.error import MinioException
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE_NAME = os.getenv("REDIS_QUEUE", "transcode_jobs")
MAX_DOWNLOAD_BYTES = 1_000_000_000
FFMPEG_IMAGE = "local/ffmpeg-transcoder:dev"
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "127.0.0.1:8333")
S3_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
S3_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET = os.getenv("S3_BUCKET", "hls-output")

def validate_public_video_url(video_url: str) -> None:
    parsed_url = urlsplit(video_url)

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("La URL debe ser HTTP o HTTPS.")

    addresses = socket.getaddrinfo(
        parsed_url.hostname,
        None,
        type=socket.SOCK_STREAM,
    )

    if not addresses:
        raise ValueError("No se pudo resolver el host de la URL.")

    for address in addresses:
        ip_address = ipaddress.ip_address(address[4][0])
        if not ip_address.is_global:
            raise ValueError("La URL debe apuntar a una dirección pública.")
def download_video(video_url: str, destination: Path) -> int:
    validate_public_video_url(video_url)
    downloaded_bytes = 0

    with requests.get(
        video_url,
        stream=True,
        timeout=(5, 30),
        allow_redirects=False,
    ) as response:
        if 300 <= response.status_code < 400:
            raise ValueError("No se permiten redirecciones.")

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        ):
            raise ValueError("La URL no devolvió un archivo de video.")

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise ValueError("El video supera el límite de 1 GB.")

        with destination.open("wb") as video_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue

                downloaded_bytes += len(chunk)
                if downloaded_bytes > MAX_DOWNLOAD_BYTES:
                    raise ValueError("El video supera el límite de 1 GB.")

                video_file.write(chunk)

    return downloaded_bytes

def transcode_to_hls(working_dir: Path) -> Path:
    output_dir = working_dir / "hls"
    output_dir.mkdir()

    docker_client = docker.from_env()
    try:
        docker_client.containers.run(
            image=FFMPEG_IMAGE,
            command=[
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
                "-y",
                "-i",
                "/work/source_video",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "10",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                "/work/hls/segment_%03d.ts",
                "/work/hls/playlist.m3u8",
            ],
            volumes={
                str(working_dir.resolve()): {
                    "bind": "/work",
                    "mode": "rw",
                }
            },
            network_mode="none",
            remove=True,
        )
    finally:
        docker_client.close()

    playlist_path = output_dir / "playlist.m3u8"
    if not playlist_path.is_file():
        raise RuntimeError("FFmpeg no generó la playlist HLS.")

    return playlist_path

def upload_hls_to_storage(
    s3_client: Minio,
    hls_dir: Path,
    job_id: str,
) -> str:
    playlist_path = hls_dir / "playlist.m3u8"
    segment_paths = sorted(hls_dir.glob("segment_*.ts"))

    if not playlist_path.is_file():
        raise RuntimeError("No se encontró la playlist HLS.")
    if not segment_paths:
        raise RuntimeError("No se encontraron segmentos HLS.")

    object_prefix = f"jobs/{job_id}/"

    for segment_path in segment_paths:
        s3_client.fput_object(
            S3_BUCKET,
            f"{object_prefix}{segment_path.name}",
            str(segment_path),
            content_type="video/mp2t",
        )

    s3_client.fput_object(
        S3_BUCKET,
        f"{object_prefix}{playlist_path.name}",
        str(playlist_path),
        content_type="application/vnd.apple.mpegurl",
    )

    return (
        f"http://{S3_ENDPOINT}/{S3_BUCKET}/"
        f"{object_prefix}{playlist_path.name}"
    )


def main():
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )
    client.ping()
    if not S3_ACCESS_KEY or not S3_SECRET_KEY:
        raise RuntimeError(
            "Configura AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY en .env."
        )

    s3_client = Minio(
        S3_ENDPOINT,
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        secure=False,
    )

    if not s3_client.bucket_exists(S3_BUCKET):
        raise RuntimeError(f"No existe el bucket S3: {S3_BUCKET}")
    print(f"Worker escuchando la cola {QUEUE_NAME}")


    while True:
        try:
            job_item = client.blpop(QUEUE_NAME, timeout=0)
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as error:
            print(f"Conexión con Redis interrumpida: {error}. Reintentando en 2 segundos.")
            time.sleep(2)
            continue

        _, raw_job = job_item
        job = json.loads(raw_job)
        job_id = job["job_id"]

        client.hset(f"job:{job_id}", mapping={"status": "processing"})
        print(f"Trabajo recibido: {job_id}")

        try:
            with TemporaryDirectory() as temp_dir:
                working_dir = Path(temp_dir)
                destination = working_dir / "source_video"
                downloaded_bytes = download_video(
                    job["video_url"],
                    destination,
                )
                print(f"Descarga terminada: {downloaded_bytes} bytes.")

                playlist_path = transcode_to_hls(working_dir)
                print(f"Playlist HLS generada: {playlist_path.name}")
                playlist_url = upload_hls_to_storage(
                    s3_client,
                    playlist_path.parent,
                    job_id,
                )
                print(f"Playlist HLS almacenada: {playlist_url}")
            client.hset(
                f"job:{job_id}",
                mapping={
                    "status": "completed",
                    "progress": 100,
                    "playlist_url": playlist_url,
                },
            )
        except (requests.RequestException,
            docker.errors.DockerException,
            MinioException,
            Urllib3HTTPError,
            ValueError,
            OSError,
            RuntimeError,) as error:
            
            client.hset(f"job:{job_id}", mapping={"status": "failed"})
            detail = (
                type(error).__name__
                if isinstance(error, requests.RequestException)
                else str(error)
            )
            print(f"Trabajo {job_id} falló: {detail}")
            continue


if __name__ == "__main__":
    main()