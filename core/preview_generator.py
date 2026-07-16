import html
from pathlib import Path
from datetime import datetime

from core.models import ProductData, ImageItem


class PreviewGenerator:
    """
    图片预览 HTML 生成器。

    功能：
    1. 支持单商品预览；
    2. 支持批量商品预览；
    3. 按主图 / 详情图 / SKU 图分组展示；
    4. 生成本地 HTML 文件；
    5. 图片可点击打开原图；
    6. 支持在 HTML 中勾选图片；
    7. 支持导出 selected_images.json。
    """

    @staticmethod
    def save_preview(
        base_dir: str,
        products: list[ProductData],
    ) -> Path:
        """
        生成图片预览 HTML。

        保存目录：
            output/图片预览/图片预览_时间.html
        """

        base_path = Path(base_dir)
        preview_dir = base_path / "图片预览"
        preview_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        preview_path = preview_dir / f"图片预览_{timestamp}.html"

        html_text = PreviewGenerator._build_html(products)

        preview_path.write_text(html_text, encoding="utf-8")

        return preview_path

    @staticmethod
    def _build_html(products: list[ProductData]) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        total_products = len(products)
        total_main = sum(len(p.main_images) for p in products)
        total_detail = sum(len(p.detail_images) for p in products)
        total_sku = sum(len(p.sku_images) for p in products)
        total_images = total_main + total_detail + total_sku

        body_parts = []

        for index, product in enumerate(products, start=1):
            body_parts.append(
                PreviewGenerator._build_product_section(index, product)
            )

        body = "\n".join(body_parts)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>商品图片预览</title>
<style>
* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    padding: 24px;
    background: #111827;
    color: #f9fafb;
    font-family: "Microsoft YaHei", Arial, sans-serif;
}}

a {{
    color: #93c5fd;
    text-decoration: none;
}}

a:hover {{
    text-decoration: underline;
}}

.header {{
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 24px;
}}

.header h1 {{
    margin: 0 0 12px 0;
    font-size: 26px;
}}

.summary {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 12px;
}}

.summary-item {{
    background: #111827;
    border: 1px solid #374151;
    border-radius: 8px;
    padding: 8px 12px;
    color: #e5e7eb;
}}

.toolbar {{
    position: sticky;
    top: 0;
    z-index: 999;
    background: #111827;
    border: 1px solid #374151;
    border-radius: 12px;
    padding: 14px;
    margin-bottom: 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
}}

.toolbar button {{
    background: #4f46e5;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 14px;
}}

.toolbar button:hover {{
    background: #6366f1;
}}

.toolbar button.danger {{
    background: #dc2626;
}}

.toolbar button.danger:hover {{
    background: #ef4444;
}}

.toolbar button.success {{
    background: #16a34a;
}}

.toolbar button.success:hover {{
    background: #22c55e;
}}

.selection-info {{
    color: #d1d5db;
    margin-left: 8px;
}}

.product {{
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 28px;
}}

.product-title {{
    font-size: 20px;
    font-weight: bold;
    margin-bottom: 10px;
    color: #ffffff;
}}

.product-meta {{
    color: #d1d5db;
    line-height: 1.8;
    margin-bottom: 18px;
    word-break: break-all;
}}

.group {{
    margin-top: 24px;
}}

.group-title {{
    font-size: 18px;
    font-weight: bold;
    margin-bottom: 12px;
    padding-left: 10px;
    border-left: 4px solid #6366f1;
}}

.group-actions {{
    margin-bottom: 12px;
    display: flex;
    gap: 8px;
}}

.group-actions button {{
    background: #374151;
    color: #f9fafb;
    border: 1px solid #4b5563;
    border-radius: 6px;
    padding: 5px 9px;
    cursor: pointer;
}}

.group-actions button:hover {{
    background: #4b5563;
}}

.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 16px;
}}

.card {{
    background: #111827;
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 10px;
    overflow: hidden;
    position: relative;
}}

.card:hover {{
    border-color: #6366f1;
}}

.card.unselected {{
    opacity: 0.45;
    border-color: #4b5563;
}}

.select-row {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;
    font-size: 13px;
    color: #e5e7eb;
}}

.select-row input {{
    width: 16px;
    height: 16px;
    cursor: pointer;
}}

