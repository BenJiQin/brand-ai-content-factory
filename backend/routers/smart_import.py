"""Smart CSV import: LLM-driven field mapping + derived metrics + AI summary."""

import csv as csv_mod
import io
import json
import time
import uuid

import httpx
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import StreamingResponse

from backend.config import (
    CLAUDE_API_BASE, CLAUDE_API_KEY, CLAUDE_MODEL,
    SOCIAL_SCHEMA_FIELDS, AD_SCHEMA_FIELDS,
)
from backend.persistence import (
    social_publish_store, save_social_publish,
    ad_campaigns_store, save_ad_campaigns,
)

router = APIRouter(prefix="/api", tags=["smart_import"])


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
    # Always recalculate derived metrics from raw data
    m["ctr"] = round(clicks / impressions, 4) if impressions > 0 else 0
    m["cpc"] = round(spend / clicks, 2) if clicks > 0 else 0
    m["cpm"] = round(spend / impressions * 1000, 2) if impressions > 0 else 0
    m["cvr"] = round(conversions / clicks, 4) if clicks > 0 else 0
    m["cpa"] = round(spend / conversions, 2) if conversions > 0 else 0
    m["roas"] = round(gmv / spend, 2) if spend > 0 else 0
    m["gpm"] = round(gmv / views * 1000, 2) if views > 0 else 0
    m["cpv"] = round(spend / views, 4) if views > 0 else 0
    return m


def _parse_num(v, default=0):
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
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(content)
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


@router.post("/import/smart-csv")
async def smart_csv_import(target: str = "social", file: UploadFile = File(...)):
    """SSE streaming: upload CSV → LLM maps fields → process rows → derive metrics → AI summary."""
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

        yield sse("progress", f"📂 检测到文件编码: {enc.upper()}，共 {len(all_rows)} 行数据，{len(headers)} 列")
        yield sse("progress", f"📋 表头: {', '.join(headers[:8])}{'...' if len(headers) > 8 else ''}")

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

        yield sse("progress", f"⚙️ 开始处理 {len(all_rows)} 行数据...")
        rev = {std: csv_col for csv_col, std in mapping.items() if std}
        mapped_csv_cols = set(mapping.keys())
        imported = []
        skipped = 0
        derived_count = 0
        stats = {"total_views": 0, "total_interact": 0, "total_gmv": 0, "total_orders": 0,
                 "total_spend": 0, "total_conversions": 0, "max_views": 0, "max_title": ""}

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
                    "video_url": "", "notes": "", "extra": extra,
                }
                fp = (record.get("title", ""), record.get("published_at", ""), record.get("account_name", ""))
                if fp in existing_fps:
                    skipped += 1
                    continue
                existing_fps.add(fp)

                social_publish_store[pid] = record
                imported.append(pid)
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
                    "video_url": "", "notes": "", "extra": extra_ad,
                }
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

            if (idx + 1) % 100 == 0:
                yield sse("progress", f"⚙️ 已处理 {idx + 1}/{len(all_rows)} 行...")

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
