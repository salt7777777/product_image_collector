from datetime import datetime
from pathlib import Path
import os
import re
from urllib.parse import urlparse, parse_qs

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QLabel,
    QTextEdit,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFileDialog,
    QCheckBox,
    QProgressBar,
    QMessageBox,
    QSpinBox,
    QComboBox,
)

from app.workers import BatchParseWorker, BatchDownloadWorker
from core.preview_generator import PreviewGenerator
from core.app_config import AppConfig


class MainWindow(QMainWindow):
    """
    主窗口。

    支持：
    1. 单链接解析下载；
    2. 多链接批量解析下载；
    3. 自动简化商品长链接；
    4. 导入 TXT / CSV / Excel 链接文件；
    5. 清空日志；
    6. 停止当前解析 / 下载任务；
    7. 主图 / 详情图 / SKU 图选择；
    8. 高清图优先下载；
    9. 图片 HTML 预览；
    10. 批量任务进度；
    11. 下载失败重试；
    12. 批量下载报告、失败清单、Excel 报告；
    13. 配置自动保存和恢复；
    14. 下载超时、重试次数、登录等待时间配置；
    15. 浏览器显示 / 隐藏配置；
    16. 按日期 / 平台分类保存；
    17. 下载后安全 MD5 去重；
    18. 图片格式转换。
    """

    def __init__(self):
        super().__init__()

        self.config = AppConfig.load()

        self.setWindowTitle("商品图片采集工具")
        self.resize(920, 780)

        self.product = None
        self.products = []
        self.failed_parse_items = []

        self.last_product_dir = None
        self.last_base_dir = None
        self.last_preview_path = None

        self.parse_worker = None
        self.download_worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)

        # 商品链接输入区域
        input_group = QGroupBox("商品链接")
        input_layout = QVBoxLayout(input_group)

        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText(
            "请粘贴商品链接，支持单个或批量链接。\n"
            "批量链接建议每行一个。\n"
            "支持导入 TXT / CSV / Excel 文件。\n"
            "长链接会自动简化。"
        )
        self.url_input.setFixedHeight(110)

        input_layout.addWidget(self.url_input)
        main_layout.addWidget(input_group)

        # 商品信息区域
        info_group = QGroupBox("商品信息")
        info_layout = QVBoxLayout(info_group)

        self.platform_label = QLabel("平台：-")
        self.product_id_label = QLabel("商品ID：-")
        self.title_label = QLabel("商品标题：-")

        info_layout.addWidget(self.platform_label)
        info_layout.addWidget(self.product_id_label)
        info_layout.addWidget(self.title_label)

        main_layout.addWidget(info_group)

        # 识别结果和下载类型
        result_group = QGroupBox("识别结果与下载类型")
        result_layout = QVBoxLayout(result_group)

        row1 = QHBoxLayout()

        self.main_count_label = QLabel("主图：- 张")
        self.detail_count_label = QLabel("详情图：- 张")
        self.sku_count_label = QLabel("SKU图：- 张")

        row1.addWidget(self.main_count_label)
        row1.addWidget(self.detail_count_label)
        row1.addWidget(self.sku_count_label)
        row1.addStretch()

        row2 = QHBoxLayout()

        self.main_checkbox = QCheckBox("下载主图")
        self.detail_checkbox = QCheckBox("下载详情图")
        self.sku_checkbox = QCheckBox("下载SKU图")
        self.high_quality_checkbox = QCheckBox("尽量下载高清图")

        self.main_checkbox.setChecked(self.config.download_main)
        self.detail_checkbox.setChecked(self.config.download_detail)
        self.sku_checkbox.setChecked(self.config.download_sku)
        self.high_quality_checkbox.setChecked(self.config.high_quality)

        row2.addWidget(self.main_checkbox)
        row2.addWidget(self.detail_checkbox)
        row2.addWidget(self.sku_checkbox)
        row2.addWidget(self.high_quality_checkbox)
        row2.addStretch()

        result_layout.addLayout(row1)
        result_layout.addLayout(row2)

        main_layout.addWidget(result_group)

        # 高级设置区域
        settings_group = QGroupBox("高级设置")
        settings_layout = QVBoxLayout(settings_group)

        settings_row1 = QHBoxLayout()

        self.timeout_label = QLabel("下载超时(秒)：")
        self.timeout_spinbox = QSpinBox()
        self.timeout_spinbox.setRange(5, 120)
        self.timeout_spinbox.setValue(self.config.download_timeout)

        self.retries_label = QLabel("重试次数：")
        self.retries_spinbox = QSpinBox()
        self.retries_spinbox.setRange(0, 10)
        self.retries_spinbox.setValue(self.config.download_retries)

        self.login_wait_label = QLabel("登录等待(秒)：")
        self.login_wait_spinbox = QSpinBox()
        self.login_wait_spinbox.setRange(30, 600)
        self.login_wait_spinbox.setValue(self.config.login_wait_seconds)

        settings_row1.addWidget(self.timeout_label)
        settings_row1.addWidget(self.timeout_spinbox)
        settings_row1.addWidget(self.retries_label)
        settings_row1.addWidget(self.retries_spinbox)
        settings_row1.addWidget(self.login_wait_label)
        settings_row1.addWidget(self.login_wait_spinbox)
        settings_row1.addStretch()

        settings_row2 = QHBoxLayout()

        self.headless_checkbox = QCheckBox("隐藏浏览器窗口")
        self.headless_checkbox.setChecked(self.config.headless)

        self.organize_by_date_checkbox = QCheckBox("按日期分类保存")
        self.organize_by_date_checkbox.setChecked(self.config.organize_by_date)

        self.organize_by_platform_checkbox = QCheckBox("按平台分类保存")
        self.organize_by_platform_checkbox.setChecked(self.config.organize_by_platform)

        self.dedupe_images_checkbox = QCheckBox("下载后MD5去重")
        self.dedupe_images_checkbox.setChecked(self.config.dedupe_images)

        self.image_format_label = QLabel("输出格式：")
        self.image_format_combo = QComboBox()
        self.image_format_combo.addItem("保持原格式", "original")
        self.image_format_combo.addItem("全部转 JPG", "jpg")
        self.image_format_combo.addItem("全部转 PNG", "png")
        self.image_format_combo.addItem("全部转 WEBP", "webp")
        self._set_image_format_combo_value(self.config.image_output_format)

        settings_row2.addWidget(self.headless_checkbox)
        settings_row2.addWidget(self.organize_by_date_checkbox)
        settings_row2.addWidget(self.organize_by_platform_checkbox)
        settings_row2.addWidget(self.dedupe_images_checkbox)
        settings_row2.addWidget(self.image_format_label)
        settings_row2.addWidget(self.image_format_combo)
        settings_row2.addStretch()

        settings_layout.addLayout(settings_row1)
        settings_layout.addLayout(settings_row2)

        main_layout.addWidget(settings_group)

        # 保存路径区域
        path_group = QGroupBox("保存路径")
        path_layout = QHBoxLayout(path_group)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("请选择图片保存路径")
        self.path_input.setText(self.config.save_dir)

        self.choose_path_button = QPushButton("选择目录")

        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.choose_path_button)

        main_layout.addWidget(path_group)

        # 操作按钮区域
        button_layout = QHBoxLayout()

        self.parse_button = QPushButton("解析商品")
        self.download_button = QPushButton("开始下载")
        self.stop_button = QPushButton("停止任务")
        self.preview_button = QPushButton("预览图片")
        self.open_dir_button = QPushButton("打开保存目录")
        self.import_links_button = QPushButton("导入链接文件")
        self.clear_log_button = QPushButton("清空日志")

        self.download_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.open_dir_button.setEnabled(False)

        button_layout.addWidget(self.parse_button)
        button_layout.addWidget(self.download_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addWidget(self.preview_button)
        button_layout.addWidget(self.open_dir_button)
        button_layout.addWidget(self.import_links_button)
        button_layout.addWidget(self.clear_log_button)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        # 日志区域
        log_group = QGroupBox("实时日志")
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)

        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group)

    def _bind_events(self):
        self.choose_path_button.clicked.connect(self.choose_path)
        self.parse_button.clicked.connect(self.parse_product)
        self.download_button.clicked.connect(self.download_images)
        self.stop_button.clicked.connect(self.stop_current_task)
        self.preview_button.clicked.connect(self.preview_images)
        self.open_dir_button.clicked.connect(self.open_last_dir)
        self.import_links_button.clicked.connect(self.import_links_file)
        self.clear_log_button.clicked.connect(self.clear_log)

        self.path_input.textChanged.connect(self.save_current_config)

        self.main_checkbox.stateChanged.connect(self.save_current_config)
        self.detail_checkbox.stateChanged.connect(self.save_current_config)
        self.sku_checkbox.stateChanged.connect(self.save_current_config)
        self.high_quality_checkbox.stateChanged.connect(self.save_current_config)

        self.timeout_spinbox.valueChanged.connect(self.save_current_config)
        self.retries_spinbox.valueChanged.connect(self.save_current_config)
        self.login_wait_spinbox.valueChanged.connect(self.save_current_config)

        self.headless_checkbox.stateChanged.connect(self.save_current_config)
        self.organize_by_date_checkbox.stateChanged.connect(self.save_current_config)
        self.organize_by_platform_checkbox.stateChanged.connect(self.save_current_config)
        self.dedupe_images_checkbox.stateChanged.connect(self.save_current_config)
        self.image_format_combo.currentIndexChanged.connect(self.save_current_config)

    def choose_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if path:
            self.path_input.setText(path)

    def save_current_config(self):
        """
        保存当前界面配置到 config.json。
        """
        try:
            self.config.save_dir = self.path_input.text().strip() or str(Path("output").absolute())

            self.config.download_main = self.main_checkbox.isChecked()
            self.config.download_detail = self.detail_checkbox.isChecked()
            self.config.download_sku = self.sku_checkbox.isChecked()
            self.config.high_quality = self.high_quality_checkbox.isChecked()

            self.config.download_timeout = self.timeout_spinbox.value()
            self.config.download_retries = self.retries_spinbox.value()

            self.config.headless = self.headless_checkbox.isChecked()
            self.config.login_wait_seconds = self.login_wait_spinbox.value()

            self.config.organize_by_date = self.organize_by_date_checkbox.isChecked()
            self.config.organize_by_platform = self.organize_by_platform_checkbox.isChecked()
            self.config.dedupe_images = self.dedupe_images_checkbox.isChecked()

            self.config.image_output_format = self._get_image_format_combo_value()

            self.config.save()

        except Exception as e:
            try:
                self.log(f"保存配置失败：{e}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 链接导入
    # ------------------------------------------------------------------

    def import_links_file(self):
        """
        导入 TXT / CSV / Excel 链接文件。
        """

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "导入链接文件",
            "",
            "链接文件 (*.txt *.csv *.xlsx);;Excel文件 (*.xlsx);;文本文件 (*.txt);;CSV文件 (*.csv);;所有文件 (*.*)",
        )

        if not file_path:
            return

        try:
            path = Path(file_path)
            suffix = path.suffix.lower()

            if suffix == ".xlsx":
                imported_urls = self._read_excel_links(path)
            else:
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = path.read_text(encoding="gbk", errors="ignore")

                imported_urls = self._extract_and_simplify_urls_from_text(text)

            if not imported_urls:
                QMessageBox.warning(self, "导入失败", "文件中未识别到有效商品链接。")
                return

            current_urls = self._get_urls(update_input=False)

            merged = []
            seen = set()

            for url in current_urls + imported_urls:
                if url in seen:
                    continue

                seen.add(url)
                merged.append(url)

            added_count = max(len(merged) - len(current_urls), 0)
            duplicate_count = max(len(imported_urls) - added_count, 0)

            self.url_input.setPlainText("\n".join(merged))

            self.log(
                f"导入链接文件成功：{path.name}，"
                f"识别链接 {len(imported_urls)} 个，"
                f"新增 {added_count} 个，"
                f"重复 {duplicate_count} 个，"
                f"当前共 {len(merged)} 个。"
            )

        except ImportError:
            QMessageBox.critical(
                self,
                "导入失败",
                "导入 Excel 文件需要安装 openpyxl。\n\n请执行：\npip install openpyxl==3.1.5",
            )

        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"导入链接文件失败：{e}")

    def _read_excel_links(self, path: Path) -> list[str]:
        """
        从 Excel 文件中读取商品链接。
        """
        from openpyxl import load_workbook

        workbook = load_workbook(
            filename=path,
            read_only=True,
            data_only=True,
        )

        all_urls = []

        try:
            for sheet in workbook.worksheets:
                urls = self._read_links_from_worksheet(sheet)
                all_urls.extend(urls)

        finally:
            try:
                workbook.close()
            except Exception:
                pass

        return self._dedupe_url_list(all_urls)

    def _read_links_from_worksheet(self, sheet) -> list[str]:
        """
        从单个 Excel Sheet 中读取链接。
        """
        result = []

        url_column_index, header_row_index = self._find_excel_url_column(sheet)

        if url_column_index is not None:
            for row in sheet.iter_rows(
                min_row=header_row_index + 1,
                values_only=True,
            ):
                if not row:
                    continue

                if url_column_index >= len(row):
                    continue

                value = row[url_column_index]

                if value is None:
                    continue

                text = str(value).strip()

                if not text:
                    continue

                urls = self._extract_and_simplify_urls_from_text(text)
                result.extend(urls)

            return self._dedupe_url_list(result)

        max_scan_rows = 10000
        scanned_rows = 0

        for row in sheet.iter_rows(values_only=True):
            scanned_rows += 1

            if scanned_rows > max_scan_rows:
                break

            if not row:
                continue

            for value in row:
                if value is None:
                    continue

                text = str(value).strip()

                if not text:
                    continue

                urls = self._extract_and_simplify_urls_from_text(text)
                result.extend(urls)

        return self._dedupe_url_list(result)

    def _find_excel_url_column(self, sheet) -> tuple[int | None, int]:
        """
        尝试从 Excel 表头中识别商品链接列。
        """

        header_keywords = {
            "商品链接",
            "链接",
            "url",
            "商品url",
            "商品地址",
            "商品链接地址",
            "地址",
            "link",
            "链接地址",
            "商品页面",
            "商品网址",
        }

        max_header_scan_rows = 10

        for row_index, row in enumerate(
            sheet.iter_rows(
                min_row=1,
                max_row=max_header_scan_rows,
                values_only=True,
            ),
            start=1,
        ):
            if not row:
                continue

            for column_index, value in enumerate(row):
                if value is None:
                    continue

                header_text = self._normalize_excel_header(str(value))

                if not header_text:
                    continue

                if header_text in header_keywords:
                    return column_index, row_index

                if "url" in header_text:
                    return column_index, row_index

                if "链接" in header_text:
                    return column_index, row_index

                if "网址" in header_text:
                    return column_index, row_index

                if "地址" in header_text and "商品" in header_text:
                    return column_index, row_index

        return None, 1

    def _normalize_excel_header(self, text: str) -> str:
        """
        规范化 Excel 表头。
        """
        if not text:
            return ""

        text = str(text).strip().lower()

        text = text.replace(" ", "")
        text = text.replace("\t", "")
        text = text.replace("\n", "")
        text = text.replace("\r", "")
        text = text.replace("_", "")
        text = text.replace("-", "")
        text = text.replace("：", "")
        text = text.replace(":", "")

        return text

    def _dedupe_url_list(self, urls: list[str]) -> list[str]:
        """
        URL 去重，保持顺序。
        """
        result = []
        seen = set()

        for url in urls:
            if not url:
                continue

            if url in seen:
                continue

            seen.add(url)
            result.append(url)

        return result

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse_product(self):
        urls = self._get_urls(update_input=True)

        if not urls:
            QMessageBox.warning(self, "提示", "请先输入商品链接。")
            return

        self.save_current_config()

        self.product = None
        self.products = []
        self.failed_parse_items = []
        self.last_product_dir = None
        self.last_base_dir = None
        self.last_preview_path = None

        self.download_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.open_dir_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.clear_product_info()

        if len(urls) == 1:
            self.log("开始解析任务...")
        else:
            self.log(f"开始批量解析任务，共 {len(urls)} 个链接...")

        self.parse_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.import_links_button.setEnabled(False)

        self.parse_worker = BatchParseWorker(
            urls=urls,
            headless=self.headless_checkbox.isChecked(),
            login_wait_seconds=self.login_wait_spinbox.value(),
        )
        self.parse_worker.log_signal.connect(self.log)
        self.parse_worker.progress_signal.connect(self.progress_bar.setValue)
        self.parse_worker.success_signal.connect(self.on_parse_success)
        self.parse_worker.error_signal.connect(self.on_parse_error)
        self.parse_worker.stopped_signal.connect(self.on_parse_stopped)
        self.parse_worker.finished.connect(lambda: self.parse_button.setEnabled(True))
        self.parse_worker.start()

    def on_parse_success(self, payload):
        """
        解析成功回调。
        """

        if isinstance(payload, dict):
            self.products = payload.get("products", []) or []
            self.failed_parse_items = payload.get("failed", []) or []
        else:
            self.products = payload or []
            self.failed_parse_items = []

        self.product = self.products[0] if self.products else None

        if not self.products:
            self.download_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.import_links_button.setEnabled(True)
            return

        if len(self.products) == 1:
            product = self.products[0]

            self.platform_label.setText(f"平台：{product.platform}")
            self.product_id_label.setText(f"商品ID：{product.product_id}")
            self.title_label.setText(f"商品标题：{product.title}")

            self.main_count_label.setText(f"主图：{len(product.main_images)} 张")
            self.detail_count_label.setText(f"详情图：{len(product.detail_images)} 张")
            self.sku_count_label.setText(f"SKU图：{len(product.sku_images)} 张")

            if self.failed_parse_items:
                self.log(f"解析完成：成功 1 个，失败 {len(self.failed_parse_items)} 个。")
            else:
                self.log("商品解析完成，可以开始下载。")

        else:
            total_main = sum(len(p.main_images) for p in self.products)
            total_detail = sum(len(p.detail_images) for p in self.products)
            total_sku = sum(len(p.sku_images) for p in self.products)

            platforms = sorted(set(p.platform for p in self.products))

            self.platform_label.setText(f"平台：批量 / {', '.join(platforms)}")
            self.product_id_label.setText(f"商品ID：共 {len(self.products)} 个商品")
            self.title_label.setText("商品标题：批量任务")

            self.main_count_label.setText(f"主图：{total_main} 张")
            self.detail_count_label.setText(f"详情图：{total_detail} 张")
            self.sku_count_label.setText(f"SKU图：{total_sku} 张")

            self.log(
                f"批量解析完成：成功 {len(self.products)} 个商品，"
                f"失败 {len(self.failed_parse_items)} 个链接，"
                f"主图 {total_main} 张，详情图 {total_detail} 张，SKU图 {total_sku} 张。"
            )

        self.download_button.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.import_links_button.setEnabled(True)
        self.progress_bar.setValue(100)

    def on_parse_stopped(self, payload):
        """
        解析任务停止。
        """

        if isinstance(payload, dict):
            self.products = payload.get("products", []) or []
            self.failed_parse_items = payload.get("failed", []) or []
        else:
            self.products = []
            self.failed_parse_items = []

        self.product = self.products[0] if self.products else None

        if self.products:
            total_main = sum(len(p.main_images) for p in self.products)
            total_detail = sum(len(p.detail_images) for p in self.products)
            total_sku = sum(len(p.sku_images) for p in self.products)

            self.platform_label.setText("平台：任务已停止")
            self.product_id_label.setText(f"商品ID：已解析 {len(self.products)} 个商品")
            self.title_label.setText("商品标题：部分解析结果")

            self.main_count_label.setText(f"主图：{total_main} 张")
            self.detail_count_label.setText(f"详情图：{total_detail} 张")
            self.sku_count_label.setText(f"SKU图：{total_sku} 张")

            self.download_button.setEnabled(True)
            self.preview_button.setEnabled(True)
            self.log(f"解析任务已停止，已保留 {len(self.products)} 个已解析商品，可继续下载或预览。")
        else:
            self.download_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.log("解析任务已停止，未产生可下载商品。")

        self.stop_button.setEnabled(False)
        self.parse_button.setEnabled(True)
        self.import_links_button.setEnabled(True)

    def on_parse_error(self, message: str):
        self.log(message)

        self.stop_button.setEnabled(False)
        self.parse_button.setEnabled(True)
        self.import_links_button.setEnabled(True)
        self.preview_button.setEnabled(False)

        QMessageBox.critical(self, "解析失败", message)

    # ------------------------------------------------------------------
    # 图片预览
    # ------------------------------------------------------------------

    def preview_images(self):
        """
        生成并打开图片预览 HTML。
        """

        if not self.products:
            QMessageBox.warning(self, "提示", "请先解析商品。")
            return

        base_dir = self.path_input.text().strip()

        if not base_dir:
            QMessageBox.warning(self, "提示", "请选择保存路径。")
            return

        self.save_current_config()

        try:
            Path(base_dir).mkdir(parents=True, exist_ok=True)

            preview_path = PreviewGenerator.save_preview(
                base_dir=base_dir,
                products=self.products,
            )

            self.last_preview_path = preview_path

            self.log(f"图片预览已生成：{preview_path}")

            os.startfile(str(preview_path))

        except Exception as e:
            QMessageBox.critical(self, "预览失败", f"生成图片预览失败：{e}")

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download_images(self):
        if not self.products:
            QMessageBox.warning(self, "提示", "请先解析商品。")
            return

        base_dir = self.path_input.text().strip()
        if not base_dir:
            QMessageBox.warning(self, "提示", "请选择保存路径。")
            return

        self.save_current_config()

        selected_types = {
            "main": self.main_checkbox.isChecked(),
            "detail": self.detail_checkbox.isChecked(),
            "sku": self.sku_checkbox.isChecked(),
        }

        high_quality = self.high_quality_checkbox.isChecked()

        if not any(selected_types.values()):
            QMessageBox.warning(self, "提示", "请至少选择一种需要下载的图片类型。")
            return

        Path(base_dir).mkdir(parents=True, exist_ok=True)

        self.progress_bar.setValue(0)
        self.download_button.setEnabled(False)
        self.parse_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.import_links_button.setEnabled(False)

        if len(self.products) == 1:
            self.log("开始下载任务...")
        else:
            self.log(f"开始批量下载任务，共 {len(self.products)} 个商品...")

        self.download_worker = BatchDownloadWorker(
            products=self.products,
            base_dir=base_dir,
            selected_types=selected_types,
            failed_parse_items=self.failed_parse_items,
            high_quality=high_quality,
            download_timeout=self.timeout_spinbox.value(),
            download_retries=self.retries_spinbox.value(),
            organize_by_date=self.organize_by_date_checkbox.isChecked(),
            organize_by_platform=self.organize_by_platform_checkbox.isChecked(),
            dedupe_images=self.dedupe_images_checkbox.isChecked(),
            image_output_format=self._get_image_format_combo_value(),
        )

        self.download_worker.log_signal.connect(self.log)
        self.download_worker.progress_signal.connect(self.progress_bar.setValue)
        self.download_worker.success_signal.connect(self.on_download_success)
        self.download_worker.error_signal.connect(self.on_download_error)
        self.download_worker.stopped_signal.connect(self.on_download_stopped)
        self.download_worker.finished.connect(self.on_download_finished)
        self.download_worker.start()

    def on_download_success(self, base_dir, last_product_dir, result):
        self.last_base_dir = base_dir
        self.last_product_dir = last_product_dir
        self.open_dir_button.setEnabled(True)

        QMessageBox.information(
            self,
            "下载完成",
            f"下载完成！\n"
            f"计划下载：{result.total} 张\n"
            f"成功：{result.success} 张\n"
            f"失败：{result.failed} 张\n"
            f"成功率：{result.success_rate}%\n"
            f"MD5去重处理：{result.duplicate_removed} 张\n"
            f"格式转换成功：{result.converted_count} 张\n"
            f"格式转换失败：{result.convert_failed} 张",
        )

    def on_download_stopped(self, payload):
        """
        下载任务停止。
        """

        if isinstance(payload, dict):
            self.last_base_dir = payload.get("base_dir")
            self.last_product_dir = payload.get("last_product_dir")
            result = payload.get("result")
        else:
            result = None

        self.open_dir_button.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.parse_button.setEnabled(True)
        self.download_button.setEnabled(True)
        self.import_links_button.setEnabled(True)

        if result:
            self.log(
                f"下载任务已停止：计划 {result.total} 张，"
                f"已成功 {result.success} 张，失败 {result.failed} 张。"
            )
            QMessageBox.information(
                self,
                "任务已停止",
                f"下载任务已停止。\n"
                f"计划下载：{result.total} 张\n"
                f"已成功：{result.success} 张\n"
                f"失败：{result.failed} 张\n"
                f"已生成下载报告和失败清单。",
            )
        else:
            self.log("下载任务已停止。")

    def on_download_error(self, message: str):
        self.log(message)

        self.stop_button.setEnabled(False)
        self.parse_button.setEnabled(True)
        self.download_button.setEnabled(True)
        self.preview_button.setEnabled(True if self.products else False)
        self.import_links_button.setEnabled(True)

        QMessageBox.critical(self, "下载失败", message)

    def on_download_finished(self):
        self.parse_button.setEnabled(True)
        self.download_button.setEnabled(True)
        self.preview_button.setEnabled(True if self.products else False)
        self.stop_button.setEnabled(False)
        self.import_links_button.setEnabled(True)

    def open_last_dir(self):
        if self.last_product_dir and Path(self.last_product_dir).exists():
            os.startfile(str(self.last_product_dir))
            return

        if self.last_base_dir and Path(self.last_base_dir).exists():
            os.startfile(str(self.last_base_dir))
            return

    # ------------------------------------------------------------------
    # 停止任务
    # ------------------------------------------------------------------

    def stop_current_task(self):
        """
        停止当前解析或下载任务。
        """

        stopped = False

        if self.parse_worker and self.parse_worker.isRunning():
            if hasattr(self.parse_worker, "stop"):
                self.parse_worker.stop()
                stopped = True

        if self.download_worker and self.download_worker.isRunning():
            if hasattr(self.download_worker, "stop"):
                self.download_worker.stop()
                stopped = True

        if stopped:
            self.log("正在停止任务，请稍候...")
            self.stop_button.setEnabled(False)
        else:
            self.log("当前没有正在运行的任务。")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def clear_log(self):
        self.log_text.clear()

    def log(self, message: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{now}] {message}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def clear_product_info(self):
        self.platform_label.setText("平台：-")
        self.product_id_label.setText("商品ID：-")
        self.title_label.setText("商品标题：-")
        self.main_count_label.setText("主图：- 张")
        self.detail_count_label.setText("详情图：- 张")
        self.sku_count_label.setText("SKU图：- 张")

    def _get_image_format_combo_value(self) -> str:
        """
        获取当前选择的图片输出格式。
        """
        if not hasattr(self, "image_format_combo"):
            return "original"

        value = self.image_format_combo.currentData()

        if value in ["original", "jpg", "png", "webp"]:
            return value

        return "original"

    def _set_image_format_combo_value(self, value: str) -> None:
        """
        设置图片输出格式下拉框。
        """
        if not hasattr(self, "image_format_combo"):
            return

        value = value or "original"

        for index in range(self.image_format_combo.count()):
            if self.image_format_combo.itemData(index) == value:
                self.image_format_combo.setCurrentIndex(index)
                return

        self.image_format_combo.setCurrentIndex(0)

    def _get_urls(self, update_input: bool = True) -> list[str]:
        """
        获取输入框中的所有商品链接，并自动简化长链接。
        """

        text = self.url_input.toPlainText().strip()

        if not text:
            return []

        result = self._extract_and_simplify_urls_from_text(text)

        if update_input and result:
            new_text = "\n".join(result)

            if new_text != text:
                self.url_input.setPlainText(new_text)

        return result

    def _extract_and_simplify_urls_from_text(self, text: str) -> list[str]:
        """
        从任意文本中提取商品链接并简化。
        """

        if not text:
            return []

        text = text.replace("\r\n", "\n").replace("\r", "\n")

        raw_urls = re.findall(
            r"https?://[^\s<>'\"，,；;。]+",
            text,
            flags=re.I,
        )

        if not raw_urls:
            raw_urls = [line.strip() for line in text.splitlines() if line.strip()]

        result = []
        seen = set()

        for raw_url in raw_urls:
            cleaned = self._clean_input_url(raw_url)
            simplified = self._simplify_product_url(cleaned)

            if not simplified:
                continue

            if simplified in seen:
                continue

            seen.add(simplified)
            result.append(simplified)

        return result

    def _clean_input_url(self, url: str) -> str:
        """
        清理用户输入的商品链接。
        """

        if not url:
            return ""

        url = str(url).strip()

        url = url.strip(" \t\r\n'\"<>")
        url = url.rstrip("，,；;。.)）]】")

        if not re.match(r"^https?://", url, flags=re.I):
            return ""

        return url

    def _simplify_product_url(self, url: str) -> str:
        """
        简化商品链接，只保留解析商品需要的核心参数。
        """

        if not url:
            return ""

        try:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            query = parse_qs(parsed.query)

            # 京东
            if "jd.com" in host:
                match = re.search(r"/(\d+)\.html", url)

                if match:
                    sku_id = match.group(1)
                    return f"https://item.jd.com/{sku_id}.html"

                return url

            # 淘宝
            if "taobao.com" in host:
                item_id = query.get("id", [""])[0]

                if item_id:
                    return f"https://item.taobao.com/item.htm?id={item_id}"

                return url

            # 天猫
            if "tmall.com" in host:
                item_id = query.get("id", [""])[0]

                if item_id:
                    return f"https://detail.tmall.com/item.htm?id={item_id}"

                return url

            # 拼多多
            if "pinduoduo.com" in host or "yangkeduo.com" in host:
                goods_id = query.get("goods_id", [""])[0]

                if goods_id:
                    return f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"

                return url

            return url

        except Exception:
            return url

    def closeEvent(self, event):
        """
        关闭窗口前保存配置。
        """
        self.save_current_config()
        super().closeEvent(event)
