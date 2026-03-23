#!/usr/bin/env python3
"""
品牌AI内容管理平台 — Backend API Server
Wraps Seedance 2.0 video generation + BlueAI Media Storage.

Run: python3 server.py [--port 8889]
"""

import asyncio
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

# --- Config ---
SEEDANCE_API_BASE = "https://bmc-model-openapi.bluemediagroup.cn/api"
SEEDANCE_APPID = "eFtlRLvR0kb5MK48"
SEEDANCE_SECRET = "05CgKka3wpyQabrlOxMM6Ha6k52K2Fpa"

MEDIA_API_HOST = "https://blueai-media-storage.bluemediagroup.cn"
MEDIA_API_KEY = "e8fd667c-061d-40a7-bc7a-d980efc3c4fe"

MODEL_MAP = {
    "2.0": "doubao-seedance-2-0-260128",
    "1.5pro": "doubao-seedance-1-5-pro-251215",
    "1.0pro": "doubao-seedance-1-0-pro-250528",
}
DEFAULT_MODEL = "2.0"

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# In-memory task store
tasks_store: dict = {}  # task_id -> {status, type, params, result, created_at, updated_at}

# Brand asset registry
brand_assets = {
    "人源活力": {
        "光子瓶": {
            "name": "光子瓶 RHC Skin Renewal Protease Essence",
            "images": [
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-610122216633270287/photon_bottle_01.png",
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-610122216633270287/photon_bottle_02.png",
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-610122216633270287/photon_bottle_03.png",
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-610122216633270287/photon_bottle_04.png",
            ],
        }
    }
}

