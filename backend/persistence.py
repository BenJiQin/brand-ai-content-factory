"""Data persistence: in-memory stores backed by JSON files."""

import json
from backend.config import CONTENT_TASKS_FILE, SOCIAL_PUBLISH_FILE, AD_CAMPAIGNS_FILE, DATA_DIR

# --- In-memory stores (module-level singletons, shared by all routers) ---
tasks_store: dict = {}           # task_id -> {status, type, params, result, ...}
content_tasks_store: dict = {}   # task_id -> content_task dict
social_publish_store: dict = {}  # publish_id -> publish record
ad_campaigns_store: dict = {}    # campaign_id -> campaign record


# --- Content tasks ---

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


# --- Social publish ---

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


# --- Ad campaigns ---

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


# --- Load all stores on import ---
load_content_tasks()
load_social_publish()
load_ad_campaigns()
