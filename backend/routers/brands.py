"""Brands & products endpoints: brand assets + products.md bidirectional sync."""

import re as _re

from fastapi import APIRouter, HTTPException, Request

from backend.config import brand_assets, PRODUCTS_MD

router = APIRouter(prefix="/api", tags=["brands"])


@router.get("/brands")
async def list_brands():
    """List all brands and their assets."""
    return {"success": True, "brands": brand_assets}


@router.get("/brands/{brand}/{sku}/images")
async def get_sku_images(brand: str, sku: str):
    """Get CDN image URLs for a specific brand/SKU."""
    b = brand_assets.get(brand, {})
    s = b.get(sku, {})
    if not s:
        raise HTTPException(404, f"Brand/SKU not found: {brand}/{sku}")
    return {"success": True, "brand": brand, "sku": sku, "images": s.get("images", [])}


# --- products.md parser ---

def _parse_products_md() -> list[dict]:
    """Parse products.md into a list of SKU dicts."""
    if not PRODUCTS_MD.exists():
        return []
    text = PRODUCTS_MD.read_text(encoding="utf-8")
    chunks = _re.split(r'^## ', text, flags=_re.MULTILINE)
    skus = []
    for chunk in chunks[1:]:
        lines = chunk.strip().split('\n')
        name = lines[0].strip()
        body = '\n'.join(lines[1:])

        sku: dict = {"name": name}

        field_map = {
            "电商昵称": "nickname", "正式名": "formalName", "系列": "series",
            "核心技术": "tech", "形态": "form", "适合人群": "targetUsers",
            "使用场景": "scenes", "关键词": "keywords", "备注": "notes",
            "规格": "spec", "价格": "price", "卖点一句话": "oneLiner",
            "使用步骤": "steps",
        }
        for zh, en in field_map.items():
            m = _re.search(rf'\*\*{zh}[：:]\*\*\s*(.*)', body)
            if m:
                sku[en] = m.group(1).strip()

        sp_match = _re.search(r'\*\*核心卖点[：:]\*\*\s*\n((?:- .+\n?)+)', body)
        if sp_match:
            sku["sellingPoints"] = [l.lstrip('- ').strip() for l in sp_match.group(1).strip().split('\n') if l.strip()]

        numbered_sp = _re.search(r'### 核心卖点[^#]*?\n((?:\d+\..+\n?)+)', body)
        if numbered_sp:
            sku["sellingPointsDetailed"] = []
            for line in numbered_sp.group(1).strip().split('\n'):
                line = _re.sub(r'^\d+\.\s*', '', line).strip()
                if line:
                    sku["sellingPointsDetailed"].append(line)

        def parse_table(section_pattern: str) -> list[dict]:
            m = _re.search(section_pattern + r'[\s\S]*?\n(\|.+\|(?:\n\|.+\|)*)', body)
            if not m:
                return []
            table_text = m.group(1).strip()
            rows = [r.strip() for r in table_text.split('\n') if r.strip()]
            if len(rows) < 3:
                return []
            headers = [h.strip() for h in rows[0].split('|')[1:-1]]
            result = []
            for row in rows[1:]:
                if _re.match(r'^\|[\s\-:|]+\|$', row):
                    continue
                cells = [c.strip() for c in row.split('|')[1:-1]]
                if len(cells) == len(headers):
                    result.append(dict(zip(headers, cells)))
            return result

        ingredients = parse_table(r'### 核心成分')
        if not ingredients:
            ingredients = parse_table(r'\| 成分 \|')
        if ingredients:
            sku["ingredients"] = ingredients

        competitors = parse_table(r'### 竞品对比')
        if not competitors:
            competitors = parse_table(r'\| 对比维度 \|')
        if competitors:
            sku["competitors"] = competitors

        skus.append(sku)
    return skus


def _sku_to_md(sku: dict) -> str:
    """Convert a SKU dict back to markdown section (without ## heading)."""
    lines = []
    field_order = [
        ("nickname", "电商昵称"), ("formalName", "正式名"), ("series", "系列"),
        ("notes", "备注"), ("spec", "规格"), ("price", "价格"),
        ("tech", "核心技术"), ("form", "形态"),
    ]
    for en, zh in field_order:
        if sku.get(en):
            lines.append(f"**{zh}：** {sku[en]}")
    lines.append("")

    if sku.get("sellingPoints"):
        lines.append("**核心卖点：**")
        for sp in sku["sellingPoints"]:
            lines.append(f"- {sp}")
        lines.append("")

    if sku.get("ingredients"):
        headers = list(sku["ingredients"][0].keys())
        lines.append("### 核心成分")
        lines.append("")
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["------"] * len(headers)) + "|")
        for row in sku["ingredients"]:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")
        lines.append("")

    if sku.get("oneLiner"):
        lines.append(f"**卖点一句话：** {sku['oneLiner']}")
        lines.append("")

    if sku.get("sellingPointsDetailed"):
        lines.append("### 核心卖点（视频文案用）")
        for i, sp in enumerate(sku["sellingPointsDetailed"], 1):
            lines.append(f"{i}. {sp}")
        lines.append("")

    simple_fields = [
        ("steps", "使用步骤"), ("targetUsers", "适合人群"),
        ("scenes", "使用场景"),
    ]
    for en, zh in simple_fields:
        if sku.get(en):
            lines.append(f"**{zh}：** {sku[en]}")

    if sku.get("competitors"):
        lines.append("")
        lines.append("### 竞品对比")
        lines.append("")
        headers = list(sku["competitors"][0].keys())
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["------"] * len(headers)) + "|")
        for row in sku["competitors"]:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")

    if sku.get("keywords"):
        lines.append("")
        lines.append(f"**关键词：** {sku['keywords']}")

    return "\n".join(lines)


def _save_products_md(skus: list[dict]):
    """Write all SKUs back to products.md, preserving the file header."""
    text = PRODUCTS_MD.read_text(encoding="utf-8") if PRODUCTS_MD.exists() else ""
    first_h2 = _re.search(r'^## ', text, flags=_re.MULTILINE)
    preamble = text[:first_h2.start()].rstrip() if first_h2 else text.rstrip()

    parts = [preamble, ""]
    for sku in skus:
        parts.append(f"## {sku['name']}")
        parts.append("")
        parts.append(_sku_to_md(sku))
        parts.append("")
        parts.append("---")
        parts.append("")
    while parts and parts[-1].strip() in ("", "---"):
        parts.pop()
    parts.append("")

    PRODUCTS_MD.write_text("\n".join(parts), encoding="utf-8")


# --- Endpoints ---

@router.get("/products")
async def list_products():
    return {"success": True, "products": _parse_products_md()}


@router.put("/products/{sku_name}")
async def update_product(sku_name: str, request: Request):
    body = await request.json()
    skus = _parse_products_md()
    found = False
    for i, s in enumerate(skus):
        if s["name"] == sku_name:
            body["name"] = sku_name
            skus[i] = body
            found = True
            break
    if not found:
        raise HTTPException(404, f"SKU not found: {sku_name}")
    _save_products_md(skus)
    return {"success": True}


@router.post("/products")
async def create_product(request: Request):
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(400, "name is required")
    skus = _parse_products_md()
    if any(s["name"] == body["name"] for s in skus):
        raise HTTPException(409, f"SKU already exists: {body['name']}")
    skus.append(body)
    _save_products_md(skus)
    return {"success": True}
