import os
import socket
import uuid
import tempfile
import requests
from urllib.parse import urlparse
from datetime import datetime
from google.cloud import storage
from dotenv import load_dotenv

load_dotenv()

GCS_IMAGE_BUCKET    = os.getenv("GCS_IMAGE_BUCKET",    "scraped_images_marketos")
GCS_MARKDOWN_BUCKET = os.getenv("GCS_MARKDOWN_BUCKET", "scraped_html_marketos")
GCS_PROJECT         = os.getenv("GOOGLE_CLOUD_PROJECT", None)

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/"
}

_gcs_client: storage.Client | None = None


def _client() -> storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = storage.Client(project=GCS_PROJECT)
    return _gcs_client


def upload_image_to_gcs(image_url: str) -> str:
    if not image_url or not image_url.startswith("http"):
        return ""
    try:
        domain = urlparse(image_url).netloc
        if not domain: return ""
        socket.gethostbyname(domain)
    except socket.gaierror:
        return ""

    temp_file_path = None
    try:
        response = requests.get(image_url, headers=HEADERS, timeout=15, stream=True)
        if response.status_code != 200: return ""

        content_length = int(response.headers.get('content-length', 0))
        if content_length > _MAX_IMAGE_BYTES:
            print(f"GCS image skip: too large ({content_length} bytes) — {image_url[:80]}")
            return ""

        ext = os.path.splitext(urlparse(image_url).path)[1]
        if not ext or len(ext) > 5: ext = ".jpg"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            downloaded = 0
            for chunk in response.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > _MAX_IMAGE_BYTES:
                    print(f"GCS image skip: exceeded {_MAX_IMAGE_BYTES} bytes mid-stream — {image_url[:80]}")
                    return ""
                tmp.write(chunk)
            temp_file_path = tmp.name

        blob_path = f"scraped/{uuid.uuid4()}{ext}"
        blob = _client().bucket(GCS_IMAGE_BUCKET).blob(blob_path)
        blob.upload_from_filename(temp_file_path, content_type=response.headers.get('content-type', 'image/jpeg'))

        return f"https://storage.googleapis.com/{GCS_IMAGE_BUCKET}/{blob_path}"
    except Exception as e:
        print(f"GCS image upload error: {e}")
        return ""
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except: pass


def upload_markdown_to_gcs(markdown_content: str, domain: str, product_url: str = "") -> str:
    if not markdown_content: return ""
    temp_file_path = None
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H%M%S%f")
        blob_path = f"{date_str}/{domain}_{time_str}.md"

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tmp:
            tmp.write(markdown_content)
            temp_file_path = tmp.name

        blob = _client().bucket(GCS_MARKDOWN_BUCKET).blob(blob_path)
        blob.upload_from_filename(temp_file_path, content_type="text/markdown")
        return f"gs://{GCS_MARKDOWN_BUCKET}/{blob_path}"
    except Exception as e:
        print(f"GCS markdown upload error: {e}")
        return ""
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except: pass


def download_markdown_from_gcs(gcs_ref: str) -> str:
    if not gcs_ref or not gcs_ref.startswith("gs://"): return ""
    try:
        path = gcs_ref[5:]
        bucket_name, blob_path = path.split("/", 1)
        return _client().bucket(bucket_name).blob(blob_path).download_as_text()
    except Exception as e:
        print(f"GCS download error: {e}")
        return ""
