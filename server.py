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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import uvicorn
import httpx

# --- Config ---
SEEDANCE_API_BASE = "https://bmc-model-openapi.bluemediagroup.cn/api"
SEEDANCE_APPID = "eFtlRLvR0kb5MK48"
SEEDANCE_SECRET = "05CgKka3wpyQabrlOxMM6Ha6k52K2Fpa"

MEDIA_API_HOST = "https://blueai-media-storage.bluemediagroup.cn"
MEDIA_API_KEY = "e8fd667c-061d-40a7-bc7a-d980efc3c4fe"

# --- Claude API Config (蓝标中转) ---
CLAUDE_API_BASE = os.environ.get("CLAUDE_API_BASE", "https://bmc-llm-relay.bluemediagroup.cn/v1")
CLAUDE_API_KEY = os.environ.get("OPENAI_API_KEY", "sk-bO01ixWMbGEzblzbvyjVXPKeDJgV1oA4uLbnuzmRnO3c6ogl")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "Doubao-Seed-2.0-pro")

# --- OpenClaw Gateway (真·小源 Agent) ---
OPENCLAW_GATEWAY = os.environ.get("OPENCLAW_GATEWAY", "http://localhost:9999")
OPENCLAW_AGENT = os.environ.get("OPENCLAW_AGENT", "xiaoyuan")

BRAND_SYSTEM_PROMPTS = {
    "video_script": """你是一位专业的品牌视频脚本创作师，擅长短视频内容创作（抖音/小红书/B站）。
请根据用户需求和品牌信息，创作专业的视频分镜脚本。
格式要求：
- 标注每个镜头的时间（如 0-3s）
- 描述画面内容、运镜方式
- 给出配音/字幕文案
- 注明视觉风格
语气：专业但不刻板，适合品牌营销""",

    "xiaohongshu": """你是一位小红书爆款文案博主，熟悉小红书的内容风格和算法。
请根据用户需求创作小红书图文种草文案。
格式要求：
- 吸睛标题（带emoji，不超过30字）
- 正文（真实感强，200-400字）
- 话题标签（5-8个）
语气：真实、有共鸣感、有种草欲望""",

    "short_video": """你是一位短视频导演，专注15-30秒的竖屏短视频内容。
请根据用户需求创作短视频脚本。
要求：
- 前3秒必须有钩子
- 节奏紧凑，快剪风格
- 结尾有CTA
- 分镜清晰，可直接执行
语气：简洁有力，节奏感强""",

    "storyboard": """你是Seedance视频任务分配专家。你的职责是：
接收用户的完整口播脚本，将其拆分为多个视频片段（每段4-15秒，Seedance平台限制）。

核心原则：如无必要，勿增实体。
- 如果一段内容能装进15秒，就用一个15秒片段，不要拆成7+8或其他组合
- 尽量减少片段数量，每个片段尽量拉满15秒

对于每个片段，你需要输出以下信息：
1. 片段编号和标题
2. API接口类型：统一使用 reference2video（必须，不要使用text2video或image2video）
3. 时长：优先15秒，只有在内容确实很短时才缩短，最短4秒
4. 画面比例：9:16（竖屏）/ 16:9（横屏）/ 1:1
5. 使用的素材资产（图片URL列表）
6. 色调风格
7. 详细Seedance提示词（中文，包含景别、光线、运镜、色调、整体风格）
8. 负面提示词
9. 与前后片段的衔接说明

输出格式：
===片段1: [标题]===
接口: reference2video
时长: [秒数]s
比例: [9:16|16:9|1:1]
素材: [URL1, URL2, ...]
色调: [描述]
提示词: [详细Seedance提示词]
负面提示词: [不希望出现的内容]
衔接: [与下一段的过渡说明]
===结束===

重要规则：
- 接口类型必须是 reference2video，不要使用其他接口
- 每段时长优先使用15秒，尽量拉满
- 片段数量要精简，能少则少
- 提示词必须中文，详细具体
- 确保片段间有连贯性
- 总时长应覆盖完整脚本
- 中文口播约4字/秒，15秒≈60字。如果脚本超过60字，必须拆分为多个片段
- 用户提示中的[建议分段数]是参考，你应该据此拆分""",
}

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

# Content tasks persistence
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CONTENT_TASKS_FILE = DATA_DIR / "content_tasks.json"
content_tasks_store: dict = {}  # task_id -> content_task dict


