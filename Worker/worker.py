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


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE_NAME = os.getenv("REDIS_QUEUE", "transcode_jobs")
MAX_DOWNLOAD_BYTES = 1_000_000_000

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


def main():
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )
    client.ping()
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
                destination = Path(temp_dir) / "source_video"
                downloaded_bytes = download_video(
                    job["video_url"],
                    destination,
                )
                print(f"Descarga terminada: {downloaded_bytes} bytes.")
        except (requests.RequestException, ValueError, OSError) as error:
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