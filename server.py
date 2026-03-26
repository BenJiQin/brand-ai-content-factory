#!/usr/bin/env python3
"""
品牌AI内容管理平台 — Backend API Server
Wraps Seedance 2.0 video generation + BlueAI Media Storage.

Run: python3 server.py [--port 8889]
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# Import persistence to trigger data loading from JSON files
import backend.persistence  # noqa: F401

from backend.routers import (
    video,
    content,
    brands,
    tasks,
    social,
    advertising,
    analytics,
    smart_import,
)

app = FastAPI(title="品牌AI内容管理平台 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all API routers
app.include_router(video.router)
app.include_router(content.router)
app.include_router(brands.router)
app.include_router(tasks.router)
app.include_router(social.router)
app.include_router(advertising.router)
app.include_router(analytics.router)
app.include_router(smart_import.router)

# Static file serving (must be LAST — catch-all route)
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
