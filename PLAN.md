# 品牌AI内容管理平台 — 重构规划 v2.0

> 生成时间: 2026-03-23
> 负责人: Marley

---

## 一、现状问题

| 问题 | 描述 |
|------|------|
| 单文件巨无霸 | index.html 4965行，开发/调试困难 |
| 创作功能假 | creation.html 只有模拟输出，不调真实 API |
| 后端存在但未接 | server.py 有完整 Seedance API，前端没用 |
| 无 LLM 文案生成 | 没有接 Claude/GPT 生成真实文案 |
| 品牌资产孤立 | CDN 图片有了，但创作流程没真正用上 |
| 无任务持久化 | tasks_store 在内存，重启消失 |

---

## 二、目标架构

```
品牌AI内容管理平台/
├── server.py              # FastAPI 后端（保留，扩展）
├── index.html             # 主框架 + sidebar（精简到 <300行）
│
├── pages/                 # 各功能页面
│   ├── dashboard.html     # 首页概览（Mock）
│   ├── assets.html        # 资产库
│   ├── brands.html        # 品牌管理
│   ├── creation.html      # 🌟 创作中心（真实 AI）
│   ├── tasks.html         # 任务队列（轮询状态）
│   └── publish.html       # 发布中心
│
├── js/                    # 共享 JS 模块
│   ├── api.js             # API 客户端（fetch wrapper）
│   ├── router.js          # SPA 路由（hash-based）
│   └── brand-assets.js    # 品牌资产注册表
│
├── css/
│   └── design-system.css  # 共享 CSS 变量 + 基础组件
│
└── api/                   # 后端扩展
    ├── llm.py             # Claude/GPT 文案生成端点
    └── tasks_db.py        # SQLite 任务持久化
```

---

## 三、创作中心核心流程（重点）

```
用户输入 prompt
    ↓
[步骤1] POST /api/llm/generate-script
  → 调用 Claude API（anthropic/claude-sonnet）
  → 返回：分镜脚本 + Seedance 提示词 + 平台文案
    ↓
用户选择资产（光子瓶 SKU 图）+ 确认脚本
    ↓
[步骤2] POST /api/generate/reference (or image2video)
  → 调用 Seedance 2.0 API
  → 返回 task_id
    ↓
[步骤3] 轮询 GET /api/task/{task_id}
  → 前端轮询，进度条更新
  → 完成后显示视频播放器
```

---

## 四、重构任务清单

### Phase 1 — 结构拆分（Claude Code 执行）

- [ ] 从 index.html 提取共享 CSS → `css/design-system.css`
- [ ] 从 index.html 提取各页面 HTML → `pages/*.html`
- [ ] 创建 `js/api.js`（封装所有 fetch 调用）
- [ ] 创建 `js/router.js`（iframe 或 fetch-replace SPA）
- [ ] index.html 精简为 shell（sidebar + content-area）

### Phase 2 — 后端扩展

- [ ] `api/llm.py`：POST /api/llm/script
  - 接收 prompt + brand + sku + mode
  - 调用 newapi/claude-sonnet（或 claude-opus）
  - 返回结构化脚本 JSON
- [ ] `api/tasks_db.py`：SQLite 持久化
  - tasks 表：task_id, type, status, prompt, result, created_at
  - server.py 集成

### Phase 3 — creation.html 真实 AI

- [ ] 接 /api/llm/script → 真实文案生成
- [ ] 接 /api/generate/reference → 真实视频生成
- [ ] 接 /api/task/{id} 轮询 → 进度条 + 视频播放
- [ ] 选中 SKU 图作为参考图传入 Seedance

### Phase 4 — 任务队列页面

- [ ] tasks.html：轮询所有任务，显示状态
- [ ] 完成的视频可预览/下载

---

## 五、API 凭据（已有）

```
# Seedance / 蓝标中转
SEEDANCE_API_BASE = https://bmc-model-openapi.bluemediagroup.cn/api
APPID = eFtlRLvR0kb5MK48
SECRET = 05CgKka3wpyQabrlOxMM6Ha6k52K2Fpa

# 图床
MEDIA_API_HOST = https://blueai-media-storage.bluemediagroup.cn
MEDIA_API_KEY = e8fd667c-061d-40a7-bc7a-d980efc3c4fe

# LLM（从环境变量或 ~/.openclaw/config 读取）
NEWAPI_BASE = https://api.newapi.link/v1  （或从 OPENAI_API_BASE 读）
NEWAPI_KEY = （从环境变量读）
LLM_MODEL = anthropic/claude-sonnet-4-6
```

---

## 六、人源活力 RHC 光子瓶资产

```json
{
  "brand": "人源活力",
  "sku": "光子瓶",
  "images": {
    "01": "https://vlc-bmc-media-storage-global.bluemediacdn.com/.../01-studio-front.png",
    "02": "https://vlc-bmc-media-storage-global.bluemediacdn.com/.../02-closeup-front.png",
    "03": "https://vlc-bmc-media-storage-global.bluemediacdn.com/.../03-side-angle.png",
    "04": "https://vlc-bmc-media-storage-global.bluemediacdn.com/.../04-handheld.png"
  }
}
```

---

## 七、执行优先级

1. **最高优先** — creation.html 接真实 LLM + Seedance（核心 demo 价值）
2. **高优先** — index.html 拆分（开发体验）
3. **中优先** — tasks.html 任务队列
4. **低优先** — publish / dashboard 等 Mock 页面

---

*Marley, 2026-03-23*
