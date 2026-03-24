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
- 总时长应覆盖完整脚本""",
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
    """Stream copy generation from Claude API."""
    # Build system prompt
    system = "你是专业的品牌内容策划师，为蓝色光标客户创作高质量的社媒内容。\n\n"
    if brand == "人源活力":
        system += f"品牌：人源活力（RHC）\n产品：{sku}\n"
        sku_descs = {
            "光子瓶": "光感焕肤精华（光子瓶）RHC Skin Renewal Protease Essence，磨砂玻璃滴管瓶，紫蓝→粉紫→乳白渐变外观，核心成分是皮肤修复蛋白酶，主打提亮肤色、细腻毛孔、修护敏感肌。",
            "冻干面膜": "焕活修护冻干面膜，冻干技术锁鲜活性成分，敷前加精华液激活，深层修护、紧致提亮。",
            "次抛精华": "鎏金抗衰次抛精华，单支独立密封，高浓度抗衰精华，主打抗皱紧致、淡化细纹。",
            "眼油": "玫瑰淡纹提拉眼油，精油质地好吸收，淡化眼周细纹、提拉紧致、消除浮肿。",
            "冻干球": "寡肽修护冻干球，球状冻干剂型，溶解后释放高浓度寡肽，深层修护受损肌肤。",
        }
        for k, v in sku_descs.items():
            if k in sku:
                system += f"产品描述：{v}\n"
                break
        system += "目标受众：25-35岁女性，高端护肤消费群体。\n"
    elif brand and sku:
        brand_info = brand_assets.get(brand, {})
        sku_info = brand_info.get(sku, {})
        if sku_info:
            system += f"品牌：{brand}\n产品：{sku_info.get('name', sku)}\n"
    system += f"\n创作类型："
    if mode == "xiaohongshu":
        system += "小红书种草文案，风格活泼，用emoji，有标题、正文、标签，真实不生硬，避免营销感太重。\n要求：字数200-300字，带话题标签。"
    elif mode == "short_video":
        system += "15秒短视频脚本，分镜头，有画面描述+旁白+字幕，节奏快，有记忆点。\n要求：分3-4个镜头，每个镜头配1-2句话。"
    else:
        system += "Seedance视频生成提示词，用中文详细描述画面内容、视觉风格、镜头语言，适合AI视频模型生成10-15秒竖屏视频。\n要求：包含景别、光线、运镜、色调、整体风格。"
    system += "\n\n重要：全部使用中文输出，不要使用英文。"

    system += f"\n\n用户需求：{prompt}"

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{CLAUDE_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {CLAUDE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            pass
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
    aspect_ratio: str = Form("9:16"),
    visual_style: str = Form(""),
):
    """Stream storyboard generation from LLM via SSE."""
    try:
        urls = json.loads(asset_urls) if isinstance(asset_urls, str) else asset_urls
    except json.JSONDecodeError:
        urls = []

    system = BRAND_SYSTEM_PROMPTS.get("storyboard", "")

    if brand:
        system += f"\n\n品牌：{brand}"
    if sku:
        system += f"\n产品：{sku}"
        sku_descs = {
            "光子瓶": "光感焕肤精华（光子瓶），磨砂玻璃滴管瓶，紫蓝→粉紫→乳白渐变外观，核心成分是皮肤修复蛋白酶。",
            "冻干面膜": "焕活修护冻干面膜，冻干技术锁鲜活性成分。",
            "次抛精华": "鎏金抗衰次抛精华，高浓度抗衰精华。",
            "眼油": "玫瑰淡纹提拉眼油，淡化眼周细纹、提拉紧致。",
            "冻干球": "寡肽修护冻干球，深层修护受损肌肤。",
        }
        for k, v in sku_descs.items():
            if k in sku:
                system += f"\n产品描述：{v}"
                break
    if urls:
        system += f"\n可用素材URL：\n" + "\n".join(urls)
    if aspect_ratio:
        system += f"\n默认画面比例：{aspect_ratio}"
    if visual_style:
        system += f"\n视觉风格要求：{visual_style}"

    user_message = f"请为以下脚本生成Seedance视频分镜方案：\n\n{script}"

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{CLAUDE_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {CLAUDE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_message},
                        ],
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            pass
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

    system = """你是一位顶级品牌数据策略分析师，服务于蓝色光标的「品牌AI内容工厂」平台。

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

    user_msg = f"""以下是当前品牌内容在各平台的表现数据汇总：

{data_summary}

{focus_prompts.get(focus, focus_prompts['comprehensive'])}"""

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{CLAUDE_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {CLAUDE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_msg},
                        ],
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
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

    status = "storyboard_ready" if segs else "draft"
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
async def update_segment_status(task_id: str, seg_id: str, status: str = Form(...), video_url: str = Form("")):
    """Update segment status (called by frontend after polling Seedance)."""
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


