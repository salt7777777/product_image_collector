from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.detector import PlatformDetector
from core.downloader import ImageDownloader
from core.file_manager import FileManager
from core.task_logger import TaskLogger
from core.image_link_reporter import ImageLinkReportExporter
from core.models import DownloadResult, DuplicateImage, ConvertedImage, SmallImage
from parsers import get_parser
from utils.file_hash import dedupe_image_files
from utils.image_converter import convert_image_files
from utils.image_filter import filter_small_images


class BatchParseWorker(QThread):
    """
    批量商品解析线程。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int)
    success_signal = Signal(object)
    error_signal = Signal(str)
    stopped_signal = Signal(object)

    def __init__(
        self,
        urls: list[str],
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
        super().__init__()
        self.urls = urls or []
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def run(self):
        products = []
        failed = []

        total = len(self.urls)

        if total == 0:
            self.error_signal.emit("没有可解析的商品链接。")
            return

        try:
            for index, url in enumerate(self.urls, start=1):
                if self.is_stop_requested():
                    self.log_signal.emit("解析任务已停止。")
                    self.stopped_signal.emit(
                        {
                            "products": products,
                            "failed": failed,
                            "stopped": True,
                        }
                    )
                    return

                prefix = f"[{index}/{total}]"

                try:
                    self.log_signal.emit(f"{prefix} 开始识别平台...")

                    platform, product_id = PlatformDetector.detect(url)

                    if platform == "unknown":
                        message = "暂不支持该平台，请输入京东、淘宝、天猫或拼多多商品链接。"
                        self.log_signal.emit(f"{prefix} 解析失败：{message}")
                        failed.append(
                            {
                                "url": url,
                                "reason": message,
                            }
                        )
                        self._emit_progress(index, total)
                        continue

                    self.log_signal.emit(
                        f"{prefix} 平台识别成功：{platform}，商品ID：{product_id or '未识别'}"
                    )
                    self.log_signal.emit(f"{prefix} 开始解析商品数据...")

                    parser = get_parser(
                        platform,
                        log_callback=self.log_signal.emit,
                        headless=self.headless,
                        login_wait_seconds=self.login_wait_seconds,
                    )

                    product = parser.parse(url)

                    if self.is_stop_requested():
                        self.log_signal.emit("解析任务已停止。")
                        self.stopped_signal.emit(
                            {
                                "products": products,
                                "failed": failed,
                                "stopped": True,
                            }
                        )
                        return

                    if product.title.strip() in ["登录", "请登录", "用户登录"]:
                        message = "当前采集到的是登录页，不是商品详情页。请完成登录后重新解析。"
                        self.log_signal.emit(f"{prefix} 解析失败：{message}")
                        failed.append(
                            {
                                "url": url,
                                "reason": message,
                            }
                        )
                        self._emit_progress(index, total)
                        continue

                    if product.total_count() == 0:
                        message = "未识别到商品图片。可能仍未登录成功，或页面结构发生变化。"
                        self.log_signal.emit(f"{prefix} 解析失败：{message}")
                        failed.append(
                            {
                                "url": url,
                                "reason": message,
                            }
                        )
                        self._emit_progress(index, total)
                        continue

                    products.append(product)

                    self.log_signal.emit(f"{prefix} 商品标题：{product.title}")
                    self.log_signal.emit(
                        f"{prefix} 解析成功：主图 {len(product.main_images)} 张，"
                        f"详情图 {len(product.detail_images)} 张，"
                        f"SKU图 {len(product.sku_images)} 张"
                    )

                except Exception as e:
                    message = str(e)
                    self.log_signal.emit(f"{prefix} 解析失败：{message}")
                    failed.append(
                        {
                            "url": url,
                            "reason": message,
                        }
                    )

                self._emit_progress(index, total)

            if not products:
                if failed:
                    self.error_signal.emit(f"批量解析失败：全部 {len(failed)} 个链接解析失败。")
                else:
                    self.error_signal.emit("批量解析失败：未解析到任何商品。")
                return

            if total == 1:
                self.log_signal.emit("商品解析完成。")
            else:
                self.log_signal.emit(
                    f"批量解析完成：成功 {len(products)} 个，失败 {len(failed)} 个。"
                )

            self.success_signal.emit(
                {
                    "products": products,
                    "failed": failed,
                    "stopped": False,
                }
            )

        except Exception as e:
            self.error_signal.emit(f"解析任务失败：{e}")

    def _emit_progress(self, current: int, total: int):
        value = int(current / total * 100) if total else 0
        self.progress_signal.emit(value)


class BatchDownloadWorker(QThread):
    """
    批量图片下载线程。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int)
    success_signal = Signal(object, object, object)
    error_signal = Signal(str)
    stopped_signal = Signal(object)

    def __init__(
        self,
        products: list,
        base_dir: str,
        selected_types: dict[str, bool],
        failed_parse_items: list[dict] | None = None,
        high_quality: bool = False,
        download_timeout: int = 20,
        download_retries: int = 3,
        organize_by_date: bool = False,
        organize_by_platform: bool = False,
        dedupe_images: bool = False,
        image_output_format: str = "original",
        filter_small_images_enabled: bool = False,
        min_image_width: int = 300,
        min_image_height: int = 300,
    ):
        super().__init__()
        self.products = products or []
        self.base_dir = base_dir
        self.selected_types = selected_types
        self.failed_parse_items = failed_parse_items or []
        self.high_quality = high_quality
        self.download_timeout = download_timeout
        self.download_retries = download_retries
        self.organize_by_date = organize_by_date
        self.organize_by_platform = organize_by_platform
        self.dedupe_images = dedupe_images
        self.image_output_format = image_output_format or "original"

        self.filter_small_images_enabled = filter_small_images_enabled
        self.min_image_width = min_image_width
        self.min_image_height = min_image_height

        self.retry_items = []
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def run(self):
        if not self.products:
            self.error_signal.emit("没有可下载的商品，请先解析商品。")
            return

        try:
            total_products = len(self.products)

            total_images = self._count_total_images()
            finished_images = 0

            aggregate_result = DownloadResult(total=total_images)
            last_product_dir = None
            batch_items = []

            if total_products == 1:
                self.log_signal.emit("开始创建商品文件夹...")
            else:
                self.log_signal.emit(f"开始批量下载，共 {total_products} 个商品...")

            for product_index, product in enumerate(self.products, start=1):
                if self.is_stop_requested():
                    self.log_signal.emit("下载任务已停止。")
                    break

                prefix = f"[{product_index}/{total_products}]"

                if total_products > 1:
                    self.log_signal.emit(f"{prefix} 开始下载：{product.title}")

                output_base_dir = FileManager.resolve_output_base_dir(
                    base_dir=self.base_dir,
                    product=product,
                    organize_by_date=self.organize_by_date,
                    organize_by_platform=self.organize_by_platform,
                )

                product_dir = FileManager.create_product_dir(output_base_dir, product)
                last_product_dir = product_dir

                dirs_by_type = FileManager.create_type_dirs(product_dir, self.selected_types)

                images_by_type = {
                    "main": product.main_images if self.selected_types.get("main") else [],
                    "detail": product.detail_images if self.selected_types.get("detail") else [],
                    "sku": product.sku_images if self.selected_types.get("sku") else [],
                }

                downloader = ImageDownloader(
                    timeout=self.download_timeout,
                    retries=self.download_retries,
                    delay=0.3,
                    retry_delay=0.5,
                    high_quality=self.high_quality,
                )

                def progress_callback(current, total):
                    if total_images <= 0:
                        self.progress_signal.emit(0)
                        return

                    value = int((finished_images + current) / total_images * 100)
                    self.progress_signal.emit(value)

                def log_callback(message):
                    if total_products > 1:
                        self.log_signal.emit(f"{prefix} {message}")
                    else:
                        self.log_signal.emit(message)

                result = downloader.download_images(
                    images_by_type=images_by_type,
                    dirs_by_type=dirs_by_type,
                    progress_callback=progress_callback,
                    log_callback=log_callback,
                    cancel_callback=self.is_stop_requested,
                )

                # ------------------------------------------------------------
                # 收集失败图片，供“重试失败”使用
                # ------------------------------------------------------------
                for failed in result.failed_items:
                    self.retry_items.append(
                        {
                            "product_title": product.title,
                            "platform": product.platform,
                            "product_id": product.product_id,
                            "product_url": product.url,
                            "product_dir": str(product_dir),
                            "image_type": failed.image_type,
                            "url": failed.url,
                            "filename": failed.filename,
                            "reason": failed.reason,
                        }
                    )

                # ------------------------------------------------------------
                # 安全 MD5 去重
                # ------------------------------------------------------------
                if self.dedupe_images and not self.is_stop_requested():
                    self.log_signal.emit(f"{prefix} 开始执行 MD5 图片去重...")
                    self.log_signal.emit(
                        f"{prefix} 安全模式：仅在同类型目录内去重，"
                        f"重复图片移动到 _重复图片备份。"
                    )

                    dedupe_result = dedupe_image_files(
                        root_dir=product_dir,
                        same_folder_only=True,
                        move_to_backup=True,
                        min_file_size=1024,
                    )

                    result.duplicate_removed = dedupe_result.removed_count
                    result.duplicate_removed_bytes = dedupe_result.removed_bytes
                    result.duplicate_items = [
                        DuplicateImage(
                            original_path=item.original_path,
                            duplicate_path=item.duplicate_path,
                            md5=item.md5,
                            size=item.size,
                        )
                        for item in dedupe_result.duplicate_items
                    ]

                    if dedupe_result.removed_count > 0:
                        self.log_signal.emit(
                            f"{prefix} MD5去重完成：扫描 {dedupe_result.scanned_count} 张，"
                            f"发现重复图片 {dedupe_result.removed_count} 张，"
                            f"已移动到 _重复图片备份。"
                        )
                    else:
                        self.log_signal.emit(
                            f"{prefix} MD5去重完成：扫描 {dedupe_result.scanned_count} 张，"
                            f"未发现重复图片。"
                        )

                # ------------------------------------------------------------
                # 图片格式转换
                # ------------------------------------------------------------
                if self.image_output_format != "original" and not self.is_stop_requested():
                    self.log_signal.emit(
                        f"{prefix} 开始执行图片格式转换：{self.image_output_format.upper()}..."
                    )
                    self.log_signal.emit(
                        f"{prefix} 格式转换可能需要一些时间，尤其是 PNG，请勿关闭程序。"
                    )

                    last_logged_percent = {"value": -1}

                    def convert_progress_callback(current, total, path):
                        if total <= 0:
                            return

                        percent = int(current / total * 100)

                        progress_value = 95 + int(percent * 4 / 100)
                        self.progress_signal.emit(min(progress_value, 99))

                        should_log = (
                            current == 1
                            or current == total
                            or percent >= last_logged_percent["value"] + 10
                        )

                        if should_log:
                            last_logged_percent["value"] = percent
                            self.log_signal.emit(
                                f"{prefix} 格式转换中：{current}/{total}，进度 {percent}%"
                            )

                    convert_result = convert_image_files(
                        root_dir=product_dir,
                        target_format=self.image_output_format,
                        backup_dir_name="_格式转换备份",
                        exclude_dir_names={
                            "_重复图片备份",
                            "_格式转换备份",
                            "_小图过滤",
                        },
                        quality=92,
                        progress_callback=convert_progress_callback,
                    )

                    result.converted_count = convert_result.converted_count
                    result.convert_failed = convert_result.failed_count
                    result.converted_items = [
                        ConvertedImage(
                            original_path=item.original_path,
                            output_path=item.output_path,
                            backup_path=item.backup_path,
                            source_format=item.source_format,
                            target_format=item.target_format,
                            success=item.success,
                            reason=item.reason,
                        )
                        for item in convert_result.items
                    ]

                    self.progress_signal.emit(99)

                    self.log_signal.emit(
                        f"{prefix} 图片格式转换完成：扫描 {convert_result.scanned_count} 张，"
                        f"转换成功 {convert_result.converted_count} 张，"
                        f"跳过 {convert_result.skipped_count} 张，"
                        f"失败 {convert_result.failed_count} 张。"
                    )

                # ------------------------------------------------------------
                # 小图过滤
                # ------------------------------------------------------------
                if self.filter_small_images_enabled and not self.is_stop_requested():
                    self.log_signal.emit(
                        f"{prefix} 开始执行小图过滤："
                        f"最小宽 {self.min_image_width}px，最小高 {self.min_image_height}px..."
                    )

                    last_logged_percent = {"value": -1}

                    def small_filter_progress_callback(current, total, path):
                        if total <= 0:
                            return

                        percent = int(current / total * 100)

                        progress_value = 95 + int(percent * 4 / 100)
                        self.progress_signal.emit(min(progress_value, 99))

                        should_log = (
                            current == 1
                            or current == total
                            or percent >= last_logged_percent["value"] + 20
                        )

                        if should_log:
                            last_logged_percent["value"] = percent
                            self.log_signal.emit(
                                f"{prefix} 小图过滤中：{current}/{total}，进度 {percent}%"
                            )

                    small_result = filter_small_images(
                        root_dir=product_dir,
                        min_width=self.min_image_width,
                        min_height=self.min_image_height,
                        backup_dir_name="_小图过滤",
                        exclude_dir_names={
                            "_重复图片备份",
                            "_格式转换备份",
                            "_小图过滤",
                        },
                        progress_callback=small_filter_progress_callback,
                    )

                    result.small_filtered_count = small_result.filtered_count
                    result.small_filter_failed = small_result.failed_count
                    result.small_image_items = [
                        SmallImage(
                            original_path=item.original_path,
                            backup_path=item.backup_path,
                            width=item.width,
                            height=item.height,
                            reason=item.reason,
                        )
                        for item in small_result.items
                    ]

                    self.progress_signal.emit(99)

                    self.log_signal.emit(
                        f"{prefix} 小图过滤完成：扫描 {small_result.scanned_count} 张，"
                        f"过滤 {small_result.filtered_count} 张，"
                        f"失败 {small_result.failed_count} 张。"
                    )

                finished_images += result.total

                aggregate_result.success += result.success
                aggregate_result.failed += result.failed
                aggregate_result.failed_items.extend(result.failed_items)

                aggregate_result.duplicate_removed += result.duplicate_removed
                aggregate_result.duplicate_removed_bytes += result.duplicate_removed_bytes
                aggregate_result.duplicate_items.extend(result.duplicate_items)

                aggregate_result.converted_count += result.converted_count
                aggregate_result.convert_failed += result.convert_failed
                aggregate_result.converted_items.extend(result.converted_items)

                aggregate_result.small_filtered_count += result.small_filtered_count
                aggregate_result.small_filter_failed += result.small_filter_failed
                aggregate_result.small_image_items.extend(result.small_image_items)

                TaskLogger.save_log(
                    product_dir=product_dir,
                    product=product,
                    selected_types=self.selected_types,
                    download_result=result,
                )

                TaskLogger.save_product_json(
                    product_dir=product_dir,
                    product=product,
                    download_result=result,
                )

                batch_items.append(
                    {
                        "product": product,
                        "product_dir": product_dir,
                        "download_result": result,
                    }
                )

                if total_products > 1:
                    self.log_signal.emit(
                        f"{prefix} 下载完成：成功 {result.success} 张，"
                        f"失败 {result.failed} 张，"
                        f"去重处理 {result.duplicate_removed} 张，"
                        f"格式转换 {result.converted_count} 张，"
                        f"小图过滤 {result.small_filtered_count} 张。"
                    )

                if self.is_stop_requested():
                    self.log_signal.emit("下载任务已停止。")
                    break

            report_paths = TaskLogger.save_batch_report(
                base_dir=self.base_dir,
                batch_items=batch_items,
                aggregate_result=aggregate_result,
                selected_types=self.selected_types,
                failed_parse_items=self.failed_parse_items,
            )
            
            try:
                image_link_report_path = ImageLinkReportExporter.save_image_link_report(
                    base_dir=self.base_dir,
                    batch_items=batch_items,
                    selected_types=self.selected_types,
                )
                report_paths["image_link_report_path"] = image_link_report_path
                report_paths["image_link_report_error"] = ""
            except Exception as e:
                report_paths["image_link_report_path"] = None
                report_paths["image_link_report_error"] = str(e)


            self.log_signal.emit(f"下载报告已生成：{report_paths['report_path']}")

            if report_paths.get("excel_path"):
                self.log_signal.emit(f"Excel报告已生成：{report_paths['excel_path']}")
            else:
                excel_error = report_paths.get("excel_error")
                if excel_error:
                    self.log_signal.emit(f"Excel报告生成失败：{excel_error}")

            if report_paths.get("image_link_report_path"):
                self.log_signal.emit(
                    f"商品图片链接总表已生成：{report_paths['image_link_report_path']}"
                )
            else:
                image_link_report_error = report_paths.get("image_link_report_error")
                if image_link_report_error:
                    self.log_signal.emit(f"商品图片链接总表生成失败：{image_link_report_error}")

            self.log_signal.emit(f"失败清单已生成：{report_paths['failed_path']}")


            if self.is_stop_requested():
                self.stopped_signal.emit(
                    {
                        "base_dir": self.base_dir,
                        "last_product_dir": last_product_dir,
                        "result": aggregate_result,
                        "retry_items": self.retry_items,
                    }
                )
                return

            self.progress_signal.emit(100)

            if total_products == 1:
                self.log_signal.emit(
                    f"下载完成，成功率：{aggregate_result.success_rate}%，"
                    f"去重处理：{aggregate_result.duplicate_removed} 张，"
                    f"格式转换：{aggregate_result.converted_count} 张，"
                    f"小图过滤：{aggregate_result.small_filtered_count} 张"
                )
            else:
                self.log_signal.emit(
                    f"批量下载完成：商品 {total_products} 个，"
                    f"成功图片 {aggregate_result.success} 张，"
                    f"失败图片 {aggregate_result.failed} 张，"
                    f"去重处理 {aggregate_result.duplicate_removed} 张，"
                    f"格式转换 {aggregate_result.converted_count} 张，"
                    f"小图过滤 {aggregate_result.small_filtered_count} 张，"
                    f"成功率 {aggregate_result.success_rate}%"
                )

            self.success_signal.emit(
                self.base_dir,
                last_product_dir,
                {
                    "result": aggregate_result,
                    "retry_items": self.retry_items,
                },
            )

        except Exception as e:
            self.error_signal.emit(f"下载失败：{e}")

    def _count_total_images(self) -> int:
        total = 0

        for product in self.products:
            if self.selected_types.get("main"):
                total += len(product.main_images)

            if self.selected_types.get("detail"):
                total += len(product.detail_images)

            if self.selected_types.get("sku"):
                total += len(product.sku_images)

        return total


