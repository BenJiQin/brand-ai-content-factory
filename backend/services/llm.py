"""LLM integration: 小源 (OpenClaw) with Claude fallback."""

import httpx

from backend.config import (
    OPENCLAW_GATEWAY, OPENCLAW_AGENT,
    CLAUDE_API_BASE, CLAUDE_API_KEY, CLAUDE_MODEL,
)


async def call_xiaoyuan(message: str, session_key: str = "platform") -> str:
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
