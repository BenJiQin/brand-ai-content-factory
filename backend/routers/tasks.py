"""Content tasks: CRUD, segment submission, video concatenation."""

import json
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request

from backend.config import MODEL_MAP, DEFAULT_MODEL
from backend.persistence import content_tasks_store, save_content_tasks, tasks_store
from backend.services.seedance import seedance_request, upload_local_to_cdn, upload_url_to_cdn

router = APIRouter(prefix="/api", tags=["tasks"])


async def _submit_segment(seg: dict, content_task: dict) -> str:
    """Submit a single segment to Seedance and return task_id."""
    api_type = "reference2video"
    prompt = seg.get("prompt", "")[:300]
    duration = seg.get("duration", 15)
    duration = max(4, min(15, duration))
    ratio = seg.get("ratio", "9:16")
    image_urls = seg.get("image_urls", [])

    if api_type == "text2video" or not image_urls:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt, "duration": str(duration),
            "ratio": ratio, "generate_audio": True,
        }
        result = seedance_request("/jimeng/text2video", params)
    elif api_type == "image2video" and len(image_urls) == 1:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt, "image_urls": image_urls,
            "duration": str(duration), "ratio": ratio, "generate_audio": True,
        }
        result = seedance_request("/jimeng/image2video", params)
    else:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt, "duration": str(duration),
            "ratio": ratio, "generate_audio": True,
        }
        if image_urls:
            params["image_urls"] = image_urls
        result = seedance_request("/jimeng/reference2video", params)

    seedance_task_id = result.get("task_id")
    if not seedance_task_id:
        raise HTTPException(500, f"No task_id from Seedance: {json.dumps(result, ensure_ascii=False)}")

    tasks_store[seedance_task_id] = {
        "task_id": seedance_task_id, "type": api_type, "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio},
        "result": None, "created_at": time.time(), "updated_at": time.time(),
    }
    return seedance_task_id


@router.post("/content-tasks")
async def create_content_task(
    title: str = Form(""),
    brand: str = Form(""),
    sku: str = Form(""),
    script: str = Form(""),
    storyboard: str = Form(""),
    segments: str = Form("[]"),
):
    """Create a new content task."""
    task_id = str(uuid.uuid4())[:8]
    try:
        segs = json.loads(segments) if isinstance(segments, str) else segments
    except json.JSONDecodeError:
        segs = []

    for seg in segs:
        if not seg.get("segment_id"):
            seg["segment_id"] = str(uuid.uuid4())[:6]
        seg.setdefault("status", "pending")
        seg.setdefault("seedance_task_id", None)
        seg.setdefault("video_url", None)

    has_submitted = any(s.get("status") in ("submitted", "generating") for s in segs)
    status = "generating" if has_submitted else ("storyboard_ready" if segs else "draft")
    now = time.time()
    task = {
        "task_id": task_id,
        "title": title or f"内容任务 {task_id}",
        "brand": brand, "sku": sku,
        "script": script, "storyboard": storyboard,
        "segments": segs, "status": status,
        "created_at": now, "updated_at": now,
    }
    content_tasks_store[task_id] = task
    save_content_tasks()
    return {"success": True, "task": task}


@router.get("/content-tasks")
async def list_content_tasks(status: Optional[str] = None, brand: Optional[str] = None):
    """List content tasks with optional filtering."""
    tasks = list(content_tasks_store.values())
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if brand:
        tasks = [t for t in tasks if t["brand"] == brand]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return {"success": True, "tasks": tasks}


@router.get("/content-tasks/{task_id}")
async def get_content_task(task_id: str):
    """Get a single content task."""
    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")
    return {"success": True, "task": task}


@router.put("/content-tasks/{task_id}")
async def update_content_task(
    task_id: str,
    title: Optional[str] = Form(None),
    storyboard: Optional[str] = Form(None),
    segments: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
):
    """Update a content task."""
    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")
    if title is not None:
        task["title"] = title
    if storyboard is not None:
        task["storyboard"] = storyboard
    if segments is not None:
        try:
            task["segments"] = json.loads(segments) if isinstance(segments, str) else segments
        except json.JSONDecodeError:
            pass
    if status is not None:
        task["status"] = status
    task["updated_at"] = time.time()
    save_content_tasks()
    return {"success": True, "task": task}


