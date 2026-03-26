"""Data insights & analytics endpoints."""

import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from backend.persistence import social_publish_store, ad_campaigns_store, content_tasks_store
from backend.services.llm import call_xiaoyuan

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/insights/summary")
async def insights_summary():
    """Aggregate social + ad data into structured summary for display and LLM analysis."""
    social = list(social_publish_store.values())
    ads = list(ad_campaigns_store.values())
    tasks = list(content_tasks_store.values())

    # Per-SKU aggregation
    sku_map = {}
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
            "sku": sku, "brand": data["brand"],
            "social_count": len(s_list), "ad_count": len(a_list),
            "total_views": total_views, "total_interact": total_interact,
            "avg_completion_rate": round(avg_comp, 3),
            "total_spend": round(total_spend, 2),
            "total_conversions": total_conv,
            "weighted_roas": round(weighted_roas, 2),
        })

    # Per-platform aggregation
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


@router.post("/insights/analyze")
async def insights_analyze(request: Request):
    """Use LLM to generate strategic insights from aggregated data. Returns SSE stream."""
    body = await request.json()
    data_summary = body.get("data_summary", "")
    focus = body.get("focus", "comprehensive")

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
            reply = await call_xiaoyuan(user_msg, session_key="insight-analyze")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
