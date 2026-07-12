from PySide6.QtCore import QThread, Signal

from core.detector import PlatformDetector
from core.downloader import ImageDownloader
from core.file_manager import FileManager
from core.task_logger import TaskLogger
from parsers import get_parser


class ParseWorker(QThread):
    """
    商品解析线程。

    使用 QThread 防止 UI 卡死。
    """

    log_signal = Signal(str)
    success_signal = Signal(object)
    error_signal = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            self.log_signal.emit("开始识别平台...")

            platform, product_id = PlatformDetector.detect(self.url)

            if platform == "unknown":
                self.error_signal.emit("暂不支持该平台，请输入京东、淘宝、天猫或拼多多商品链接。")
                return

            self.log_signal.emit(f"平台识别成功：{platform}，商品ID：{product_id or '未识别'}")
            self.log_signal.emit("开始解析商品数据...")

            parser = get_parser(platform, log_callback=self.log_signal.emit)
            product = parser.parse(self.url)

            # 防止登录页、错误页被当成商品页
            if product.title.strip() in ["登录", "请登录", "用户登录"]:
                self.error_signal.emit("当前采集到的是登录页，不是商品详情页。请完成登录后重新解析。")
                return

            if product.total_count() == 0:
                self.error_signal.emit("未识别到商品图片。可能仍未登录成功，或页面结构发生变化。")
                return

            self.log_signal.emit(f"商品标题：{product.title}")
            self.log_signal.emit(f"主图识别：{len(product.main_images)} 张")

            self.log_signal.emit(f"详情图识别：{len(product.detail_images)} 张")
            self.log_signal.emit(f"SKU图识别：{len(product.sku_images)} 张")

            self.success_signal.emit(product)

        except Exception as e:
            self.error_signal.emit(f"解析失败：{e}")


class DownloadWorker(QThread):
    """
    图片下载线程。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int)
    success_signal = Signal(object, object)
    error_signal = Signal(str)

    def __init__(self, product, base_dir: str, selected_types: dict[str, bool]):
        super().__init__()
        self.product = product
        self.base_dir = base_dir
        self.selected_types = selected_types

    def run(self):
        try:
            self.log_signal.emit("开始创建商品文件夹...")

            product_dir = FileManager.create_product_dir(self.base_dir, self.product)
            dirs_by_type = FileManager.create_type_dirs(product_dir, self.selected_types)

            images_by_type = {
                "main": self.product.main_images if self.selected_types.get("main") else [],
                "detail": self.product.detail_images if self.selected_types.get("detail") else [],
                "sku": self.product.sku_images if self.selected_types.get("sku") else [],
            }

            downloader = ImageDownloader()

            def progress_callback(current, total):
                value = int(current / total * 100) if total else 0
                self.progress_signal.emit(value)

            def log_callback(message):
                self.log_signal.emit(message)

            result = downloader.download_images(
                images_by_type=images_by_type,
                dirs_by_type=dirs_by_type,
                progress_callback=progress_callback,
                log_callback=log_callback,
            )

            TaskLogger.save_log(
                product_dir=product_dir,
                product=self.product,
                selected_types=self.selected_types,
                download_result=result,
            )

            TaskLogger.save_product_json(
                product_dir=product_dir,
                product=self.product,
                download_result=result,
            )

            self.log_signal.emit(f"下载完成，成功率：{result.success_rate}%")
            self.success_signal.emit(product_dir, result)

        except Exception as e:
            self.error_signal.emit(f"下载失败：{e}")