app = FastAPI(title="品牌AI内容管理平台 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Seedance API helpers ---

def seedance_request(endpoint: str, payload: dict) -> dict:
    url = f"{SEEDANCE_API_BASE}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("appid", SEEDANCE_APPID)
    req.add_header("secret", SEEDANCE_SECRET)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def media_api_request(method: str, path: str, body=None) -> dict:
    url = f"{MEDIA_API_HOST}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MEDIA_API_KEY}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_url_to_cdn(source_url: str, wait: bool = True, timeout: int = 120) -> str:
    """Upload a URL to media storage and return CDN URL."""
    body = {
        "source_type": "PUBLIC_URL",
        "object_list": [{"url": source_url, "url_type": 0}],
        "target_storage": ["TOS"],
        "object_acl": "public-read",
    }
    result = media_api_request("POST", "/api/v1/tasks/upload", body)
    task_id = result.get("data", {}).get("task_id")
    if not task_id:
        return source_url  # fallback

    if not wait:
        return source_url

    start = time.time()
    while time.time() - start < timeout:
        status_result = media_api_request("GET", f"/api/v1/tasks/upload/{task_id}?page_num=1&page_size=100")
        data = status_result.get("data", {})
        status = data.get("status", "")
        if status in ("COMPLETED", "PARTIAL_SUCCESS"):
            results = data.get("results", [])
            for r in results:
                for sr in r.get("object_storage_result", []):
                    url = sr.get("url", "")
                    if url:
                        return url
            return source_url
        time.sleep(3)
    return source_url


async def upload_local_to_cdn(file_path: str, filename: str) -> str:
    """Upload a local file to CDN via S3 temp credentials."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise HTTPException(500, "boto3 required for local upload. pip install boto3")

    ext = os.path.splitext(filename)[1].lstrip('.') or 'bin'
    size = os.path.getsize(file_path)

    body = {
        "file_list": [{"file_name": filename, "file_size": size, "file_ext": ext}],
        "target_storage": ["TOS"],
        "source_storage": "TOS",
        "task_type": "UPLOAD",
    }
    result = media_api_request("POST", "/api/v1/tasks/local_upload", body)
    data = result.get("data", {})
    task_id = data.get("task_id")
    file_objects = data.get("file_objects", [])
    cred = data.get("os_credential", {})

    if not task_id or not file_objects or not cred:
        raise HTTPException(500, f"Failed to get upload credentials: {json.dumps(result)}")

    endpoint = cred.get("endpoint", "")
    bucket = cred.get("bucket_name", "")
    region = cred.get("region", "cn-beijing")

    s3 = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=cred.get("temp_secret_id"),
        aws_secret_access_key=cred.get("temp_secret_key"),
        aws_session_token=cred.get("session_token") or None,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4",
        "mov": "video/quicktime", "mp3": "audio/mpeg", "wav": "audio/wav",
    }

    fo = file_objects[0]
    obj_key = fo.get("object_key")
    media_id = fo.get("media_id")

    content_type = mime_map.get(ext.lower(), "application/octet-stream")
    with open(file_path, "rb") as f:
        s3.upload_fileobj(f, bucket, obj_key, ExtraArgs={"ContentType": content_type})

    try:
        s3.put_object_acl(Bucket=bucket, Key=obj_key, ACL="public-read")
    except Exception:
        pass

    # Report progress
    progress_body = {
        "task_id": task_id,
        "object_progress_list": [{"object_key": obj_key, "media_id": media_id, "progress": 100}],
    }
    try:
        media_api_request("POST", "/api/v1/tasks/callback_upload_progress", progress_body)
    except Exception:
        pass

    # Poll for final URL
    start = time.time()
    while time.time() - start < 120:
        status_result = media_api_request("GET", f"/api/v1/tasks/upload/{task_id}?page_num=1&page_size=100")
        sdata = status_result.get("data", {})
        status = sdata.get("status", "")
        if status in ("COMPLETED", "PARTIAL_SUCCESS"):
            for r in sdata.get("results", []):
                for sr in r.get("object_storage_result", []):
                    url = sr.get("url", "")
                    if url:
                        return url
            break
        await asyncio.sleep(3)

    ep_host = endpoint.replace("https://", "").replace("http://", "").split("/")[0]
    return f"https://{bucket}.{ep_host}/{obj_key}"


# --- API Endpoints ---

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and return its CDN URL."""
    # Save locally first
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    local_path = UPLOAD_DIR / safe_name
    content = await file.read()
    with open(local_path, "wb") as f:
        f.write(content)

    try:
        cdn_url = await upload_local_to_cdn(str(local_path), file.filename)
        return {"success": True, "url": cdn_url, "filename": file.filename, "size": len(content)}
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")
    finally:
        # Clean up local file after upload
        try:
            os.remove(local_path)
        except Exception:
            pass


@app.post("/api/upload/url")
async def upload_from_url(url: str = Form(...)):
    """Upload from a public URL and return CDN URL."""
    try:
        cdn_url = upload_url_to_cdn(url, wait=True)
        return {"success": True, "url": cdn_url, "original_url": url}
    except Exception as e:
        raise HTTPException(500, f"URL upload failed: {str(e)}")


@app.post("/api/generate/text2video")
async def generate_text2video(
    prompt: str = Form(...),
    duration: int = Form(10),
    ratio: str = Form("16:9"),
    model: str = Form("2.0"),
    audio: bool = Form(True),
):
    """Generate video from text prompt."""
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    params = {
        "model_id": model_id,
        "prompt": prompt,
        "duration": str(duration),
        "ratio": ratio,
        "generate_audio": audio,
    }

    try:
        result = seedance_request("/jimeng/text2video", params)
    except Exception as e:
        raise HTTPException(500, f"Seedance API error: {str(e)}")

    task_id = result.get("task_id")
    if not task_id:
        raise HTTPException(500, f"No task_id returned: {json.dumps(result, ensure_ascii=False)}")

    tasks_store[task_id] = {
        "task_id": task_id,
        "type": "text2video",
        "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio, "model": model},
        "result": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    return {"success": True, "task_id": task_id, "type": "text2video"}


@app.post("/api/generate/image2video")
async def generate_image2video(
    prompt: str = Form(""),
    image_url: str = Form(...),
    duration: int = Form(5),
    ratio: str = Form("16:9"),
    model: str = Form("2.0"),
    audio: bool = Form(True),
):
    """Generate video from image (first frame)."""
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])
    params = {
        "model_id": model_id,
        "prompt": prompt,
        "image_urls": [image_url],
        "duration": str(duration),
        "ratio": ratio,
        "generate_audio": audio,
    }

    try:
        result = seedance_request("/jimeng/image2video", params)
    except Exception as e:
        raise HTTPException(500, f"Seedance API error: {str(e)}")

    task_id = result.get("task_id")
    if not task_id:
        raise HTTPException(500, f"No task_id returned: {json.dumps(result, ensure_ascii=False)}")

    tasks_store[task_id] = {
        "task_id": task_id,
        "type": "image2video",
        "status": "submitted",
        "prompt": prompt[:100],
        "image_url": image_url,
        "params": {"duration": duration, "ratio": ratio, "model": model},
        "result": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    return {"success": True, "task_id": task_id, "type": "image2video"}


@app.post("/api/generate/reference")
async def generate_reference(
    prompt: str = Form(...),
    image_urls: str = Form("[]"),  # JSON array of URLs
    video_urls: str = Form("[]"),
    audio_urls: str = Form("[]"),
    duration: int = Form(10),
    ratio: str = Form("16:9"),
    model: str = Form("2.0"),
    audio: bool = Form(True),
):
    """Multi-modal reference to video (Seedance 2.0)."""
    model_id = MODEL_MAP.get(model, MODEL_MAP[DEFAULT_MODEL])

    try:
        img_list = json.loads(image_urls) if isinstance(image_urls, str) else image_urls
        vid_list = json.loads(video_urls) if isinstance(video_urls, str) else video_urls
        aud_list = json.loads(audio_urls) if isinstance(audio_urls, str) else audio_urls
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON in image_urls/video_urls/audio_urls")

    params = {
        "model_id": model_id,
        "prompt": prompt,
        "duration": str(duration),
        "ratio": ratio,
        "generate_audio": audio,
    }
    if img_list:
        params["image_urls"] = img_list
    if vid_list:
        params["video_urls"] = vid_list
    if aud_list:
        params["audio_urls"] = aud_list

    try:
        result = seedance_request("/jimeng/reference2video", params)
    except Exception as e:
        raise HTTPException(500, f"Seedance API error: {str(e)}")

    task_id = result.get("task_id")
    if not task_id:
        raise HTTPException(500, f"No task_id returned: {json.dumps(result, ensure_ascii=False)}")

    tasks_store[task_id] = {
        "task_id": task_id,
        "type": "reference",
        "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio, "model": model,
                    "images": len(img_list), "videos": len(vid_list), "audios": len(aud_list)},
        "result": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    return {"success": True, "task_id": task_id, "type": "reference"}


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """Poll task status from Seedance API."""
    try:
        result = seedance_request("/jimeng/task_status", {"task_id": task_id})
    except Exception as e:
        raise HTTPException(500, f"Failed to poll task: {str(e)}")

    status = result.get("status", "unknown")

    # Update local store
    if task_id in tasks_store:
        tasks_store[task_id]["status"] = status
        tasks_store[task_id]["updated_at"] = time.time()
        if status in ("succeeded", "failed", "error"):
            tasks_store[task_id]["result"] = result

    return {
        "success": True,
        "task_id": task_id,
        "status": status,
        "data": result,
    }


@app.get("/api/tasks")
async def list_tasks(limit: int = 20):
    """List recent tasks."""
    sorted_tasks = sorted(tasks_store.values(), key=lambda t: t["created_at"], reverse=True)[:limit]
    return {"success": True, "tasks": sorted_tasks}


@app.get("/api/brands")
async def list_brands():
    """List all brands and their assets."""
    return {"success": True, "brands": brand_assets}


@app.get("/api/brands/{brand}/{sku}/images")
async def get_sku_images(brand: str, sku: str):
    """Get CDN image URLs for a specific brand/SKU."""
    b = brand_assets.get(brand, {})
    s = b.get(sku, {})
    if not s:
        raise HTTPException(404, f"Brand/SKU not found: {brand}/{sku}")
    return {"success": True, "brand": brand, "sku": sku, "images": s.get("images", [])}


# Serve static files
static_dir = Path(__file__).parent
app.mount("/assets", StaticFiles(directory=str(static_dir / "public" / "assets")), name="assets")


@app.get("/")
async def serve_index():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/{path:path}")
async def serve_static(path: str):
    file_path = static_dir / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return FileResponse(str(static_dir / "index.html"))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8889)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    print(f"🚀 Starting server at http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
