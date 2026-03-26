"""Content creation endpoints: copy generation, storyboard, chat."""

import json
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.config import (
    BRAND_SYSTEM_PROMPTS, OPENCLAW_GATEWAY, OPENCLAW_AGENT, XIAOYUAN_WORKSPACE,
)
from backend.persistence import social_publish_store, ad_campaigns_store, save_social_publish, save_ad_campaigns
from backend.services.llm import call_xiaoyuan

router = APIRouter(prefix="/api", tags=["content"])


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
    insights = []
    for delta in (0, 1):
        d = date.today() - timedelta(days=delta)
        daily = XIAOYUAN_WORKSPACE / "memory" / "daily" / f"{d.isoformat()}.md"
        if daily.exists():
            text = daily.read_text(encoding="utf-8")
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


@router.post("/generate/copy")
async def generate_copy(
    request: Request,
    prompt: str = Form(...),
    brand: str = Form(""),
    sku: str = Form(""),
):
    """Stream video script generation via 小源 (OpenClaw) with brand knowledge + data insights."""
    brand_knowledge = _load_brand_knowledge()
    context = "你是人源活力品牌专属视频脚本策划师，基于品牌知识库和数据洞察，创作适合Seedance AI视频生成的口播脚本。\n\n"
    if brand_knowledge:
        context += f"## 品牌与产品知识库\n{brand_knowledge}\n\n"
    else:
        context += f"品牌：人源活力（RHC）\n产品：{sku}\n"

    data_insights = _load_data_insights()
    if data_insights:
        context += f"## 数据洞察（请参考以优化内容策略）\n{data_insights}\n\n"

    context += "## 创作要求\n"
    context += "类型：Seedance视频口播脚本。请输出完整的中文口播文案，前端会根据时长自动拆分为15秒片段并生成视频。\n"
    context += "- 用中文详细描述画面内容、视觉风格、镜头语言\n"
    context += "- 前3秒必须有钩子，吸引注意力\n"
    context += "- 节奏紧凑，每句话都要有信息量\n"
    context += "- 结尾有CTA（行动号召）\n"
    context += "- 中文口播约4字/秒，15秒≈60字\n"

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
            reply = await call_xiaoyuan(full_message, session_key="copy-gen")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/generate/storyboard")
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

    brand_knowledge = _load_brand_knowledge()
    if brand_knowledge:
        context += f"\n\n## 品牌与产品知识库\n{brand_knowledge}"

    data_insights = _load_data_insights()
    if data_insights:
        context += f"\n\n## 数据洞察（参考以优化内容策略）\n{data_insights}"

    if brand:
        context += f"\n\n当前品牌：{brand}"
    if sku:
        context += f"\n当前产品：{sku}"

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

    char_count = len(script.strip())
    suggested_segments = max(1, (char_count + 59) // 60)
    seg_hint = f"[口播脚本约{char_count}字，按4字/秒语速约{char_count/4:.0f}秒，建议拆分为{suggested_segments}个15秒片段]"

    full_message = f"[分镜创作上下文]\n{context}\n\n{seg_hint}\n\n请为以下脚本生成Seedance视频分镜方案：\n\n{script}"

    async def stream():
        try:
            reply = await call_xiaoyuan(full_message, session_key="storyboard-gen")
            if reply:
                yield f"data: {json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'text': '⚠️ OpenClaw Gateway 未运行，请确认 openclaw gateway start 已执行'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/chat")
async def chat_with_xiaoyuan(request: Request):
    """Forward chat messages to real 小源 agent via OpenClaw Gateway."""
    body = await request.json()
    user_message = body.get("message", "")
    session_id = body.get("session_id", "web-visitor")
    context = body.get("context", "")

    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")

    full_message = user_message
    if context:
        full_message = f"[数据上下文]\n{context}\n\n[用户问题]\n{user_message}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OPENCLAW_GATEWAY}/api/sessions/{OPENCLAW_AGENT}/send",
                json={"message": full_message, "session_key": session_id},
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
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/data-chat")
async def data_chat(request: Request):
    """Data analysis chat — routes to real 小源 via OpenClaw Gateway with data context."""
    import re

    body = await request.json()
    messages = body.get("messages", [])
    page = body.get("page", "social")

    store = social_publish_store if page == "social" else ad_campaigns_store
    id_key = "publish_id" if page == "social" else "campaign_id"
    records = list(store.values())

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