@app.post("/api/social-publish/import-csv")
async def import_social_csv(file: UploadFile = File(...)):
    """Import social publish data from CSV.
    Expected columns: task_id, platform, account_name, published_url, published_at,
    views, likes, comments, shares, favorites, completion_rate, notes
    """
    import csv
    import io
    content = await file.read()
    text = content.decode("utf-8-sig")  # Handle BOM
    reader = csv.DictReader(io.StringIO(text))
    imported = []
    for row in reader:
        task_id = row.get("task_id", "").strip()
        task = content_tasks_store.get(task_id)
        publish_id = str(uuid.uuid4())[:8]

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
                    video_url = s["video_url"]
                    break

        def to_float(v, default=0):
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        record = {
            "publish_id": publish_id,
            "task_id": task_id,
            "segment_ids": [],
            "platform": row.get("platform", "douyin").strip(),
            "account_name": row.get("account_name", "").strip(),
            "published_url": row.get("published_url", "").strip(),
            "published_at": row.get("published_at", time.strftime("%Y-%m-%d")).strip(),
            "metrics": {
                "views": int(to_float(row.get("views", 0))),
                "likes": int(to_float(row.get("likes", 0))),
                "comments": int(to_float(row.get("comments", 0))),
                "shares": int(to_float(row.get("shares", 0))),
                "favorites": int(to_float(row.get("favorites", 0))),
                "completion_rate": to_float(row.get("completion_rate", 0)),
            },
            "updated_at": time.time(),
            "brand": brand,
            "sku": sku,
            "title": title,
            "video_url": video_url,
            "notes": row.get("notes", "").strip(),
        }
        social_publish_store[publish_id] = record
        imported.append(publish_id)

    save_social_publish()
    return {"success": True, "imported_count": len(imported), "ids": imported}


# --- Ad Campaigns CRUD ---

AD_PLATFORMS = {
    "juliang": "巨量引擎", "ciliyinqing": "磁力引擎", "tengxun": "腾讯广告",
    "jvguang": "聚光平台", "bilibili": "B站花火", "baidu": "百度信息流",
}


@app.get("/api/ad-campaigns")
async def list_ad_campaigns(platform: Optional[str] = None, brand: Optional[str] = None):
    records = list(ad_campaigns_store.values())
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
    """CSV columns: task_id,platform,campaign_name,creative_name,account_name,
    date_start,date_end,budget,spend,impressions,clicks,ctr,cpc,cpm,
    conversions,cvr,cpa,roas,notes"""
    import csv
    import io
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    imported = []
    for row in reader:
        task_id = row.get("task_id", "").strip()
        task = content_tasks_store.get(task_id)
        cid = str(uuid.uuid4())[:8]

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
                    video_url = s["video_url"]
                    break

        def fl(v, d=0):
            try: return float(v)
            except (ValueError, TypeError): return d

        record = {
            "campaign_id": cid,
            "task_id": task_id,
            "platform": row.get("platform", "juliang").strip(),
            "campaign_name": row.get("campaign_name", "").strip(),
            "creative_name": row.get("creative_name", "").strip(),
            "account_name": row.get("account_name", "").strip(),
            "date_start": row.get("date_start", "").strip(),
            "date_end": row.get("date_end", "").strip(),
            "budget": fl(row.get("budget", 0)),
            "metrics": {
                "spend": fl(row.get("spend", 0)),
                "impressions": int(fl(row.get("impressions", 0))),
                "clicks": int(fl(row.get("clicks", 0))),
                "ctr": fl(row.get("ctr", 0)),
                "cpc": fl(row.get("cpc", 0)),
                "cpm": fl(row.get("cpm", 0)),
                "conversions": int(fl(row.get("conversions", 0))),
                "cvr": fl(row.get("cvr", 0)),
                "cpa": fl(row.get("cpa", 0)),
                "roas": fl(row.get("roas", 0)),
            },
            "updated_at": time.time(),
            "brand": brand, "sku": sku, "title": title, "video_url": video_url,
            "notes": row.get("notes", "").strip(),
        }
        ad_campaigns_store[cid] = record
        imported.append(cid)

    save_ad_campaigns()
    return {"success": True, "imported_count": len(imported), "ids": imported}


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