class RetryFailedDownloadWorker(QThread):
    """
    失败图片重试线程。

    只重试上一次下载失败的图片，不重新下载全部图片。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int)
    success_signal = Signal(object)
    error_signal = Signal(str)
    stopped_signal = Signal(object)

    TYPE_DIR_NAMES = {
        "main": "主图",
        "detail": "详情图",
        "sku": "SKU图",
    }

    def __init__(
        self,
        retry_items: list[dict],
        download_timeout: int = 20,
        download_retries: int = 3,
        high_quality: bool = False,
    ):
        super().__init__()
        self.retry_items = retry_items or []
        self.download_timeout = download_timeout
        self.download_retries = download_retries
        self.high_quality = high_quality
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def run(self):
        if not self.retry_items:
            self.error_signal.emit("没有可重试的失败图片。")
            return

        result = DownloadResult(total=len(self.retry_items))
        still_failed_items = []

        try:
            total = len(self.retry_items)

            self.log_signal.emit(f"开始重试失败图片，共 {total} 张...")

            downloader = ImageDownloader(
                timeout=self.download_timeout,
                retries=self.download_retries,
                delay=0.2,
                retry_delay=0.5,
                high_quality=self.high_quality,
            )

            for index, item in enumerate(self.retry_items, start=1):
                if self.is_stop_requested():
                    self.log_signal.emit("失败图片重试任务已停止。")
                    self.stopped_signal.emit(
                        {
                            "result": result,
                            "retry_items": still_failed_items,
                        }
                    )
                    return

                image_type = item.get("image_type", "")
                image_url = item.get("url", "")
                filename = item.get("filename", "")
                product_title = item.get("product_title", "")
                product_dir = Path(item.get("product_dir", ""))

                type_dir_name = self.TYPE_DIR_NAMES.get(image_type, image_type or "失败图片")
                save_dir = product_dir / type_dir_name
                save_dir.mkdir(parents=True, exist_ok=True)

                if not filename:
                    filename = f"retry_{index:03d}.jpg"

                save_path = save_dir / filename

                prefix = f"[{index}/{total}]"

                self.log_signal.emit(
                    f"{prefix} 重试下载：{product_title} / {type_dir_name} / {filename}"
                )

                ok, reason = downloader._download_one(
                    url=image_url,
                    save_path=save_path,
                    filename=filename,
                    log_callback=self.log_signal.emit,
                    cancel_callback=self.is_stop_requested,
                )

                if self.is_stop_requested():
                    self.log_signal.emit("失败图片重试任务已停止。")
                    self.stopped_signal.emit(
                        {
                            "result": result,
                            "retry_items": still_failed_items,
                        }
                    )
                    return

                if ok:
                    result.success += 1
                    self.log_signal.emit(f"{prefix} 重试成功：{filename}")
                else:
                    result.failed += 1
                    self.log_signal.emit(f"{prefix} 重试失败：{filename}，原因：{reason}")

                    retry_item = dict(item)
                    retry_item["reason"] = reason
                    still_failed_items.append(retry_item)

                self.progress_signal.emit(int(index / total * 100))

            self.progress_signal.emit(100)

            self.log_signal.emit(
                f"失败图片重试完成：成功 {result.success} 张，"
                f"失败 {result.failed} 张，成功率 {result.success_rate}%"
            )

            self.success_signal.emit(
                {
                    "result": result,
                    "retry_items": still_failed_items,
                }
            )

        except Exception as e:
            self.error_signal.emit(f"重试失败图片任务异常：{e}")


# 兼容旧名称
ParseWorker = BatchParseWorker
DownloadWorker = BatchDownloadWorker
