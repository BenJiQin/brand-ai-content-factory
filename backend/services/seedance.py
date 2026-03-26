"""Seedance video generation + BlueAI Media Storage helpers."""

import asyncio
import json
import os
import time
import urllib.request
import urllib.error

from fastapi import HTTPException

from backend.config import (
    SEEDANCE_API_BASE, SEEDANCE_APPID, SEEDANCE_SECRET,
    MEDIA_API_HOST, MEDIA_API_KEY, UPLOAD_DIR,
)


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
        return source_url

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

    progress_body = {
        "task_id": task_id,
        "object_progress_list": [{"object_key": obj_key, "media_id": media_id, "progress": 100}],
    }
    try:
        media_api_request("POST", "/api/v1/tasks/callback_upload_progress", progress_body)
    except Exception:
        pass

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