def load_content_tasks():
    global content_tasks_store
    if CONTENT_TASKS_FILE.exists():
        try:
            content_tasks_store = json.loads(CONTENT_TASKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            content_tasks_store = {}


def save_content_tasks():
    CONTENT_TASKS_FILE.write_text(
        json.dumps(content_tasks_store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


load_content_tasks()

# Social publish persistence
SOCIAL_PUBLISH_FILE = DATA_DIR / "social_publish.json"
social_publish_store: dict = {}  # publish_id -> publish record


def load_social_publish():
    global social_publish_store
    if SOCIAL_PUBLISH_FILE.exists():
        try:
            social_publish_store = json.loads(SOCIAL_PUBLISH_FILE.read_text(encoding="utf-8"))
        except Exception:
            social_publish_store = {}


def save_social_publish():
    SOCIAL_PUBLISH_FILE.write_text(
        json.dumps(social_publish_store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


load_social_publish()

# Ad campaigns persistence
AD_CAMPAIGNS_FILE = DATA_DIR / "ad_campaigns.json"
ad_campaigns_store: dict = {}


def load_ad_campaigns():
    global ad_campaigns_store
    if AD_CAMPAIGNS_FILE.exists():
        try:
            ad_campaigns_store = json.loads(AD_CAMPAIGNS_FILE.read_text(encoding="utf-8"))
        except Exception:
            ad_campaigns_store = {}


def save_ad_campaigns():
    AD_CAMPAIGNS_FILE.write_text(
        json.dumps(ad_campaigns_store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


load_ad_campaigns()

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
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-612037061129535503/80eae667-bdd2-419e-b027-970e049bbfcb.jpg",
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-612037118759272453/92246334-9228-4f7a-a059-94306850a329.jpg",
                "https://vlc-bmc-media-storage-global.bluemediacdn.com/blueai_media_storage/1746837956497416/task-612037147901296645/87bc1e3e-94fa-41aa-a857-10eb5e97ae37.jpg",
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


# --- Copy Generation ---
@app.post("/api/generate/copy")
async def generate_copy(
    request: Request,
    prompt: str = Form(...),
    brand: str = Form(""),
    sku: str = Form(""),
    mode: str = Form("video_script"),
):
    """Stream copy generation via 小源 (OpenClaw) with brand knowledge + data insights."""
    # --- 1. Brand knowledge base ---
    brand_knowledge = _load_brand_knowledge()
    context = "你是人源活力品牌专属内容策划师，基于品牌知识库和数据洞察，创作高质量的社媒营销内容。\n\n"
    if brand_knowledge:
        context += f"## 品牌与产品知识库\n{brand_knowledge}\n\n"
    else:
        context += f"品牌：人源活力（RHC）\n产品：{sku}\n"

    # --- 2. Data insights feedback loop ---
    data_insights = _load_data_insights()
    if data_insights:
        context += f"## 数据洞察（请参考以优化内容策略）\n{data_insights}\n\n"

    # --- 3. Mode-specific instructions ---
    context += "## 创作要求\n"
    if mode == "xiaohongshu":
        context += "类型：小红书种草文案。风格活泼真实，用emoji，有标题、正文、标签。字数200-300字，带话题标签。避免营销感太重。\n"
    elif mode == "short_video":
        context += "类型：15秒短视频脚本。分3-4个镜头，每个镜头配画面描述+旁白+字幕。前3秒必须有钩子，节奏快，有记忆点。\n"
    else:
        context += "类型：Seedance视频生成提示词。用中文详细描述画面内容、视觉风格、镜头语言，适合AI视频模型生成10-15秒竖屏视频。包含景别、光线、运镜、色调、整体风格。\n"

    # --- 4. Image-aware context ---
    context += "\n## 素材使用指南\n"
    context += "用户已选择的素材图片及其场景描述会在用户消息中列出。请根据素材的具体内容来设计脚本的画面和叙事。\n"
    context += "- 白底图 → 适合产品特写、品牌logo露出\n"
    context += "- 模特展示/使用演示 → 适合真人种草、使用教程\n"
    context += "- 场景图 → 适合氛围营造、生活方式展示\n"
    context += "- 包装/开箱 → 适合开箱惊喜、拆快递场景\n"
    context += "\n重要：全部使用中文输出。"

    full_message = f"[创作上下文]\n{context}\n\n[用户创作需求]\n{prompt}"

    async def stream():
        try:
            reply = await _call_xiaoyuan(full_message, session_key="copy-gen")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/generate/storyboard")
async def generate_storyboard(
    request: Request,
    script: str = Form(...),
    brand: str = Form(""),
    sku: str = Form(""),
    asset_urls: str = Form("[]"),
    asset_descriptions: str = Form("[]"),
    aspect_ratio: str = Form("9:16"),
    visual_style: str = Form(""),
):
    """Stream storyboard generation via 小源 (OpenClaw), with brand knowledge + image awareness."""
    try:
        urls = json.loads(asset_urls) if isinstance(asset_urls, str) else asset_urls
    except json.JSONDecodeError:
        urls = []
    try:
        descs = json.loads(asset_descriptions) if isinstance(asset_descriptions, str) else asset_descriptions
    except json.JSONDecodeError:
        descs = []

    context = BRAND_SYSTEM_PROMPTS.get("storyboard", "")

    # --- Brand knowledge injection ---
    brand_knowledge = _load_brand_knowledge()
    if brand_knowledge:
        context += f"\n\n## 品牌与产品知识库\n{brand_knowledge}"

    # --- Data insights feedback ---
    data_insights = _load_data_insights()
    if data_insights:
        context += f"\n\n## 数据洞察（参考以优化内容策略）\n{data_insights}"

    if brand:
        context += f"\n\n当前品牌：{brand}"
    if sku:
        context += f"\n当前产品：{sku}"

    # --- Image-aware asset descriptions ---
    if descs:
        context += "\n\n## 可用素材及场景说明（请根据每张图的具体内容来分配镜头）"
        for d in descs:
            context += f"\n- [{d.get('sku','')}·{d.get('label','')}] {d.get('scene','')} → URL: {d.get('url','')}"
        context += "\n\n注意：为每个片段选择最匹配场景的素材。白底图适合产品特写，模特/使用图适合真人演示，场景图适合氛围营造。"
    elif urls:
        context += f"\n可用素材URL：\n" + "\n".join(urls)

    if aspect_ratio:
        context += f"\n默认画面比例：{aspect_ratio}"
    if visual_style:
        context += f"\n视觉风格要求：{visual_style}"

    # Calculate script length and suggest segment count
    char_count = len(script.strip())
    suggested_segments = max(1, (char_count + 59) // 60)  # ~60字/15秒 at 4字/秒
    seg_hint = f"[口播脚本约{char_count}字，按4字/秒语速约{char_count/4:.0f}秒，建议拆分为{suggested_segments}个15秒片段]"

    full_message = f"[分镜创作上下文]\n{context}\n\n{seg_hint}\n\n请为以下脚本生成Seedance视频分镜方案：\n\n{script}"

    async def stream():
        try:
            reply = await _call_xiaoyuan(full_message, session_key="storyboard-gen")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- Content Intelligence: Data Summary + LLM Insights ---

@app.get("/api/insights/summary")
async def insights_summary():
    """Aggregate social + ad data into structured summary for display and LLM analysis."""
    social = list(social_publish_store.values())
    ads = list(ad_campaigns_store.values())
    tasks = list(content_tasks_store.values())

    # --- Per-SKU aggregation ---
    sku_map = {}  # sku -> { social: {...}, ad: {...} }
    for r in social:
        key = r.get("sku") or "未分类"
        if key not in sku_map:
            sku_map[key] = {"brand": r.get("brand", ""), "social": [], "ad": []}
        sku_map[key]["social"].append(r)
    for r in ads:
        key = r.get("sku") or "未分类"
        if key not in sku_map:
            sku_map[key] = {"brand": r.get("brand", ""), "social": [], "ad": []}
        sku_map[key]["ad"].append(r)

    sku_summary = []
    for sku, data in sku_map.items():
        s_list = data["social"]
        a_list = data["ad"]
        total_views = sum(r.get("metrics", {}).get("views", 0) for r in s_list)
        total_interact = sum(
            r["metrics"].get("likes", 0) + r["metrics"].get("comments", 0) +
            r["metrics"].get("shares", 0) + r["metrics"].get("favorites", 0)
            for r in s_list if r.get("metrics")
        )
        avg_comp = (sum(r["metrics"].get("completion_rate", 0) for r in s_list) / len(s_list)) if s_list else 0
        total_spend = sum(r.get("metrics", {}).get("spend", 0) for r in a_list)
        total_conv = sum(r.get("metrics", {}).get("conversions", 0) for r in a_list)
        weighted_roas = (
            sum(r["metrics"].get("roas", 0) * r["metrics"].get("spend", 0) for r in a_list) / total_spend
            if total_spend > 0 else 0
        )
        sku_summary.append({
            "sku": sku,
            "brand": data["brand"],
            "social_count": len(s_list),
            "ad_count": len(a_list),
            "total_views": total_views,
            "total_interact": total_interact,
            "avg_completion_rate": round(avg_comp, 3),
            "total_spend": round(total_spend, 2),
            "total_conversions": total_conv,
            "weighted_roas": round(weighted_roas, 2),
        })

    # --- Per-platform aggregation ---
    plat_social = {}
    for r in social:
        p = r.get("platform", "unknown")
        if p not in plat_social:
            plat_social[p] = {"views": 0, "interact": 0, "count": 0, "comp_sum": 0}
        m = r.get("metrics", {})
        plat_social[p]["views"] += m.get("views", 0)
        plat_social[p]["interact"] += m.get("likes", 0) + m.get("comments", 0) + m.get("shares", 0) + m.get("favorites", 0)
        plat_social[p]["count"] += 1
        plat_social[p]["comp_sum"] += m.get("completion_rate", 0)

    plat_ad = {}
    for r in ads:
        p = r.get("platform", "unknown")
        if p not in plat_ad:
            plat_ad[p] = {"spend": 0, "impressions": 0, "clicks": 0, "conversions": 0, "roas_weighted": 0}
        m = r.get("metrics", {})
        plat_ad[p]["spend"] += m.get("spend", 0)
        plat_ad[p]["impressions"] += m.get("impressions", 0)
        plat_ad[p]["clicks"] += m.get("clicks", 0)
        plat_ad[p]["conversions"] += m.get("conversions", 0)
        plat_ad[p]["roas_weighted"] += m.get("roas", 0) * m.get("spend", 0)

    platform_summary = []
    all_plats = set(list(plat_social.keys()) + list(plat_ad.keys()))
    for p in all_plats:
        s = plat_social.get(p, {})
        a = plat_ad.get(p, {})
        spend = a.get("spend", 0)
        platform_summary.append({
            "platform": p,
            "social_views": s.get("views", 0),
            "social_interact": s.get("interact", 0),
            "social_count": s.get("count", 0),
            "avg_completion": round(s.get("comp_sum", 0) / s["count"], 3) if s.get("count") else 0,
            "ad_spend": round(spend, 2),
            "ad_impressions": a.get("impressions", 0),
            "ad_clicks": a.get("clicks", 0),
            "ad_conversions": a.get("conversions", 0),
            "ad_roas": round(a.get("roas_weighted", 0) / spend, 2) if spend > 0 else 0,
        })

    return {
        "success": True,
        "sku_summary": sku_summary,
        "platform_summary": platform_summary,
        "totals": {
            "content_tasks": len(tasks),
            "social_records": len(social),
            "ad_campaigns": len(ads),
            "total_views": sum(s.get("total_views", 0) for s in sku_summary),
            "total_spend": round(sum(s.get("total_spend", 0) for s in sku_summary), 2),
            "total_conversions": sum(s.get("total_conversions", 0) for s in sku_summary),
        },
    }


@app.post("/api/insights/analyze")
async def insights_analyze(request: Request):
    """Use LLM to generate strategic insights from aggregated data. Returns SSE stream."""
    body = await request.json()
    data_summary = body.get("data_summary", "")
    focus = body.get("focus", "comprehensive")  # comprehensive / social / ad / creative

    system = """你是一位顶级品牌数据策略分析师，服务于人源活力「品牌AI内容工厂」平台。

你的任务：基于社媒自然流量数据和广告投放数据，输出可操作的内容策略洞察。

输出格式要求（必须严格遵循）：

## 📊 数据总览
用1-2句话概括当前数据表现的全局画面。

## 🔥 核心发现
列出3-5条最重要的数据发现，每条用数据支撑。格式：
- **发现标题**：具体数据 + 解读

## 🎯 内容策略建议
基于数据给出3条可执行的内容创作策略，每条包含：
- 策略名称
- 具体做法
- 预期效果

## 🚀 下一步行动
给出最优先的1-2个具体行动项，可以直接变成下一轮AI视频创作的指令。
格式：直接给出可用于Seedance视频生成的创作指令/prompt方向。

要求：全中文，数据驱动，结论明确，避免空话套话。每个建议都要有数据依据。"""

    focus_prompts = {
        "social": "请重点分析自然流量数据（播放量、互动率、完播率），找出最佳内容模式。",
        "ad": "请重点分析广告投放数据（ROAS、CTR、CPA），优化投放效率。",
        "creative": "请重点从创意素材角度分析，哪类内容表现最好，下一步应该生成什么样的视频。",
        "comprehensive": "请综合分析自然流量和付费投放数据，给出全面的内容策略建议。",
    }

    user_msg = f"""请以品牌数据策略分析师的身份分析以下数据。

{system}

以下是当前品牌内容在各平台的表现数据汇总：

{data_summary}

{focus_prompts.get(focus, focus_prompts['comprehensive'])}"""

    async def stream():
        try:
            reply = await _call_xiaoyuan(user_msg, session_key="insight-analyze")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- Data Chat: Persistent LLM assistant for data Q&A + modifications ---

def _execute_data_action(action: dict, page: str) -> dict:
    store = social_publish_store if page == "social" else ad_campaigns_store
    save_fn = save_social_publish if page == "social" else save_ad_campaigns
    atype = action.get("type", "")
    if atype == "delete":
        deleted = 0
        for rid in action.get("ids", []):
            if rid in store:
                del store[rid]
                deleted += 1
        if deleted:
            save_fn()
        return {"type": "delete", "deleted": deleted}
    elif atype == "update":
        rid = action.get("id")
        if rid and rid in store:
            for k, v in action.get("fields", {}).items():
                if k == "metrics" and isinstance(v, dict):
                    store[rid].setdefault("metrics", {}).update(v)
                else:
                    store[rid][k] = v
            save_fn()
            return {"type": "update", "id": rid, "success": True}
    return {"type": atype, "success": False}


# --- 小源 workspace (for brand knowledge + data insights used by script generation) ---
XIAOYUAN_WORKSPACE = Path.home() / "renyuan-workspace"


def _load_brand_knowledge() -> str:
    """Load products.md + brand.md for script generation context."""
    parts = []
    for rel in ("memory/core/products.md", "memory/core/brand.md"):
        p = XIAOYUAN_WORKSPACE / rel
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(parts) if parts else ""


def _load_data_insights() -> str:
    """Load recent data insights from 小源's daily logs (today + yesterday)."""
    from datetime import date, timedelta
    insights = []
    for delta in (0, 1):
        d = date.today() - timedelta(days=delta)
        daily = XIAOYUAN_WORKSPACE / "memory" / "daily" / f"{d.isoformat()}.md"
        if daily.exists():
            text = daily.read_text(encoding="utf-8")
            # Extract insight sections (not full log — just conclusions/summaries)
            lines = text.split("\n")
            snippet = []
            for line in lines:
                if "洞察" in line or "结论" in line or "建议" in line or "数据" in line:
                    snippet.append(line)
                elif snippet and line.strip():
                    snippet.append(line)
                elif snippet and not line.strip():
                    snippet.append("")
                    if len(snippet) > 3:
                        break
            if snippet:
                insights.append(f"[{d.isoformat()}] " + "\n".join(snippet[:8]))
    return "\n".join(insights) if insights else ""


# --- Products API: products.md ↔ brands.html 双向联动 ---

PRODUCTS_MD = XIAOYUAN_WORKSPACE / "memory" / "core" / "products.md"
import re as _re


def _parse_products_md() -> list[dict]:
    """Parse products.md into a list of SKU dicts."""
    if not PRODUCTS_MD.exists():
        return []
    text = PRODUCTS_MD.read_text(encoding="utf-8")
    # Split by ## headings
    chunks = _re.split(r'^## ', text, flags=_re.MULTILINE)
    skus = []
    for chunk in chunks[1:]:  # skip preamble
        lines = chunk.strip().split('\n')
        name = lines[0].strip()
        body = '\n'.join(lines[1:])

        sku: dict = {"name": name}

        # Parse **field：** value pairs
        field_map = {
            "电商昵称": "nickname", "正式名": "formalName", "系列": "series",
            "核心技术": "tech", "形态": "form", "适合人群": "targetUsers",
            "使用场景": "scenes", "关键词": "keywords", "备注": "notes",
            "规格": "spec", "价格": "price", "卖点一句话": "oneLiner",
            "使用步骤": "steps",
        }
        for zh, en in field_map.items():
            m = _re.search(rf'\*\*{zh}[：:]\*\*\s*(.*)', body)
            if m:
                sku[en] = m.group(1).strip()

        # Parse selling points (bullet list after **核心卖点**)
        sp_match = _re.search(r'\*\*核心卖点[：:]\*\*\s*\n((?:- .+\n?)+)', body)
        if sp_match:
            sku["sellingPoints"] = [l.lstrip('- ').strip() for l in sp_match.group(1).strip().split('\n') if l.strip()]

        # Parse numbered selling points (### 核心卖点 section)
        numbered_sp = _re.search(r'### 核心卖点[^#]*?\n((?:\d+\..+\n?)+)', body)
        if numbered_sp:
            sku["sellingPointsDetailed"] = []
            for line in numbered_sp.group(1).strip().split('\n'):
                line = _re.sub(r'^\d+\.\s*', '', line).strip()
                if line:
                    sku["sellingPointsDetailed"].append(line)

        # Parse markdown tables (ingredients, competitors)
        def parse_table(section_pattern: str) -> list[dict]:
            m = _re.search(section_pattern + r'[\s\S]*?\n(\|.+\|(?:\n\|.+\|)*)', body)
            if not m:
                return []
            table_text = m.group(1).strip()
            rows = [r.strip() for r in table_text.split('\n') if r.strip()]
            if len(rows) < 3:
                return []
            headers = [h.strip() for h in rows[0].split('|')[1:-1]]
            # Skip separator row (contains only dashes)
            result = []
            for row in rows[1:]:
                if _re.match(r'^\|[\s\-:|]+\|$', row):
                    continue  # skip separator
                cells = [c.strip() for c in row.split('|')[1:-1]]
                if len(cells) == len(headers):
                    result.append(dict(zip(headers, cells)))
            return result

        ingredients = parse_table(r'### 核心成分')
        if not ingredients:
            ingredients = parse_table(r'\| 成分 \|')
        if ingredients:
            sku["ingredients"] = ingredients

        competitors = parse_table(r'### 竞品对比')
        if not competitors:
            competitors = parse_table(r'\| 对比维度 \|')
        if competitors:
            sku["competitors"] = competitors

        skus.append(sku)
    return skus


def _sku_to_md(sku: dict) -> str:
    """Convert a SKU dict back to markdown section (without ## heading)."""
    lines = []
    field_order = [
        ("nickname", "电商昵称"), ("formalName", "正式名"), ("series", "系列"),
        ("notes", "备注"), ("spec", "规格"), ("price", "价格"),
        ("tech", "核心技术"), ("form", "形态"),
    ]
    for en, zh in field_order:
        if sku.get(en):
            lines.append(f"**{zh}：** {sku[en]}")
    lines.append("")

    if sku.get("sellingPoints"):
        lines.append("**核心卖点：**")
        for sp in sku["sellingPoints"]:
            lines.append(f"- {sp}")
        lines.append("")

    if sku.get("ingredients"):
        headers = list(sku["ingredients"][0].keys())
        lines.append("### 核心成分")
        lines.append("")
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["------"] * len(headers)) + "|")
        for row in sku["ingredients"]:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")
        lines.append("")

    if sku.get("oneLiner"):
        lines.append(f"**卖点一句话：** {sku['oneLiner']}")
        lines.append("")

    if sku.get("sellingPointsDetailed"):
        lines.append("### 核心卖点（视频文案用）")
        for i, sp in enumerate(sku["sellingPointsDetailed"], 1):
            lines.append(f"{i}. {sp}")
        lines.append("")

    simple_fields = [
        ("steps", "使用步骤"), ("targetUsers", "适合人群"),
        ("scenes", "使用场景"),
    ]
    for en, zh in simple_fields:
        if sku.get(en):
            lines.append(f"**{zh}：** {sku[en]}")

    if sku.get("competitors"):
        lines.append("")
        lines.append("### 竞品对比")
        lines.append("")
        headers = list(sku["competitors"][0].keys())
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["------"] * len(headers)) + "|")
        for row in sku["competitors"]:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")

    if sku.get("keywords"):
        lines.append("")
        lines.append(f"**关键词：** {sku['keywords']}")

    return "\n".join(lines)


def _save_products_md(skus: list[dict]):
    """Write all SKUs back to products.md, preserving the file header."""
    text = PRODUCTS_MD.read_text(encoding="utf-8") if PRODUCTS_MD.exists() else ""
    # Extract preamble (everything before first ## )
    first_h2 = _re.search(r'^## ', text, flags=_re.MULTILINE)
    preamble = text[:first_h2.start()].rstrip() if first_h2 else text.rstrip()

    parts = [preamble, ""]
    for sku in skus:
        parts.append(f"## {sku['name']}")
        parts.append("")
        parts.append(_sku_to_md(sku))
        parts.append("")
        parts.append("---")
        parts.append("")
    # Remove trailing ---
    while parts and parts[-1].strip() in ("", "---"):
        parts.pop()
    parts.append("")  # final newline

    PRODUCTS_MD.write_text("\n".join(parts), encoding="utf-8")


@app.get("/api/products")
async def list_products():
    return {"success": True, "products": _parse_products_md()}


@app.put("/api/products/{sku_name}")
async def update_product(sku_name: str, request: Request):
    body = await request.json()
    skus = _parse_products_md()
    found = False
    for i, s in enumerate(skus):
        if s["name"] == sku_name:
            body["name"] = sku_name  # preserve name
            skus[i] = body
            found = True
            break
    if not found:
        raise HTTPException(404, f"SKU not found: {sku_name}")
    _save_products_md(skus)
    return {"success": True}


@app.post("/api/products")
async def create_product(request: Request):
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(400, "name is required")
    skus = _parse_products_md()
    # Check duplicate
    if any(s["name"] == body["name"] for s in skus):
        raise HTTPException(409, f"SKU already exists: {body['name']}")
    skus.append(body)
    _save_products_md(skus)
    return {"success": True}


async def _call_xiaoyuan(message: str, session_key: str = "platform") -> str:
    """Call 小源 via OpenClaw Gateway. Falls back to Claude LLM if gateway unavailable."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OPENCLAW_GATEWAY}/api/sessions/{OPENCLAW_AGENT}/send",
                json={"message": message, "session_key": session_key},
            )
            resp.raise_for_status()
            reply = resp.json().get("reply", "")
            if reply:
                return reply
    except (httpx.ConnectError, httpx.HTTPStatusError) as e:
        print(f"[INFO] 小源不可用 ({e})，回退到 Claude LLM")

    # Fallback: Claude LLM
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{CLAUDE_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {CLAUDE_API_KEY}", "Content-Type": "application/json"},
            json={"model": CLAUDE_MODEL, "messages": [{"role": "user", "content": message}]},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


# --- 真·小源 Chat (OpenClaw Gateway) ---

@app.post("/api/chat")
async def chat_with_xiaoyuan(request: Request):
    """Forward chat messages to real 小源 agent via OpenClaw Gateway."""
    body = await request.json()
    user_message = body.get("message", "")
    session_id = body.get("session_id", "web-visitor")
    context = body.get("context", "")  # optional data context for data-chat

    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")

    # If data context is provided, prepend it to the message
    full_message = user_message
    if context:
        full_message = f"[数据上下文]\n{context}\n\n[用户问题]\n{user_message}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OPENCLAW_GATEWAY}/api/sessions/{OPENCLAW_AGENT}/send",
                json={
                    "message": full_message,
                    "session_key": session_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {"reply": data.get("reply", ""), "session_id": session_id}
    except httpx.ConnectError:
        return JSONResponse(
            status_code=503,
            content={"error": "OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行", "session_id": session_id},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))@app.post("/api/data-chat")
async def data_chat(request: Request):
    """Data analysis chat — routes to real 小源 via OpenClaw Gateway with data context."""
    body = await request.json()
    messages = body.get("messages", [])
    page = body.get("page", "social")

    store = social_publish_store if page == "social" else ad_campaigns_store
    id_key = "publish_id" if page == "social" else "campaign_id"
    records = list(store.values())

    # Build compact data summary
    compact = []
    for r in records[:200]:
        item = {"id": r.get(id_key, ""), "title": r.get("title", ""), "platform": r.get("platform", "")}
        if page == "social":
            item["account"] = r.get("account_name", "")
            item["date"] = r.get("published_at", "")
            m = r.get("metrics", {})
            item.update({k: m.get(k, 0) for k in ("views", "likes", "comments", "shares", "favorites", "follows", "gmv", "orders", "gpm")})
        else:
            item["date"] = r.get("date_start", "")
            m = r.get("metrics", {})
            item.update({k: m.get(k, 0) for k in ("spend", "impressions", "clicks", "conversions", "ctr", "cpa", "roas")})
        compact.append(item)

    data_json = json.dumps(compact, ensure_ascii=False)
    page_label = "社媒发布" if page == "social" else "广告投放"
    user_msg = messages[-1]["content"] if messages else ""

    # --- Route to real 小源 via OpenClaw Gateway ---
    data_context = f"用户正在查看{page_label}数据（共{len(records)}条）。数据如下：\n{data_json}\n\n"
    data_context += "如需修改/删除数据，请在回复末尾嵌入操作指令：\n"
    data_context += '删除: <!--ACTION:{"type":"delete","ids":["id1"]}-->\n'
    data_context += '修改: <!--ACTION:{"type":"update","id":"xxx","fields":{"platform":"kuaishou"}}-->\n'

    async def stream():
        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{OPENCLAW_GATEWAY}/api/sessions/{OPENCLAW_AGENT}/send",
                    json={
                        "message": f"[数据上下文]\n{data_context}\n[用户问题]\n{user_msg}",
                        "session_key": f"data-{page}",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                full_text = data.get("reply", "")
                # Stream the complete reply as a single SSE chunk (for frontend compatibility)
                if full_text:
                    yield f"data: {json.dumps({'text': full_text}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            err = "⚠️ OpenClaw Gateway 未运行。请确认 `openclaw gateway start` 已执行。"
            yield f"data: {json.dumps({'text': err}, ensure_ascii=False)}\n\n"
            full_text = err
        except Exception as e:
            err = f"\n\n⚠️ 请求失败: {str(e)}"
            yield f"data: {json.dumps({'text': err}, ensure_ascii=False)}\n\n"
            full_text = err

        # Parse and execute ACTION tags from 小源's response
        import re
        actions = re.findall(r'<!--ACTION:(.*?)-->', full_text)
        executed = []
        for act_json in actions:
            try:
                act = json.loads(act_json)
                result = _execute_data_action(act, page)
                executed.append(result)
            except json.JSONDecodeError:
                pass
        if executed:
            yield f"data: {json.dumps({'actions_executed': executed}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- Content Tasks CRUD ---

@app.post("/api/content-tasks")
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
        "brand": brand,
        "sku": sku,
        "script": script,
        "storyboard": storyboard,
        "segments": segs,
        "status": status,
        "created_at": now,
        "updated_at": now,
    }
    content_tasks_store[task_id] = task
    save_content_tasks()
    return {"success": True, "task": task}


@app.get("/api/content-tasks")
async def list_content_tasks(status: Optional[str] = None, brand: Optional[str] = None):
    """List content tasks with optional filtering."""
    tasks = list(content_tasks_store.values())
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if brand:
        tasks = [t for t in tasks if t["brand"] == brand]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return {"success": True, "tasks": tasks}


@app.get("/api/content-tasks/{task_id}")
async def get_content_task(task_id: str):
    """Get a single content task."""
    task = content_tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Content task not found: {task_id}")
    return {"success": True, "task": task}


@app.put("/api/content-tasks/{task_id}")
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


async def _submit_segment(seg: dict, content_task: dict) -> str:
    """Submit a single segment to Seedance and return task_id."""
    # Always use reference2video — principle: 如无必要，勿增实体
    api_type = "reference2video"
    prompt = seg.get("prompt", "")[:300]
    duration = seg.get("duration", 15)
    duration = max(4, min(15, duration))
    ratio = seg.get("ratio", "9:16")
    image_urls = seg.get("image_urls", [])

    if api_type == "text2video" or not image_urls:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt,
            "duration": str(duration),
            "ratio": ratio,
            "generate_audio": True,
        }
        result = seedance_request("/jimeng/text2video", params)
    elif api_type == "image2video" and len(image_urls) == 1:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt,
            "image_urls": image_urls,
            "duration": str(duration),
            "ratio": ratio,
            "generate_audio": True,
        }
        result = seedance_request("/jimeng/image2video", params)
    else:
        params = {
            "model_id": MODEL_MAP[DEFAULT_MODEL],
            "prompt": prompt,
            "duration": str(duration),
            "ratio": ratio,
            "generate_audio": True,
        }
        if image_urls:
            params["image_urls"] = image_urls
        result = seedance_request("/jimeng/reference2video", params)

    seedance_task_id = result.get("task_id")
    if not seedance_task_id:
        raise HTTPException(500, f"No task_id from Seedance: {json.dumps(result, ensure_ascii=False)}")

    # Register in tasks_store for polling
    tasks_store[seedance_task_id] = {
        "task_id": seedance_task_id,
        "type": api_type,
        "status": "submitted",
        "prompt": prompt[:100],
        "params": {"duration": duration, "ratio": ratio},
        "result": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    return seedance_task_id


@app.post("/api/content-tasks/{task_id}/submit")
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


@app.post("/api/content-tasks/{task_id}/segments/{seg_id}/submit")
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

    # Update parent task status
    statuses = {s["status"] for s in task["segments"]}
    if statuses <= {"completed"}:
        task["status"] = "completed"
    elif "submitted" in statuses or "generating" in statuses:
        task["status"] = "generating"
    task["updated_at"] = time.time()
    save_content_tasks()
    return {"success": True, "seedance_task_id": seedance_id, "segment_id": seg_id}


@app.post("/api/content-tasks/{task_id}/segments/{seg_id}/status")
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

    # Recompute parent status
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


@app.post("/api/content-tasks/{task_id}/concat")
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

    tmpdir = tempfile.mkdtemp(prefix="concat_")
    try:
        # Download all segment videos
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

        # Build concat list
        list_path = os.path.join(tmpdir, "list.txt")
        with open(list_path, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")

        # FFmpeg concat
        output_path = os.path.join(tmpdir, "final.mp4")
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"ffmpeg 失败: {result.stderr[:200]}"}

        # Upload to CDN
        final_name = f"concat_{task_id}_{int(time.time())}.mp4"
        try:
            cdn_url = await upload_local_to_cdn(output_path, final_name)
        except Exception:
            cdn_url = upload_url_to_cdn(f"file://{output_path}", wait=False)
            if cdn_url.startswith("file://"):
                cdn_url = ""

        if not cdn_url:
            return {"success": False, "error": "CDN 上传失败"}

        # Store final URL
        task["final_video_url"] = cdn_url
        task["updated_at"] = time.time()
        save_content_tasks()
        return {"success": True, "final_video_url": cdn_url}
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- Social Publish CRUD ---

PLATFORM_NAMES = {
    "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手",
    "weixin": "微信视频号", "bilibili": "B站", "weibo": "微博",
}


@app.get("/api/social-publish")
async def list_social_publish(platform: Optional[str] = None, brand: Optional[str] = None):
    """List social publish records with optional filtering."""
    records = list(social_publish_store.values())
    if platform:
        records = [r for r in records if r["platform"] == platform]
    if brand:
        records = [r for r in records if r.get("brand") == brand]
    records.sort(key=lambda r: r.get("published_at", ""), reverse=True)
    return {"success": True, "records": records}


@app.post("/api/social-publish")
async def create_social_publish(request: Request):
    """Create a new social publish record."""
    body = await request.json()
    publish_id = str(uuid.uuid4())[:8]
    task_id = body.get("task_id", "")

    # Auto-fill from content task if available
    task = content_tasks_store.get(task_id)
    brand = body.get("brand", "")
    sku = body.get("sku", "")
    title = body.get("title", "")
    video_url = body.get("video_url", "")
    if task and not brand:
        brand = task.get("brand", "")
    if task and not sku:
        sku = task.get("sku", "")
    if task and not title:
        title = task.get("title", "")
    if task and not video_url:
        segs = task.get("segments", [])
        for s in segs:
            if s.get("video_url"):
                video_url = s["video_url"]
                break

    record = {
        "publish_id": publish_id,
        "task_id": task_id,
        "segment_ids": body.get("segment_ids", []),
        "platform": body.get("platform", "douyin"),
        "account_name": body.get("account_name", ""),
        "published_url": body.get("published_url", ""),
        "published_at": body.get("published_at", time.strftime("%Y-%m-%d")),
        "metrics": body.get("metrics", {
            "views": 0, "likes": 0, "comments": 0,
            "shares": 0, "favorites": 0, "completion_rate": 0,
        }),
        "updated_at": time.time(),
        "brand": brand,
        "sku": sku,
        "title": title,
        "video_url": video_url,
        "notes": body.get("notes", ""),
    }
    social_publish_store[publish_id] = record
    save_social_publish()
    return {"success": True, "record": record}


@app.put("/api/social-publish/{publish_id}")
async def update_social_publish(publish_id: str, request: Request):
    """Update a social publish record."""
    record = social_publish_store.get(publish_id)
    if not record:
        raise HTTPException(404, f"Publish record not found: {publish_id}")
    body = await request.json()
    for key in ("platform", "account_name", "published_url", "published_at",
                "metrics", "notes", "segment_ids"):
        if key in body:
            record[key] = body[key]
    record["updated_at"] = time.time()
    save_social_publish()
    return {"success": True, "record": record}


@app.delete("/api/social-publish/{publish_id}")
async def delete_social_publish(publish_id: str):
    """Delete a social publish record."""
    if publish_id not in social_publish_store:
        raise HTTPException(404, f"Publish record not found: {publish_id}")
    del social_publish_store[publish_id]
    save_social_publish()
    return {"success": True}


def _parse_num(v, default=0):
    """Parse number from strings like '¥1,234.5', '12.3%', '-', '0秒', '26.12万'."""
    if not v or v.strip() in ("-", "--", ""):
        return default
    s = v.strip().replace("¥", "").replace(",", "").replace("%", "").replace("秒", "")
    multiplier = 1
    if s.endswith("万"):
        s = s[:-1]
        multiplier = 10000
    elif s.endswith("亿"):
        s = s[:-1]
        multiplier = 100000000
    try:
        return float(s) * multiplier
    except (ValueError, TypeError):
        return default


def _is_qianchuan_csv(headers):
    """Detect if CSV is from 千川/巨量千川 by checking for signature columns."""
    return "作品ID" in headers and "观看次数" in headers


@app.post("/api/social-publish/import-csv")
async def import_social_csv(file: UploadFile = File(...)):
    """Import social publish data from CSV. Supports both standard format and 千川 format."""
    import csv
    import io
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    is_qc = _is_qianchuan_csv(headers)
    imported = []
    skipped = 0

    # 构建已有数据指纹集用于去重 (title + date + account)
    existing_fps = set()
    for r in social_publish_store.values():
        fp = (r.get("title", ""), r.get("published_at", ""), r.get("account_name", ""))
        existing_fps.add(fp)

    for row in reader:
        publish_id = str(uuid.uuid4())[:8]

        if is_qc:
            # --- 千川格式映射 ---
            title = row.get("作品标题", "").strip()
            published_url = row.get("播放链接", "").strip()
            account_name = row.get("达人昵称", "").strip()
            douyin_id = row.get("达人抖音号", "").strip()
            raw_date = row.get("发布时间", "").strip()
            published_at = raw_date[:10].replace("/", "-") if raw_date else time.strftime("%Y-%m-%d")
            views = int(_parse_num(row.get("观看次数", 0)))
            likes = int(_parse_num(row.get("点赞数", 0)))
            comments = int(_parse_num(row.get("评论数", 0)))
            shares = int(_parse_num(row.get("转发数", 0)))
            favorites = int(_parse_num(row.get("收藏数", 0)))
            follows = int(_parse_num(row.get("点击关注次数", 0)))
            completion_pct = _parse_num(row.get("完播率", 0))
            completion_rate = completion_pct / 100.0 if completion_pct > 1 else completion_pct
            avg_watch = row.get("平均观看时长", "").strip()
            duration = row.get("作品时长", "").strip()
            # 电商引流数据
            gmv = _parse_num(row.get("直接成交金额", 0))
            orders = int(_parse_num(row.get("成交订单数", 0)))
            product_exposure = int(_parse_num(row.get("商品曝光次数", 0)))
            product_clicks = int(_parse_num(row.get("商品点击次数", 0)))
            gpm = _parse_num(row.get("千次观看成交金额", 0))
            divert_gmv = _parse_num(row.get("引流成交金额", 0))
            divert_orders = int(_parse_num(row.get("引流成交订单数", 0)))
            record = {
                "publish_id": publish_id, "task_id": "", "segment_ids": [],
                "platform": "douyin",
                "account_name": f"{account_name}({douyin_id})" if douyin_id else account_name,
                "published_url": published_url,
                "published_at": published_at,
                "metrics": {
                    "views": views, "likes": likes, "comments": comments,
                    "shares": shares, "favorites": favorites,
                    "completion_rate": round(completion_rate, 4),
                    "follows": follows,
                    "avg_watch": avg_watch,
                    "duration": duration,
                    "gmv": gmv,
                    "orders": orders,
                    "product_exposure": product_exposure,
                    "product_clicks": product_clicks,
                    "gpm": gpm,
                    "divert_gmv": divert_gmv,
                    "divert_orders": divert_orders,
                },
                "updated_at": time.time(),
                "brand": "", "sku": "", "title": title, "video_url": "",
                "notes": f"作品ID: {row.get('作品ID', '')}",
                "extra": {
                    "作品ID": row.get("作品ID", ""),
                    "作品类型": row.get("作品类型", ""),
                },
            }
        else:
            # --- 标准格式 ---
            task_id = row.get("task_id", "").strip()
            task = content_tasks_store.get(task_id)
            brand = row.get("brand", "").strip()
            sku = row.get("sku", "").strip()
            title = row.get("title", "").strip()
            video_url = ""
            if task:
                brand = brand or task.get("brand", "")
                sku = sku or task.get("sku", "")
                title = title or task.get("title", "")
                for s in task.get("segments", []):
                    if s.get("video_url"):
                        video_url = s["video_url"]; break
            record = {
                "publish_id": publish_id, "task_id": task_id, "segment_ids": [],
                "platform": row.get("platform", "douyin").strip(),
                "account_name": row.get("account_name", "").strip(),
                "published_url": row.get("published_url", "").strip(),
                "published_at": row.get("published_at", time.strftime("%Y-%m-%d")).strip(),
                "metrics": {
                    "views": int(_parse_num(row.get("views", 0))),
                    "likes": int(_parse_num(row.get("likes", 0))),
                    "comments": int(_parse_num(row.get("comments", 0))),
                    "shares": int(_parse_num(row.get("shares", 0))),
                    "favorites": int(_parse_num(row.get("favorites", 0))),
                    "completion_rate": _parse_num(row.get("completion_rate", 0)),
                },
                "updated_at": time.time(),
                "brand": brand, "sku": sku, "title": title, "video_url": video_url,
                "notes": row.get("notes", "").strip(),
            }

        # 去重检查
        fp = (record.get("title", ""), record.get("published_at", ""), record.get("account_name", ""))
        if fp in existing_fps:
            skipped += 1
            continue
        existing_fps.add(fp)

        social_publish_store[publish_id] = record
        imported.append(publish_id)

    save_social_publish()
    return {"success": True, "imported_count": len(imported), "skipped_duplicates": skipped, "ids": imported, "format": "qianchuan" if is_qc else "standard"}


# --- Ad Campaigns CRUD ---

AD_PLATFORMS = {
    "juliang": "巨量引擎", "ciliyinqing": "磁力引擎", "tengxun": "腾讯广告",
    "jvguang": "聚光平台", "bilibili": "B站花火", "baidu": "百度信息流",
}


@app.get("/api/ad-campaigns")
async def list_ad_campaigns(platform: Optional[str] = None, brand: Optional[str] = None):
    records = list(ad_campaigns_store.values())
    # 补算衍生指标（确保旧数据也有完整指标）
    for r in records:
        if "metrics" in r:
            r["metrics"] = _derive_ad_metrics(r["metrics"])
    if platform:
        records = [r for r in records if r["platform"] == platform]
    if brand:
        records = [r for r in records if r.get("brand") == brand]
    records.sort(key=lambda r: r.get("date_start", ""), reverse=True)
    return {"success": True, "records": records}


@app.post("/api/ad-campaigns")
async def create_ad_campaign(request: Request):
    body = await request.json()
    cid = str(uuid.uuid4())[:8]
    task_id = body.get("task_id", "")
    task = content_tasks_store.get(task_id)

    brand = body.get("brand", "")
    sku = body.get("sku", "")
    title = body.get("title", "")
    video_url = body.get("video_url", "")
    if task:
        brand = brand or task.get("brand", "")
        sku = sku or task.get("sku", "")
        title = title or task.get("title", "")
        if not video_url:
            for s in task.get("segments", []):
                if s.get("video_url"):
                    video_url = s["video_url"]
                    break

    record = {
        "campaign_id": cid,
        "task_id": task_id,
        "platform": body.get("platform", "juliang"),
        "campaign_name": body.get("campaign_name", ""),
        "creative_name": body.get("creative_name", ""),
        "account_name": body.get("account_name", ""),
        "date_start": body.get("date_start", time.strftime("%Y-%m-%d")),
        "date_end": body.get("date_end", ""),
        "budget": float(body.get("budget", 0)),
        "metrics": body.get("metrics", {
            "spend": 0, "impressions": 0, "clicks": 0,
            "ctr": 0, "cpc": 0, "cpm": 0,
            "conversions": 0, "cvr": 0, "cpa": 0, "roas": 0,
        }),
        "updated_at": time.time(),
        "brand": brand, "sku": sku, "title": title, "video_url": video_url,
        "notes": body.get("notes", ""),
    }
    ad_campaigns_store[cid] = record
    save_ad_campaigns()
    return {"success": True, "record": record}


@app.put("/api/ad-campaigns/{cid}")
async def update_ad_campaign(cid: str, request: Request):
    record = ad_campaigns_store.get(cid)
    if not record:
        raise HTTPException(404, f"Ad campaign not found: {cid}")
    body = await request.json()
    for key in ("platform", "campaign_name", "creative_name", "account_name",
                "date_start", "date_end", "budget", "metrics", "notes"):
        if key in body:
            record[key] = body[key]
    record["updated_at"] = time.time()
    save_ad_campaigns()
    return {"success": True, "record": record}


@app.delete("/api/ad-campaigns/{cid}")
async def delete_ad_campaign(cid: str):
    if cid not in ad_campaigns_store:
        raise HTTPException(404, f"Ad campaign not found: {cid}")
    del ad_campaigns_store[cid]
    save_ad_campaigns()
    return {"success": True}


@app.post("/api/ad-campaigns/import-csv")
async def import_ad_csv(file: UploadFile = File(...)):
    """Import ad campaign data from CSV. Supports both standard format and 千川 format."""
    import csv
    import io
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    is_qc = _is_qianchuan_csv(headers)
    imported = []
    skipped = 0

    # 构建已有数据指纹集用于去重 (title + date + account)
    existing_fps = set()
    for r in ad_campaigns_store.values():
        fp = (r.get("creative_name", "") or r.get("title", ""), r.get("date_start", ""), r.get("account_name", ""))
        existing_fps.add(fp)

    for row in reader:
        cid = str(uuid.uuid4())[:8]

        if is_qc:
            # --- 千川格式映射 ---
            title = row.get("作品标题", "").strip()
            account_name = row.get("达人昵称", "").strip()
            douyin_id = row.get("达人抖音号", "").strip()
            raw_date = row.get("发布时间", "").strip()
            date_str = raw_date[:10].replace("/", "-") if raw_date else time.strftime("%Y-%m-%d")
            spend = _parse_num(row.get("直接成交金额", 0))
            views = int(_parse_num(row.get("观看次数", 0)))
            clicks = int(_parse_num(row.get("商品点击次数", 0)))
            impressions = int(_parse_num(row.get("商品曝光次数", 0)))
            conversions = int(_parse_num(row.get("成交订单数", 0)))
            gmv = _parse_num(row.get("直接成交金额", 0))
            ctr = _parse_num(row.get("商品曝光点击率（次数）", 0)) / 100.0 if _parse_num(row.get("商品曝光点击率（次数）", 0)) > 1 else _parse_num(row.get("商品曝光点击率（次数）", 0))
            cvr = _parse_num(row.get("商品点击成交率（次数）", 0)) / 100.0 if _parse_num(row.get("商品点击成交率（次数）", 0)) > 1 else _parse_num(row.get("商品点击成交率（次数）", 0))
            gpm = _parse_num(row.get("千次观看成交金额", 0))

            # Skip rows with zero views and zero GMV (no meaningful data)
            if views == 0 and gmv == 0 and conversions == 0:
                continue

            published_url = row.get("播放链接", "").strip()
            record = {
                "campaign_id": cid, "task_id": "",
                "platform": "juliang",
                "campaign_name": title[:40] if title else "",
                "creative_name": title,
                "account_name": f"{account_name}({douyin_id})" if douyin_id else account_name,
                "published_url": published_url,
                "date_start": date_str, "date_end": date_str,
                "budget": 0,
                "metrics": {
                    "spend": spend,
                    "impressions": impressions,
                    "clicks": clicks,
                    "ctr": round(ctr, 4),
                    "cpc": round(spend / clicks, 2) if clicks > 0 else 0,
                    "cpm": round(spend / impressions * 1000, 2) if impressions > 0 else 0,
                    "conversions": conversions,
                    "cvr": round(cvr, 4),
                    "cpa": round(spend / conversions, 2) if conversions > 0 else 0,
                    "roas": round(gmv / spend, 2) if spend > 0 else 0,
                    # 千川特有指标
                    "gmv": gmv,
                    "gpm": gpm,
                },
                "updated_at": time.time(),
                "brand": "", "sku": "", "title": title, "video_url": "",
                "notes": f"作品ID: {row.get('作品ID', '')}",
                "extra": {
                    "作品ID": row.get("作品ID", ""),
                    "播放链接": row.get("播放链接", ""),
                    "发货金额": _parse_num(row.get("发货金额", 0)),
                    "退款金额": _parse_num(row.get("退款金额", 0)),
                    "T7结算金额": _parse_num(row.get("T-7结算金额", 0)),
                    "预估佣金收入": _parse_num(row.get("预估佣金收入", 0)),
                },
            }
        else:
            # --- 标准格式 ---
            task_id = row.get("task_id", "").strip()
            task = content_tasks_store.get(task_id)
            brand = row.get("brand", "").strip()
            sku = row.get("sku", "").strip()
            title = row.get("title", "").strip()
            video_url = ""
            if task:
                brand = brand or task.get("brand", "")
                sku = sku or task.get("sku", "")
                title = title or task.get("title", "")
                for s in task.get("segments", []):
                    if s.get("video_url"):
                        video_url = s["video_url"]; break
            record = {
                "campaign_id": cid, "task_id": task_id,
                "platform": row.get("platform", "juliang").strip(),
                "campaign_name": row.get("campaign_name", "").strip(),
                "creative_name": row.get("creative_name", "").strip(),
                "account_name": row.get("account_name", "").strip(),
                "date_start": row.get("date_start", "").strip(),
                "date_end": row.get("date_end", "").strip(),
                "budget": _parse_num(row.get("budget", 0)),
                "metrics": {
                    "spend": _parse_num(row.get("spend", 0)),
                    "impressions": int(_parse_num(row.get("impressions", 0))),
                    "clicks": int(_parse_num(row.get("clicks", 0))),
                    "ctr": _parse_num(row.get("ctr", 0)),
                    "cpc": _parse_num(row.get("cpc", 0)),
                    "cpm": _parse_num(row.get("cpm", 0)),
                    "conversions": int(_parse_num(row.get("conversions", 0))),
                    "cvr": _parse_num(row.get("cvr", 0)),
                    "cpa": _parse_num(row.get("cpa", 0)),
                    "roas": _parse_num(row.get("roas", 0)),
                },
                "updated_at": time.time(),
                "brand": brand, "sku": sku, "title": title, "video_url": video_url,
                "notes": row.get("notes", "").strip(),
            }

        # 补全衍生指标
        record["metrics"] = _derive_ad_metrics(record["metrics"])
        # 去重检查
        fp = (record.get("creative_name", "") or record.get("title", ""), record.get("date_start", ""), record.get("account_name", ""))
        if fp in existing_fps:
            skipped += 1
            continue
        existing_fps.add(fp)

        ad_campaigns_store[cid] = record
        imported.append(cid)

    save_ad_campaigns()
    return {"success": True, "imported_count": len(imported), "skipped_duplicates": skipped, "ids": imported, "format": "qianchuan" if is_qc else "standard"}


# ---------------------------------------------------------------------------
# Smart CSV Import — LLM-driven field mapping + derived metrics
# ---------------------------------------------------------------------------

SOCIAL_SCHEMA_FIELDS = (
    "title, published_url, platform, account_name, published_at, "
    "views, likes, comments, shares, favorites, follows, completion_rate, "
    "duration, avg_watch_time, gmv, orders, product_exposure, product_clicks, "
    "gpm, divert_gmv, divert_orders"
)
AD_SCHEMA_FIELDS = (
    "title, published_url, platform, account_name, campaign_name, "
    "date_start, date_end, budget, spend, impressions, clicks, conversions, "
    "gmv, orders, views"
)


def _detect_encoding(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def _derive_social_metrics(m: dict) -> dict:
    views = m.get("views", 0) or 0
    likes = m.get("likes", 0) or 0
    comments = m.get("comments", 0) or 0
    shares = m.get("shares", 0) or 0
    favorites = m.get("favorites", 0) or 0
    interact = likes + comments + shares + favorites
    m["interact_total"] = interact
    m["interact_rate"] = round(interact / views, 4) if views > 0 else 0
    if views > 0 and m.get("gmv", 0):
        m["gpm"] = m.get("gpm") or round(m["gmv"] / views * 1000, 2)
    return m


def _derive_ad_metrics(m: dict) -> dict:
    spend = m.get("spend", 0) or 0
    impressions = m.get("impressions", 0) or 0
    clicks = m.get("clicks", 0) or 0
    conversions = m.get("conversions", 0) or 0
    gmv = m.get("gmv", 0) or 0
    views = m.get("views", 0) or 0
    # CTR = 点击/展现
    m["ctr"] = m.get("ctr") or (round(clicks / impressions, 4) if impressions > 0 else 0)
    # CPC = 消耗/点击  (也可由 CPM/CTR 推导)
    m["cpc"] = m.get("cpc") or (round(spend / clicks, 2) if clicks > 0 else 0)
    # CPM = 消耗/展现*1000
    m["cpm"] = m.get("cpm") or (round(spend / impressions * 1000, 2) if impressions > 0 else 0)
    # CVR = 转化/点击
    m["cvr"] = m.get("cvr") or (round(conversions / clicks, 4) if clicks > 0 else 0)
    # CPA = 消耗/转化  (也可由 CPC/CVR 推导)
    m["cpa"] = m.get("cpa") or (round(spend / conversions, 2) if conversions > 0 else 0)
    # ROAS = GMV/消耗
    m["roas"] = m.get("roas") or (round(gmv / spend, 2) if spend > 0 else 0)
    # GPM = GMV/播放*1000
    m["gpm"] = m.get("gpm") or (round(gmv / views * 1000, 2) if views > 0 else 0)
    # CPV = 消耗/播放
    m["cpv"] = m.get("cpv") or (round(spend / views, 4) if views > 0 else 0)
    return m


async def _llm_map_csv(headers: list, samples: list, target: str) -> dict:
    schema = SOCIAL_SCHEMA_FIELDS if target == "social" else AD_SCHEMA_FIELDS
    sample_text = ""
    for i, row in enumerate(samples):
        sample_text += f"行{i+1}: {row}\n"

    prompt = f"""你是数据工程师。分析这个CSV的列名和样本数据，将每列映射到标准字段。

CSV表头（共{len(headers)}列）: {headers}
样本数据:
{sample_text}
标准字段: {schema}

字段映射提示（中文列名 → 标准字段）:
- 观看次数/播放量/播放次数 → views
- 点赞数/点赞 → likes
- 评论数/评论 → comments
- 转发数/分享数/分享 → shares
- 收藏数/收藏 → favorites
- 点击关注次数/新增关注/关注 → follows
- 完播率 → completion_rate
- 作品时长/时长 → duration
- 平均观看时长/平均播放时长 → avg_watch_time
- 直接成交金额/成交金额 → gmv
- 成交订单数/订单数 → orders
- 商品曝光次数/商品曝光 → product_exposure
- 商品点击次数/商品点击 → product_clicks
- 千次观看成交金额/GPM → gpm
- 引流成交金额/引流GMV → divert_gmv
- 引流成交订单数/引流订单 → divert_orders
- 作品标题/标题 → title
- 播放链接/视频链接 → published_url
- 发布时间/发布日期 → published_at (社媒) 或 date_start (投放)
- 达人昵称/账户名称/账号 → account_name
- 消耗/花费/消费 → spend
- 展现量/展现次数/曝光 → impressions
- 点击量/点击次数 → clicks
- 转化数/转化次数 → conversions

返回JSON（只返回JSON，不要解释）:
{{
  "platform": "推断的平台标识(douyin/xiaohongshu/kuaishou/weixin/bilibili/weibo/juliang/jvguang/tengxun/ciliyinqing/baidu)",
  "mapping": {{ "CSV列名": "标准字段名", ... }},
  "unmapped": ["无法映射的CSV列名"],
  "notes": "简短说明"
}}

规则:
- 数值字段可能包含 ¥、%、万、亿、秒 等符号，映射时忽略
- 只映射有对应标准字段的列，其余放 unmapped
- platform 根据数据特征推断（如出现抖音链接则为 douyin）
- 尽量映射所有能对应的列，不要遗漏"""

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{CLAUDE_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {CLAUDE_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": CLAUDE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Extract JSON from possible markdown code block
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(content)
        # Normalize platform name to English ID
        plat_map = {"抖音": "douyin", "快手": "kuaishou", "小红书": "xiaohongshu",
                     "微信": "weixin", "微博": "weibo", "B站": "bilibili",
                     "千川": "douyin", "巨量引擎": "juliang", "聚光": "jvguang",
                     "腾讯广告": "tengxun", "磁力引擎": "ciliyinqing", "百度": "baidu"}
        raw_plat = result.get("platform", "")
        for cn, en in plat_map.items():
            if cn in raw_plat:
                result["platform"] = en
                break
        return result


@app.post("/api/import/smart-csv")
async def smart_csv_import(target: str = "social", file: UploadFile = File(...)):
    """SSE streaming: upload CSV → LLM maps fields → process rows → derive metrics → AI summary."""
    import csv as csv_mod
    import io

    raw = await file.read()
    enc = _detect_encoding(raw)
    text = raw.decode(enc)
    reader = csv_mod.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    all_rows = list(reader)

    def sse(evt: str, data):
        return f"data: {json.dumps({'event': evt, **data} if isinstance(data, dict) else {'event': evt, 'text': data}, ensure_ascii=False)}\n\n"

    async def stream():
        if not headers:
            yield sse("error", {"message": "CSV 文件为空或格式错误"})
            yield "data: [DONE]\n\n"
            return

        # Step 1: encoding + basic info
        yield sse("progress", f"📂 检测到文件编码: {enc.upper()}，共 {len(all_rows)} 行数据，{len(headers)} 列")
        yield sse("progress", f"📋 表头: {', '.join(headers[:8])}{'...' if len(headers) > 8 else ''}")

        # Step 2: LLM mapping
        yield sse("progress", "🤖 正在调用 AI 分析字段映射...")
        sample_rows = [[row.get(h, "") for h in headers] for row in all_rows[:3]]
        try:
            mapping_result = await _llm_map_csv(headers, sample_rows, target)
        except Exception as e:
            yield sse("error", {"message": f"AI 映射失败: {str(e)}"})
            yield "data: [DONE]\n\n"
            return

        mapping = mapping_result.get("mapping", {})
        platform_detected = mapping_result.get("platform", "douyin")
        unmapped = mapping_result.get("unmapped", [])
        ai_notes = mapping_result.get("notes", "")

        mapped_count = len(mapping)
        yield sse("mapping", {"platform": platform_detected, "mapped": mapped_count, "unmapped_count": len(unmapped)})
        yield sse("progress", f"✅ AI 识别为「{platform_detected}」平台，成功映射 {mapped_count} 个字段")
        if ai_notes:
            yield sse("progress", f"💡 {ai_notes}")

        # Step 3: process rows
        yield sse("progress", f"⚙️ 开始处理 {len(all_rows)} 行数据...")
        rev = {std: csv_col for csv_col, std in mapping.items() if std}
        mapped_csv_cols = set(mapping.keys())
        imported = []
        skipped = 0
        derived_count = 0
        stats = {"total_views": 0, "total_interact": 0, "total_gmv": 0, "total_orders": 0,
                 "total_spend": 0, "total_conversions": 0, "max_views": 0, "max_title": ""}

        # 构建已有数据指纹集用于去重
        existing_fps = set()
        if target == "social":
            for r in social_publish_store.values():
                existing_fps.add((r.get("title", ""), r.get("published_at", ""), r.get("account_name", "")))
        else:
            for r in ad_campaigns_store.values():
                existing_fps.add((r.get("creative_name", "") or r.get("title", ""), r.get("date_start", ""), r.get("account_name", "")))

        for idx, row in enumerate(all_rows):
            def get(field, default=""):
                col = rev.get(field)
                return row.get(col, default) if col else default

            if target == "social":
                pid = str(uuid.uuid4())[:8]
                views = int(_parse_num(get("views", "0")))
                likes = int(_parse_num(get("likes", "0")))
                comments = int(_parse_num(get("comments", "0")))
                shares = int(_parse_num(get("shares", "0")))
                favorites = int(_parse_num(get("favorites", "0")))
                follows = int(_parse_num(get("follows", "0")))
                comp = _parse_num(get("completion_rate", "0"))
                completion_rate = comp / 100.0 if comp > 1 else comp
                raw_date = get("published_at", "").strip()
                published_at = raw_date[:10].replace("/", "-") if raw_date else time.strftime("%Y-%m-%d")
                metrics = {
                    "views": views, "likes": likes, "comments": comments,
                    "shares": shares, "favorites": favorites, "follows": follows,
                    "completion_rate": round(completion_rate, 4),
                    "duration": get("duration", "").replace("秒", "").strip() or "",
                    "avg_watch_time": get("avg_watch_time", "").replace("秒", "").strip() or "",
                    "gmv": _parse_num(get("gmv", "0")),
                    "orders": int(_parse_num(get("orders", "0"))),
                    "product_exposure": int(_parse_num(get("product_exposure", "0"))),
                    "product_clicks": int(_parse_num(get("product_clicks", "0"))),
                    "gpm": _parse_num(get("gpm", "0")),
                    "divert_gmv": _parse_num(get("divert_gmv", "0")),
                    "divert_orders": int(_parse_num(get("divert_orders", "0"))),
                }
                metrics = _derive_social_metrics(metrics)
                if metrics.get("interact_rate", 0) > 0:
                    derived_count += 1
                extra = {"ai_mapped": True}
                for col in headers:
                    if col not in mapped_csv_cols and row.get(col, "").strip():
                        extra[col] = row[col].strip()
                record = {
                    "publish_id": pid, "task_id": "", "segment_ids": [],
                    "platform": platform_detected,
                    "account_name": get("account_name", "").strip(),
                    "published_url": get("published_url", "").strip(),
                    "published_at": published_at,
                    "metrics": metrics, "updated_at": time.time(),
                    "brand": "", "sku": "", "title": get("title", "").strip(),
                    "video_url": "", "notes": "",
                    "extra": extra,
                }
                # 去重检查
                fp = (record.get("title", ""), record.get("published_at", ""), record.get("account_name", ""))
                if fp in existing_fps:
                    skipped += 1
                    continue
                existing_fps.add(fp)

                social_publish_store[pid] = record
                imported.append(pid)
                # Update stats
                stats["total_views"] += views
                stats["total_interact"] += metrics.get("interact_total", 0)
                stats["total_gmv"] += metrics.get("gmv", 0) + metrics.get("divert_gmv", 0)
                stats["total_orders"] += metrics.get("orders", 0) + metrics.get("divert_orders", 0)
                if views > stats["max_views"]:
                    stats["max_views"] = views
                    stats["max_title"] = get("title", "")[:30]
            else:
                cid = str(uuid.uuid4())[:8]
                raw_date = get("date_start", "").strip()
                date_str = raw_date[:10].replace("/", "-") if raw_date else time.strftime("%Y-%m-%d")
                raw_end = get("date_end", "").strip()
                date_end = raw_end[:10].replace("/", "-") if raw_end else date_str
                spend = _parse_num(get("spend", "0"))
                impressions = int(_parse_num(get("impressions", "0")))
                clicks = int(_parse_num(get("clicks", "0")))
                conversions = int(_parse_num(get("conversions", "0")))
                gmv = _parse_num(get("gmv", "0"))
                views = int(_parse_num(get("views", "0")))
                metrics = {
                    "spend": spend, "impressions": impressions, "clicks": clicks,
                    "conversions": conversions, "gmv": gmv, "views": views,
                    "ctr": 0, "cpc": 0, "cpm": 0, "cvr": 0, "cpa": 0, "roas": 0, "gpm": 0,
                }
                metrics = _derive_ad_metrics(metrics)
                if metrics.get("roas", 0) > 0 or metrics.get("ctr", 0) > 0:
                    derived_count += 1
                extra_ad = {"ai_mapped": True}
                for col in headers:
                    if col not in mapped_csv_cols and row.get(col, "").strip():
                        extra_ad[col] = row[col].strip()
                record = {
                    "campaign_id": cid, "task_id": "",
                    "platform": platform_detected,
                    "campaign_name": get("campaign_name", "").strip() or get("title", "").strip()[:40],
                    "creative_name": get("title", "").strip(),
                    "account_name": get("account_name", "").strip(),
                    "published_url": get("published_url", "").strip(),
                    "date_start": date_str, "date_end": date_end,
                    "budget": _parse_num(get("budget", "0")),
                    "metrics": metrics, "updated_at": time.time(),
                    "brand": "", "sku": "", "title": get("title", "").strip(),
                    "video_url": "", "notes": "",
                    "extra": extra_ad,
                }
                # 去重检查
                fp = (record.get("creative_name", "") or record.get("title", ""), record.get("date_start", ""), record.get("account_name", ""))
                if fp in existing_fps:
                    skipped += 1
                    continue
                existing_fps.add(fp)

                ad_campaigns_store[cid] = record
                imported.append(cid)
                stats["total_spend"] += spend
                stats["total_gmv"] += gmv
                stats["total_conversions"] += conversions
                stats["total_views"] += views
                stats["total_impressions"] = stats.get("total_impressions", 0) + impressions
                stats["total_clicks"] = stats.get("total_clicks", 0) + clicks
                if spend > 0 and gmv > 0:
                    stats["avg_roas"] = stats.get("_roas_sum", 0) + gmv
                    stats["_roas_spend"] = stats.get("_roas_spend", 0) + spend

            # Progress every 100 rows
            if (idx + 1) % 100 == 0:
                yield sse("progress", f"⚙️ 已处理 {idx + 1}/{len(all_rows)} 行...")

        # Step 4: save
        # Compute avg ROAS for ad target
        if target == "ad" and stats.get("_roas_spend", 0) > 0:
            stats["avg_roas"] = round(stats.get("avg_roas", 0) / stats["_roas_spend"], 2)
        stats.pop("_roas_sum", None)
        stats.pop("_roas_spend", None)

        if target == "social":
            save_social_publish()
        else:
            save_ad_campaigns()

        yield sse("progress", f"💾 数据入库完成: {len(imported)} 条记录{f'，跳过 {skipped} 条重复' if skipped else ''}，计算了 {derived_count} 条衍生指标")
        yield sse("result", {
            "imported_count": len(imported),
            "skipped_duplicates": skipped,
            "platform": platform_detected,
            "mapped": mapped_count,
            "derived_count": derived_count,
            "stats": stats,
        })

        # Step 5: LLM summary
        yield sse("progress", "🧠 正在生成 AI 数据摘要...")
        summary_prompt = _build_summary_prompt(target, stats, len(imported), platform_detected, derived_count)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "POST", f"{CLAUDE_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {CLAUDE_API_KEY}", "Content-Type": "application/json"},
                    json={"model": CLAUDE_MODEL, "messages": [{"role": "user", "content": summary_prompt}], "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield sse("summary", delta)
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            yield sse("summary", f"\n\n⚠️ AI 总结生成失败: {str(e)}")

        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def _build_summary_prompt(target, stats, count, platform, derived_count):
    if target == "social":
        return f"""你是品牌数据分析师。刚导入了一批社媒数据，请用中文给出简洁的数据摘要和初步洞察。

数据概况:
- 平台: {platform}
- 导入记录: {count} 条
- 总播放量: {stats['total_views']:,.0f}
- 总互动量: {stats['total_interact']:,.0f}
- 总引流GMV: ¥{stats['total_gmv']:,.2f}
- 总引流订单: {stats['total_orders']}
- 最高播放作品: {stats['max_title']}（{stats['max_views']:,.0f}次）
- 自动计算衍生指标: {derived_count} 条

请输出:
1. **数据概览** — 一句话总结
2. **关键发现** — 2-3 个数据亮点或异常
3. **建议** — 基于数据的 1-2 条可执行建议
保持简洁，不超过200字。"""
    else:
        roas = round(stats['total_gmv'] / stats['total_spend'], 2) if stats['total_spend'] > 0 else 0
        return f"""你是品牌投放分析师。刚导入了一批广告投放数据，请用中文给出简洁的数据摘要和初步洞察。

数据概况:
- 平台: {platform}
- 导入记录: {count} 条
- 总消耗: ¥{stats['total_spend']:,.2f}
- 总GMV: ¥{stats['total_gmv']:,.2f}
- 总转化: {stats['total_conversions']}
- 整体ROAS: {roas}
- 总播放/展现: {stats['total_views']:,.0f}
- 自动计算衍生指标: {derived_count} 条

请输出:
1. **数据概览** — 一句话总结
2. **关键发现** — 2-3 个数据亮点或异常
3. **建议** — 基于数据的 1-2 条可执行建议
保持简洁，不超过200字。"""


# Serve static files
static_dir = Path(__file__).parent
app.mount("/assets", StaticFiles(directory=str(static_dir / "public" / "assets")), name="assets")


@app.get("/")
async def serve_index():
    return FileResponse(str(static_dir / "creation.html"))


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
