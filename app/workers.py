from PySide6.QtCore import QThread, Signal

from core.detector import PlatformDetector
from core.downloader import ImageDownloader
from core.file_manager import FileManager
from core.task_logger import TaskLogger
from core.models import DownloadResult
from parsers import get_parser


class BatchParseWorker(QThread):
    """
    批量商品解析线程。

    支持：
    1. 单链接；
    2. 多链接；
    3. 单个失败不影响后续链接；
    4. 返回成功商品列表和失败链接列表；
    5. 支持停止任务。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int)
    success_signal = Signal(object)
    error_signal = Signal(str)
    stopped_signal = Signal(object)

    def __init__(self, urls: list[str]):
        super().__init__()
        self.urls = urls or []
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

                    parser = get_parser(platform, log_callback=self.log_signal.emit)
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

    支持：
    1. 单商品下载；
    2. 多商品批量下载；
    3. 每个商品独立文件夹；
    4. 下载失败自动重试；
    5. 每个商品生成采集日志和商品 JSON；
    6. 批量任务生成下载报告和失败清单；
    7. 支持停止任务；
    8. 支持高清图优先下载。
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
    ):
        super().__init__()
        self.products = products or []
        self.base_dir = base_dir
        self.selected_types = selected_types
        self.failed_parse_items = failed_parse_items or []
        self.high_quality = high_quality
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

                product_dir = FileManager.create_product_dir(self.base_dir, product)
                last_product_dir = product_dir

                dirs_by_type = FileManager.create_type_dirs(product_dir, self.selected_types)

                images_by_type = {
                    "main": product.main_images if self.selected_types.get("main") else [],
                    "detail": product.detail_images if self.selected_types.get("detail") else [],
                    "sku": product.sku_images if self.selected_types.get("sku") else [],
                }

                downloader = ImageDownloader(
                    timeout=20,
                    retries=3,
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

                finished_images += result.total

                aggregate_result.success += result.success
                aggregate_result.failed += result.failed
                aggregate_result.failed_items.extend(result.failed_items)

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
                        f"{prefix} 下载完成：成功 {result.success} 张，失败 {result.failed} 张。"
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

            self.log_signal.emit(f"下载报告已生成：{report_paths['report_path']}")
            self.log_signal.emit(f"失败清单已生成：{report_paths['failed_path']}")

            if self.is_stop_requested():
                self.stopped_signal.emit(
                    {
                        "base_dir": self.base_dir,
                        "last_product_dir": last_product_dir,
                        "result": aggregate_result,
                    }
                )
                return

            self.progress_signal.emit(100)

            if total_products == 1:
                self.log_signal.emit(f"下载完成，成功率：{aggregate_result.success_rate}%")
            else:
                self.log_signal.emit(
                    f"批量下载完成：商品 {total_products} 个，"
                    f"成功图片 {aggregate_result.success} 张，"
                    f"失败图片 {aggregate_result.failed} 张，"
                    f"成功率 {aggregate_result.success_rate}%"
                )

            self.success_signal.emit(self.base_dir, last_product_dir, aggregate_result)

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


# 兼容旧名称
ParseWorker = BatchParseWorker
DownloadWorker = BatchDownloadWorker
