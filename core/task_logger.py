import json
from pathlib import Path
from datetime import datetime

from core.models import ProductData, DownloadResult


class TaskLogger:
    """
    生成采集日志、商品数据 JSON、批量下载报告、失败清单。
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

    @staticmethod
    def save_batch_report(
        base_dir: str,
        batch_items: list[dict],
        aggregate_result: DownloadResult,
        selected_types: dict[str, bool],
        failed_parse_items: list[dict] | None = None,
    ) -> dict:
        """
        保存批量下载报告和失败清单。

        目录结构：
            output/
            ├── 下载报告/
            │   └── 批量下载报告_时间.txt
            └── 失败清单/
                └── 失败清单_时间.txt
        """

        base_path = Path(base_dir)
        report_dir = base_path / "下载报告"
        failed_dir = base_path / "失败清单"

        report_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        report_path = report_dir / f"批量下载报告_{timestamp}.txt"
        failed_path = failed_dir / f"失败清单_{timestamp}.txt"

        failed_parse_items = failed_parse_items or []

        # ------------------------------------------------------------
        # 批量下载报告
        # ------------------------------------------------------------

        lines = []
        lines.append("批量下载报告")
        lines.append("=" * 60)
        lines.append(f"任务时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"商品总数：{len(batch_items)}")
        lines.append(f"解析失败链接：{len(failed_parse_items)} 个")
        lines.append("")
        lines.append("下载选择：")
        lines.append(f"主图：{'是' if selected_types.get('main') else '否'}")
        lines.append(f"详情图：{'是' if selected_types.get('detail') else '否'}")
        lines.append(f"SKU图：{'是' if selected_types.get('sku') else '否'}")
        lines.append("")
        lines.append("下载汇总：")
        lines.append(f"计划下载：{aggregate_result.total} 张")
        lines.append(f"成功下载：{aggregate_result.success} 张")
        lines.append(f"失败下载：{aggregate_result.failed} 张")
        lines.append(f"成功率：{aggregate_result.success_rate}%")
        lines.append("")
        lines.append("=" * 60)
        lines.append("商品明细")
        lines.append("=" * 60)
        lines.append("")

        for index, item in enumerate(batch_items, start=1):
            product = item["product"]
            product_dir = item["product_dir"]
            result = item["download_result"]

            lines.append(f"[{index}] {product.title}")
            lines.append(f"    平台：{product.platform}")
            lines.append(f"    商品ID：{product.product_id}")
            lines.append(f"    商品链接：{product.url}")
            lines.append(f"    保存路径：{product_dir}")
            lines.append("    识别结果：")
            lines.append(f"        主图：{len(product.main_images)} 张")
            lines.append(f"        详情图：{len(product.detail_images)} 张")
            lines.append(f"        SKU图：{len(product.sku_images)} 张")
            lines.append(f"        总计：{product.total_count()} 张")
            lines.append("    下载结果：")
            lines.append(f"        计划下载：{result.total} 张")
            lines.append(f"        成功下载：{result.success} 张")
            lines.append(f"        失败下载：{result.failed} 张")
            lines.append(f"        成功率：{result.success_rate}%")
            lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")

        # ------------------------------------------------------------
        # 失败清单
        # ------------------------------------------------------------

        failed_lines = []
        failed_lines.append("失败清单")
        failed_lines.append("=" * 60)
        failed_lines.append(f"任务时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        failed_lines.append("")

        has_failed = False

        if failed_parse_items:
            has_failed = True
            failed_lines.append("一、解析失败链接")
            failed_lines.append("-" * 60)

            for index, item in enumerate(failed_parse_items, start=1):
                failed_lines.append(f"[{index}]")
                failed_lines.append(f"链接：{item.get('url', '')}")
                failed_lines.append(f"原因：{item.get('reason', '')}")
                failed_lines.append("")

        download_failed_count = 0

        for batch_item in batch_items:
            result = batch_item["download_result"]

            if result.failed_items:
                has_failed = True
                download_failed_count += len(result.failed_items)

        if download_failed_count:
            failed_lines.append("二、下载失败图片")
            failed_lines.append("-" * 60)

            item_index = 1

            for batch_item in batch_items:
                product = batch_item["product"]
                result = batch_item["download_result"]

                if not result.failed_items:
                    continue

                failed_lines.append(f"商品：{product.title}")
                failed_lines.append(f"平台：{product.platform}")
                failed_lines.append(f"商品ID：{product.product_id}")
                failed_lines.append(f"商品链接：{product.url}")

                for failed in result.failed_items:
                    failed_lines.append(f"    [{item_index}]")
                    failed_lines.append(f"    类型：{failed.image_type}")
                    failed_lines.append(f"    文件名：{failed.filename}")
                    failed_lines.append(f"    URL：{failed.url}")
                    failed_lines.append(f"    原因：{failed.reason}")
                    failed_lines.append("")
                    item_index += 1

        if not has_failed:
            failed_lines.append("无失败链接或失败图片。")

        failed_path.write_text("\n".join(failed_lines), encoding="utf-8")

        return {
            "report_path": report_path,
            "failed_path": failed_path,
        }