@router.post("/content-tasks/{task_id}/submit")
async def submit_all_segments(task_id: str):
    """Submit all pending segments to Seedance."""
    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")

    submitted = []
    errors = []
    for seg in task["segments"]:
        if seg.get("status") not in (None, "pending", "failed"):
            continue
        try:
            seedance_id = await _submit_segment(seg, task)
            seg["seedance_task_id"] = seedance_id
            seg["status"] = "submitted"
            submitted.append(seg["segment_id"])
        except Exception as e:
            seg["status"] = "failed"
            errors.append({"segment_id": seg["segment_id"], "error": str(e)})

    task["status"] = "generating"
    task["updated_at"] = time.time()
    save_content_tasks()
    return {"success": True, "submitted": submitted, "errors": errors}


@router.post("/content-tasks/{task_id}/segments/{seg_id}/submit")
async def submit_single_segment(task_id: str, seg_id: str):
    """Submit a single segment to Seedance."""
    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")

    seg = None
    for s in task["segments"]:
        if str(s.get("segment_id")) == seg_id:
            seg = s
            break
    if not seg:
        raise HTTPException(404, f"Segment not found: {seg_id}")

    try:
        seedance_id = await _submit_segment(seg, task)
        seg["seedance_task_id"] = seedance_id
        seg["status"] = "submitted"
    except Exception as e:
        seg["status"] = "failed"
        raise HTTPException(500, f"Submit failed: {str(e)}")

    statuses = {s["status"] for s in task["segments"]}
    if statuses <= {"completed"}:
        task["status"] = "completed"
    elif "submitted" in statuses or "generating" in statuses:
        task["status"] = "generating"
    task["updated_at"] = time.time()
    save_content_tasks()
    return {"success": True, "seedance_task_id": seedance_id, "segment_id": seg_id}


@router.post("/content-tasks/{task_id}/segments/{seg_id}/status")
async def update_segment_status(task_id: str, seg_id: str, request: Request):
    """Update segment status (supports both JSON and Form body)."""
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        body = await request.json()
    else:
        body = dict(await request.form())
    status = body.get("status", "")
    video_url = body.get("video_url", "")

    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")

    for s in task["segments"]:
        if str(s.get("segment_id")) == seg_id:
            s["status"] = status
            if video_url:
                s["video_url"] = video_url
            break

    statuses = {s["status"] for s in task["segments"]}
    if statuses <= {"completed"}:
        task["status"] = "completed"
    elif "failed" in statuses and not (statuses & {"submitted", "generating"}):
        task["status"] = "partial"
    elif statuses & {"submitted", "generating"}:
        task["status"] = "generating"
    task["updated_at"] = time.time()
    save_content_tasks()
    return {"success": True}


@router.post("/content-tasks/{task_id}/concat")
async def concat_segments(task_id: str):
    """Download all completed segment videos, concat with ffmpeg, upload to CDN."""
    import tempfile
    import subprocess

    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    segments = sorted(task.get("segments", []), key=lambda s: int(s.get("segment_id", 0)))
    video_urls = [(s["segment_id"], s["video_url"]) for s in segments if s.get("status") == "completed" and s.get("video_url")]
    if len(video_urls) < 2:
        return {"success": False, "error": "需要至少2个已完成片段才能拼接"}

    import httpx
    tmpdir = tempfile.mkdtemp(prefix="concat_")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            paths = []
            for seg_id, url in video_urls:
                resp = await client.get(url)
                resp.raise_for_status()
                if len(resp.content) < 10000:
                    return {"success": False, "error": f"片段{seg_id}视频链接已失效，请重新生成"}
                fpath = os.path.join(tmpdir, f"seg_{seg_id}.mp4")
                with open(fpath, "wb") as f:
                    f.write(resp.content)
                paths.append(fpath)

        list_path = os.path.join(tmpdir, "list.txt")
        with open(list_path, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")

        output_path = os.path.join(tmpdir, "final.mp4")
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"ffmpeg 失败: {result.stderr[:200]}"}

        final_name = f"concat_{task_id}_{int(time.time())}.mp4"
        try:
            cdn_url = await upload_local_to_cdn(output_path, final_name)
        except Exception:
            cdn_url = upload_url_to_cdn(f"file://{output_path}", wait=False)
            if cdn_url.startswith("file://"):
                cdn_url = ""

        if not cdn_url:
            return {"success": False, "error": "CDN 上传失败"}

        task["final_video_url"] = cdn_url
        task["updated_at"] = time.time()
        save_content_tasks()
        return {"success": True, "final_video_url": cdn_url}
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
