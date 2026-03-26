"""Social media publish: CRUD + CSV import."""

import csv
import io
import time
import uuid
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Request

from backend.persistence import (
    social_publish_store, save_social_publish,
    content_tasks_store,
)
from backend.routers.smart_import import _derive_social_metrics

router = APIRouter(prefix="/api", tags=["social"])


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


@router.get("/social-publish")
async def list_social_publish(platform: Optional[str] = None, brand: Optional[str] = None):
    """List social publish records with optional filtering."""
    records = list(social_publish_store.values())
    for r in records:
        if "metrics" in r:
            r["metrics"] = _derive_social_metrics(r["metrics"])
    if platform:
        records = [r for r in records if r["platform"] == platform]
    if brand:
        records = [r for r in records if r.get("brand") == brand]
    records.sort(key=lambda r: r.get("published_at", ""), reverse=True)
    return {"success": True, "records": records}


@router.post("/social-publish")
async def create_social_publish(request: Request):
    """Create a new social publish record."""
    body = await request.json()
    publish_id = str(uuid.uuid4())[:8]
    task_id = body.get("task_id", "")

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
        "brand": brand, "sku": sku, "title": title, "video_url": video_url,
        "notes": body.get("notes", ""),
    }
    social_publish_store[publish_id] = record
    save_social_publish()
    return {"success": True, "record": record}


@router.put("/social-publish/{publish_id}")
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


@router.delete("/social-publish/{publish_id}")
async def delete_social_publish(publish_id: str):
    """Delete a social publish record."""
    if publish_id not in social_publish_store:
        raise HTTPException(404, f"Publish record not found: {publish_id}")
    del social_publish_store[publish_id]
    save_social_publish()
    return {"success": True}


@router.post("/social-publish/import-csv")
async def import_social_csv(file: UploadFile = File(...)):
    """Import social publish data from CSV. Supports both standard format and 千川 format."""
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    is_qc = _is_qianchuan_csv(headers)
    imported = []
    skipped = 0

    existing_fps = set()
    for r in social_publish_store.values():
        fp = (r.get("title", ""), r.get("published_at", ""), r.get("account_name", ""))
        existing_fps.add(fp)

    for row in reader:
        publish_id = str(uuid.uuid4())[:8]

        if is_qc:
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
                    "follows": follows, "avg_watch": avg_watch, "duration": duration,
                    "gmv": gmv, "orders": orders,
                    "product_exposure": product_exposure, "product_clicks": product_clicks,
                    "gpm": gpm, "divert_gmv": divert_gmv, "divert_orders": divert_orders,
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

        fp = (record.get("title", ""), record.get("published_at", ""), record.get("account_name", ""))
        if fp in existing_fps:
            skipped += 1
            continue
        existing_fps.add(fp)

        social_publish_store[publish_id] = record
        imported.append(publish_id)

    save_social_publish()
    return {"success": True, "imported_count": len(imported), "skipped_duplicates": skipped, "ids": imported, "format": "qianchuan" if is_qc else "standard"}
