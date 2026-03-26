"""Centralized configuration: API keys, paths, system prompts, brand assets."""

import os
from pathlib import Path

# --- Project root (where server.py lives) ---
PROJECT_ROOT = Path(__file__).parent.parent

# --- Seedance (video generation) ---
SEEDANCE_API_BASE = "https://bmc-model-openapi.bluemediagroup.cn/api"
SEEDANCE_APPID = "eFtlRLvR0kb5MK48"
SEEDANCE_SECRET = "05CgKka3wpyQabrlOxMM6Ha6k52K2Fpa"

# --- BlueAI Media Storage ---
MEDIA_API_HOST = "https://blueai-media-storage.bluemediagroup.cn"
MEDIA_API_KEY = "e8fd667c-061d-40a7-bc7a-d980efc3c4fe"

# --- Claude API (蓝标中转) ---
CLAUDE_API_BASE = os.environ.get("CLAUDE_API_BASE", "https://bmc-llm-relay.bluemediagroup.cn/v1")
CLAUDE_API_KEY = os.environ.get("OPENAI_API_KEY", "sk-bO01ixWMbGEzblzbvyjVXPKeDJgV1oA4uLbnuzmRnO3c6ogl")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "Doubao-Seed-2.0-pro")

# --- OpenClaw Gateway (真·小源 Agent) ---
OPENCLAW_GATEWAY = os.environ.get("OPENCLAW_GATEWAY", "http://127.0.0.1:18789")
OPENCLAW_AGENT = os.environ.get("OPENCLAW_AGENT", "xiaoyuan")

# --- Seedance model map ---
MODEL_MAP = {
    "2.0": "doubao-seedance-2-0-260128",
    "1.5pro": "doubao-seedance-1-5-pro-251215",
    "1.0pro": "doubao-seedance-1-0-pro-250528",
}
DEFAULT_MODEL = "2.0"

# --- Directories ---
UPLOAD_DIR = PROJECT_ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

CONTENT_TASKS_FILE = DATA_DIR / "content_tasks.json"
SOCIAL_PUBLISH_FILE = DATA_DIR / "social_publish.json"
AD_CAMPAIGNS_FILE = DATA_DIR / "ad_campaigns.json"

# --- 小源 workspace (external, not in this repo) ---
XIAOYUAN_WORKSPACE = Path.home() / "renyuan-workspace"
PRODUCTS_MD = XIAOYUAN_WORKSPACE / "memory" / "core" / "products.md"

# --- System prompts ---
BRAND_SYSTEM_PROMPTS = {
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

# --- Brand asset registry ---
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

# --- Platform name maps ---
PLATFORM_NAMES = {
    "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手",
    "weixin": "微信视频号", "bilibili": "B站", "weibo": "微博",
}

AD_PLATFORMS = {
    "juliang": "巨量引擎", "ciliyinqing": "磁力引擎", "tengxun": "腾讯广告",
    "jvguang": "聚光平台", "bilibili": "B站花火", "baidu": "百度信息流",
}

# --- Smart import schemas ---
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
