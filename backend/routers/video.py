"""Video generation & upload endpoints (Seedance integration)."""

import json
import os
import time
import uuid

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from backend.config import MODEL_MAP, DEFAULT_MODEL, UPLOAD_DIR
from backend.persistence import tasks_store
from backend.services.seedance import (
    seedance_request, upload_url_to_cdn, upload_local_to_cdn,
)

router = APIRouter(prefix="/api", tags=["video"])


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and return its CDN URL."""
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
        try:
            os.remove(local_path)
        except Exception:
            pass


@router.post("/upload/url")
async def upload_from_url(url: str = Form(...)):
    """Upload from a public URL and return CDN URL."""
    try:
        cdn_url = upload_url_to_cdn(url, wait=True)
        return {"success": True, "url": cdn_url, "original_url": url}
    except Exception as e:
        raise HTTPException(500, f"URL upload failed: {str(e)}")


@router.post("/generate/text2video")
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
        "task_id": task_id, "type": "text2video", "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio, "model": model},
        "result": None, "created_at": time.time(), "updated_at": time.time(),
    }
    return {"success": True, "task_id": task_id, "type": "text2video"}


@router.post("/generate/image2video")
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
        "model_id": model_id, "prompt": prompt, "image_urls": [image_url],
        "duration": str(duration), "ratio": ratio, "generate_audio": audio,
    }

    try:
        result = seedance_request("/jimeng/image2video", params)
    except Exception as e:
        raise HTTPException(500, f"Seedance API error: {str(e)}")

    task_id = result.get("task_id")
    if not task_id:
        raise HTTPException(500, f"No task_id returned: {json.dumps(result, ensure_ascii=False)}")

    tasks_store[task_id] = {
        "task_id": task_id, "type": "image2video", "status": "submitted",
        "prompt": prompt[:100], "image_url": image_url,
        "params": {"duration": duration, "ratio": ratio, "model": model},
        "result": None, "created_at": time.time(), "updated_at": time.time(),
    }
    return {"success": True, "task_id": task_id, "type": "image2video"}


@router.post("/generate/reference")
async def generate_reference(
    prompt: str = Form(...),
    image_urls: str = Form("[]"),
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
        "model_id": model_id, "prompt": prompt,
        "duration": str(duration), "ratio": ratio, "generate_audio": audio,
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
        "task_id": task_id, "type": "reference", "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio, "model": model,
                    "images": len(img_list), "videos": len(vid_list), "audios": len(aud_list)},
        "result": None, "created_at": time.time(), "updated_at": time.time(),
    }
    return {"success": True, "task_id": task_id, "type": "reference"}


@router.get("/task/{task_id}")
async def get_task_status(task_id: str):
    """Poll task status from Seedance API."""
    try:
        result = seedance_request("/jimeng/task_status", {"task_id": task_id})
    except Exception as e:
        raise HTTPException(500, f"Failed to poll task: {str(e)}")

    status = result.get("status", "unknown")

    if task_id in tasks_store:
        tasks_store[task_id]["status"] = status
        tasks_store[task_id]["updated_at"] = time.time()
        if status in ("succeeded", "failed", "error"):
            tasks_store[task_id]["result"] = result

    return {"success": True, "task_id": task_id, "status": status, "data": result}


@router.get("/tasks")
async def list_tasks(limit: int = 20):
    """List recent tasks."""
    sorted_tasks = sorted(tasks_store.values(), key=lambda t: t["created_at"], reverse=True)[:limit]
    return {"success": True, "tasks": sorted_tasks}
