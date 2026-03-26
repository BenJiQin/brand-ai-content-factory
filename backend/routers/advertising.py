"""Ad campaigns: CRUD + CSV import."""

import csv
import io
import time
import uuid
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Request

from backend.persistence import (
    ad_campaigns_store, save_ad_campaigns,
    content_tasks_store,
)
from backend.routers.smart_import import _derive_ad_metrics

router = APIRouter(prefix="/api", tags=["advertising"])


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
    return "作品ID" in headers and "观看次数" in headers


def _is_sucai_csv(headers):
    """Detect 巨量千川 素材分析 CSV format."""
    return "素材名称" in headers and "整体消耗" in headers


@router.get("/ad-campaigns")
async def list_ad_campaigns(platform: Optional[str] = None, brand: Optional[str] = None):
    records = list(ad_campaigns_store.values())
    for r in records:
        if "metrics" in r:
            r["metrics"] = _derive_ad_metrics(r["metrics"])
    if platform:
        records = [r for r in records if r["platform"] == platform]
    if brand:
        records = [r for r in records if r.get("brand") == brand]
    records.sort(key=lambda r: r.get("date_start", ""), reverse=True)
    return {"success": True, "records": records}


@router.post("/ad-campaigns")
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
        "campaign_id": cid, "task_id": task_id,
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


@router.put("/ad-campaigns/{cid}")
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


@router.delete("/ad-campaigns/{cid}")
async def delete_ad_campaign(cid: str):
    if cid not in ad_campaigns_store:
        raise HTTPException(404, f"Ad campaign not found: {cid}")
    del ad_campaigns_store[cid]
    save_ad_campaigns()
    return {"success": True}


@router.post("/ad-campaigns/import-csv")
async def import_ad_csv(file: UploadFile = File(...)):
    """Import ad campaign data from CSV. Supports standard, 千川作品, and 千川素材分析 formats."""
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    is_qc = _is_qianchuan_csv(headers)
    is_sc = _is_sucai_csv(headers)
    imported = []
    skipped = 0
    fmt = "standard"

    existing_fps = set()
    for r in ad_campaigns_store.values():
        fp = (r.get("creative_name", "") or r.get("title", ""), r.get("date_start", ""), r.get("account_name", ""))
        existing_fps.add(fp)

    for row in reader:
        cid = str(uuid.uuid4())[:8]

        if is_sc:
            # 巨量千川 素材分析 格式
            fmt = "sucai"
            title = row.get("素材名称", "").strip()
            raw_date = row.get("素材创建时间", "").strip()
            date_str = raw_date[:10].replace("/", "-") if raw_date else time.strftime("%Y-%m-%d")
            spend = _parse_num(row.get("整体消耗", 0))
            impressions = int(_parse_num(row.get("整体展示次数", 0)))
            clicks = int(_parse_num(row.get("整体点击次数", 0)))
            conversions = int(_parse_num(row.get("整体成交订单数", 0)))
            gmv = _parse_num(row.get("整体成交金额", 0))
            views = int(_parse_num(row.get("视频播放数", 0)))

            if spend == 0 and gmv == 0 and conversions == 0:
                continue

            record = {
                "campaign_id": cid, "task_id": "",
                "platform": "juliang",
                "campaign_name": title[:40] if title else "",
                "creative_name": title,
                "account_name": "",
                "date_start": date_str, "date_end": date_str,
                "budget": 0,
                "metrics": {
                    "spend": spend, "impressions": impressions, "clicks": clicks,
                    "conversions": conversions, "gmv": gmv, "views": views,
                },
                "updated_at": time.time(),
                "brand": "", "sku": "", "title": title, "video_url": "",
                "notes": f"素材ID: {row.get('素材ID', '')}",
                "extra": {
                    "素材ID": row.get("素材ID", ""),
                    "素材评估": row.get("素材评估", ""),
                    "素材时长": row.get("素材时长", ""),
                    "净成交ROI": _parse_num(row.get("净成交ROI", 0)),
                    "净成交金额": _parse_num(row.get("净成交金额", 0)),
                    "净成交订单数": int(_parse_num(row.get("净成交订单数", 0))),
                    "视频点赞数": int(_parse_num(row.get("视频点赞数", 0))),
                    "新增粉丝数": int(_parse_num(row.get("新增粉丝数", 0))),
                    "视频完播率": row.get("视频完播率", ""),
                },
            }
        elif is_qc:
            fmt = "qianchuan"
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
            gpm = _parse_num(row.get("千次观看成交金额", 0))

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
                    "spend": spend, "impressions": impressions, "clicks": clicks,
                    "conversions": conversions, "gmv": gmv, "views": views,
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
                    "conversions": int(_parse_num(row.get("conversions", 0))),
                    "gmv": _parse_num(row.get("gmv", 0)),
                    "views": int(_parse_num(row.get("views", 0))),
                    "roas": _parse_num(row.get("roas", 0)),
                },
                "updated_at": time.time(),
                "brand": brand, "sku": sku, "title": title, "video_url": video_url,
                "notes": row.get("notes", "").strip(),
            }

        record["metrics"] = _derive_ad_metrics(record["metrics"])
        fp = (record.get("creative_name", "") or record.get("title", ""), record.get("date_start", ""), record.get("account_name", ""))
        if fp in existing_fps:
            skipped += 1
            continue
        existing_fps.add(fp)

        ad_campaigns_store[cid] = record
        imported.append(cid)

    save_ad_campaigns()
    return {"success": True, "imported_count": len(imported), "skipped_duplicates": skipped, "ids": imported, "format": fmt}
