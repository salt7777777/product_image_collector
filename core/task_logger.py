import json
from pathlib import Path
from datetime import datetime

from core.models import ProductData, DownloadResult


class TaskLogger:
    """
    生成采集日志和商品数据 JSON。
    """

    @staticmethod
    def save_log(
        product_dir: Path,
        product: ProductData,
        selected_types: dict[str, bool],
        download_result: DownloadResult,
    ):
        log_path = product_dir / "采集日志.txt"

        lines = []
        lines.append("采集日志")
        lines.append("=" * 40)
        lines.append(f"采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"平台：{product.platform}")
        lines.append(f"商品ID：{product.product_id}")
        lines.append(f"商品标题：{product.title}")
        lines.append(f"商品链接：{product.url}")
        lines.append(f"保存路径：{product_dir}")
        lines.append("")
        lines.append("识别结果：")
        lines.append(f"主图：{len(product.main_images)} 张")
        lines.append(f"详情图：{len(product.detail_images)} 张")
        lines.append(f"SKU图：{len(product.sku_images)} 张")
        lines.append(f"总计：{product.total_count()} 张")
        lines.append("")
        lines.append("下载选择：")
        lines.append(f"主图：{'是' if selected_types.get('main') else '否'}")
        lines.append(f"详情图：{'是' if selected_types.get('detail') else '否'}")
        lines.append(f"SKU图：{'是' if selected_types.get('sku') else '否'}")
        lines.append("")
        lines.append("下载结果：")
        lines.append(f"计划下载：{download_result.total} 张")
        lines.append(f"成功下载：{download_result.success} 张")
        lines.append(f"失败下载：{download_result.failed} 张")
        lines.append(f"成功率：{download_result.success_rate}%")
        lines.append("")

        if download_result.failed_items:
            lines.append("失败明细：")
            for index, item in enumerate(download_result.failed_items, start=1):
                lines.append(f"[{index}] 类型：{item.image_type}")
                lines.append(f"    文件名：{item.filename}")
                lines.append(f"    URL：{item.url}")
                lines.append(f"    原因：{item.reason}")
        else:
            lines.append("失败明细：无")

        log_path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def save_product_json(
        product_dir: Path,
        product: ProductData,
        download_result: DownloadResult,
    ):
        json_path = product_dir / "商品数据.json"

        data = {
            "platform": product.platform,
            "product_id": product.product_id,
            "title": product.title,
            "url": product.url,
            "images": {
                "main": [img.url for img in product.main_images],
                "detail": [img.url for img in product.detail_images],
                "sku": [
                    {
                        "url": img.url,
                        "sku_name": img.sku_name,
                    }
                    for img in product.sku_images
                ],
            },
            "download_result": {
                "total": download_result.total,
                "success": download_result.success,
                "failed": download_result.failed,
                "success_rate": download_result.success_rate,
            },
        }

        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