.thumb-wrap {{
    width: 100%;
    height: 180px;
    background: #0f172a;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}}

.thumb-wrap img {{
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}}

.card-info {{
    margin-top: 8px;
    font-size: 13px;
    color: #d1d5db;
    line-height: 1.5;
    word-break: break-all;
}}

.badge {{
    display: inline-block;
    background: #4f46e5;
    color: white;
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 12px;
    margin-bottom: 4px;
}}

.empty {{
    color: #9ca3af;
    font-style: italic;
    padding: 8px 0;
}}

.footer {{
    text-align: center;
    color: #9ca3af;
    margin-top: 36px;
    font-size: 13px;
}}

.notice {{
    color: #fbbf24;
    font-size: 13px;
    line-height: 1.7;
}}
</style>
</head>
<body>

<div class="header">
    <h1>商品图片预览</h1>
    <div>生成时间：{html.escape(now)}</div>
    <div class="summary">
        <div class="summary-item">商品数量：{total_products}</div>
        <div class="summary-item">图片总数：{total_images}</div>
        <div class="summary-item">主图：{total_main}</div>
        <div class="summary-item">详情图：{total_detail}</div>
        <div class="summary-item">SKU图：{total_sku}</div>
    </div>
</div>

<div class="toolbar">
    <button onclick="selectAllImages()">全选</button>
    <button class="danger" onclick="unselectAllImages()">取消全选</button>
    <button onclick="invertSelection()">反选</button>
    <button onclick="selectOnlyType('main')">仅选择主图</button>
    <button onclick="selectOnlyType('detail')">仅选择详情图</button>
    <button onclick="selectOnlyType('sku')">仅选择SKU图</button>
    <button class="success" onclick="exportSelectedImages()">导出选择结果</button>
    <span class="selection-info" id="selectionInfo">已选择：0 / 0</span>
    <div class="notice">
        说明：勾选需要下载的图片，点击“导出选择结果”会生成 selected_images.json。
        下一步可在软件中导入该文件后，只下载选中的图片。
    </div>
</div>

{body}

<div class="footer">
    商品图片采集工具 - 本地预览页
</div>

<script>
function getAllCheckboxes() {{
    return Array.from(document.querySelectorAll(".image-checkbox"));
}}

function updateCardState(checkbox) {{
    const card = checkbox.closest(".card");
    if (!card) return;

    if (checkbox.checked) {{
        card.classList.remove("unselected");
    }} else {{
        card.classList.add("unselected");
    }}
}}

function updateSelectionInfo() {{
    const boxes = getAllCheckboxes();
    const selected = boxes.filter(box => box.checked).length;

    const info = document.getElementById("selectionInfo");
    if (info) {{
        info.textContent = "已选择：" + selected + " / " + boxes.length;
    }}

    boxes.forEach(updateCardState);
}}

function selectAllImages() {{
    getAllCheckboxes().forEach(box => {{
        box.checked = true;
    }});
    updateSelectionInfo();
}}

function unselectAllImages() {{
    getAllCheckboxes().forEach(box => {{
        box.checked = false;
    }});
    updateSelectionInfo();
}}

function invertSelection() {{
    getAllCheckboxes().forEach(box => {{
        box.checked = !box.checked;
    }});
    updateSelectionInfo();
}}

function selectOnlyType(type) {{
    getAllCheckboxes().forEach(box => {{
        box.checked = box.dataset.type === type;
    }});
    updateSelectionInfo();
}}

function selectGroup(productIndex, imageType, checked) {{
    getAllCheckboxes().forEach(box => {{
        if (
            box.dataset.productIndex === String(productIndex) &&
            box.dataset.type === imageType
        ) {{
            box.checked = checked;
        }}
    }});
    updateSelectionInfo();
}}

function exportSelectedImages() {{
    const products = [];

    const productElements = Array.from(document.querySelectorAll(".product"));

    productElements.forEach(productEl => {{
        const productIndex = productEl.dataset.productIndex;

        const productData = {{
            index: Number(productIndex),
            platform: productEl.dataset.platform || "",
            product_id: productEl.dataset.productId || "",
            title: productEl.dataset.title || "",
            url: productEl.dataset.url || "",
            selected: {{
                main: [],
                detail: [],
                sku: []
            }}
        }};

        const boxes = Array.from(
            productEl.querySelectorAll(".image-checkbox")
        );

        boxes.forEach(box => {{
            if (!box.checked) return;

            const imageType = box.dataset.type;
            const imageData = {{
                url: box.dataset.url || "",
                sku_name: box.dataset.skuName || "",
                source: box.dataset.source || "",
                index: Number(box.dataset.imageIndex || "0")
            }};

            if (imageType === "main") {{
                productData.selected.main.push(imageData);
            }} else if (imageType === "detail") {{
                productData.selected.detail.push(imageData);
            }} else if (imageType === "sku") {{
                productData.selected.sku.push(imageData);
            }}
        }});

        products.push(productData);
    }});

    const payload = {{
        version: 1,
        generated_at: new Date().toISOString(),
        products: products
    }};

    const jsonText = JSON.stringify(payload, null, 2);
    const blob = new Blob([jsonText], {{ type: "application/json;charset=utf-8" }});
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = "selected_images.json";
    document.body.appendChild(a);
    a.click();

    setTimeout(() => {{
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }}, 100);

    alert("选择结果已导出：selected_images.json");
}}

document.addEventListener("DOMContentLoaded", () => {{
    getAllCheckboxes().forEach(box => {{
        box.addEventListener("change", updateSelectionInfo);
    }});
    updateSelectionInfo();
}});
</script>

</body>
</html>
"""

    @staticmethod
    def _build_product_section(index: int, product: ProductData) -> str:
        title = html.escape(product.title or "未命名商品")
        platform = html.escape(product.platform or "-")
        product_id = html.escape(product.product_id or "-")
        url = html.escape(product.url or "")

        data_title = html.escape(product.title or "", quote=True)
        data_platform = html.escape(product.platform or "", quote=True)
        data_product_id = html.escape(product.product_id or "", quote=True)
        data_url = html.escape(product.url or "", quote=True)

        main_count = len(product.main_images)
        detail_count = len(product.detail_images)
        sku_count = len(product.sku_images)
        total_count = product.total_count()

        main_group = PreviewGenerator._build_image_group(
            product_index=index,
            title="主图",
            image_type="main",
            images=product.main_images,
        )

        detail_group = PreviewGenerator._build_image_group(
            product_index=index,
            title="详情图",
            image_type="detail",
            images=product.detail_images,
        )

        sku_group = PreviewGenerator._build_image_group(
            product_index=index,
            title="SKU图",
            image_type="sku",
            images=product.sku_images,
        )

        return f"""
<div class="product"
     data-product-index="{index}"
     data-platform="{data_platform}"
     data-product-id="{data_product_id}"
     data-title="{data_title}"
     data-url="{data_url}">
    <div class="product-title">[{index}] {title}</div>
    <div class="product-meta">
        平台：{platform}<br>
        商品ID：{product_id}<br>
        商品链接：<a href="{url}" target="_blank">{url}</a><br>
        识别结果：主图 {main_count} 张，详情图 {detail_count} 张，SKU图 {sku_count} 张，总计 {total_count} 张
    </div>

    {main_group}
    {detail_group}
    {sku_group}
</div>
"""

    @staticmethod
    def _build_image_group(
        product_index: int,
        title: str,
        image_type: str,
        images: list[ImageItem],
    ) -> str:
        safe_title = html.escape(title)

        if not images:
            return f"""
<div class="group">
    <div class="group-title">{safe_title}：0 张</div>
    <div class="empty">未识别到该类型图片。</div>
</div>
"""

        cards = []

        for index, item in enumerate(images, start=1):
            cards.append(
                PreviewGenerator._build_image_card(
                    product_index=product_index,
                    index=index,
                    image_type=image_type,
                    item=item,
                )
            )

        cards_html = "\n".join(cards)

        return f"""
<div class="group">
    <div class="group-title">{safe_title}：{len(images)} 张</div>
    <div class="group-actions">
        <button onclick="selectGroup({product_index}, '{image_type}', true)">本组全选</button>
        <button onclick="selectGroup({product_index}, '{image_type}', false)">本组取消</button>
    </div>
    <div class="grid">
        {cards_html}
    </div>
</div>
"""

    @staticmethod
    def _build_image_card(
        product_index: int,
        index: int,
        image_type: str,
        item: ImageItem,
    ) -> str:
        url = html.escape(item.url or "")
        data_url = html.escape(item.url or "", quote=True)

        sku_name = html.escape(item.sku_name or "")
        data_sku_name = html.escape(item.sku_name or "", quote=True)

        source = html.escape(item.source or "")
        data_source = html.escape(item.source or "", quote=True)

        type_map = {
            "main": "主图",
            "detail": "详情图",
            "sku": "SKU图",
        }

        type_label = type_map.get(image_type, image_type)

        sku_html = ""
        if sku_name:
            sku_html = f"<div>SKU：{sku_name}</div>"

        source_html = ""
        if source:
            source_html = f"<div>来源：{source}</div>"

        return f"""
<div class="card">
    <label class="select-row">
        <input
            type="checkbox"
            class="image-checkbox"
            checked
            data-product-index="{product_index}"
            data-type="{image_type}"
            data-image-index="{index}"
            data-url="{data_url}"
            data-sku-name="{data_sku_name}"
            data-source="{data_source}"
        >
        <span>选择 {type_label} #{index}</span>
    </label>

    <a href="{url}" target="_blank" title="点击打开原图">
        <div class="thumb-wrap">
            <img src="{url}" loading="lazy" alt="{type_label}_{index}">
        </div>
    </a>

    <div class="card-info">
        <span class="badge">{type_label} #{index}</span>
        {sku_html}
        {source_html}
        <div>
            <a href="{url}" target="_blank">打开原图</a>
        </div>
    </div>
</div>
"""
