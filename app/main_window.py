from datetime import datetime
from pathlib import Path
import os

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
)

from app.workers import ParseWorker, DownloadWorker


class MainWindow(QMainWindow):
    """
    主窗口。

    当前版本使用 Python 代码构建 UI。
    后续你可以替换成 Qt Designer 的 .ui 文件。
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("商品图片采集工具")
        self.resize(900, 720)

        self.product = None
        self.last_product_dir = None

        self.parse_worker = None
        self.download_worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)

        # 链接输入区域
        input_group = QGroupBox("商品链接")
        input_layout = QVBoxLayout(input_group)

        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("请粘贴商品链接，目前建议一次输入一个链接。")
        self.url_input.setFixedHeight(90)

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

        # 识别结果区域
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

        self.main_checkbox.setChecked(True)
        self.detail_checkbox.setChecked(True)
        self.sku_checkbox.setChecked(True)

        row2.addWidget(self.main_checkbox)
        row2.addWidget(self.detail_checkbox)
        row2.addWidget(self.sku_checkbox)
        row2.addStretch()

        result_layout.addLayout(row1)
        result_layout.addLayout(row2)

        main_layout.addWidget(result_group)

        # 保存路径区域
        path_group = QGroupBox("保存路径")
        path_layout = QHBoxLayout(path_group)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("请选择图片保存路径")
        self.path_input.setText(str(Path("output").absolute()))

        self.choose_path_button = QPushButton("选择目录")

        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.choose_path_button)

        main_layout.addWidget(path_group)

        # 操作按钮区域
        button_layout = QHBoxLayout()

        self.parse_button = QPushButton("解析商品")
        self.download_button = QPushButton("开始下载")
        self.open_dir_button = QPushButton("打开保存目录")

        self.download_button.setEnabled(False)
        self.open_dir_button.setEnabled(False)

        button_layout.addWidget(self.parse_button)
        button_layout.addWidget(self.download_button)
        button_layout.addWidget(self.open_dir_button)
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
        self.open_dir_button.clicked.connect(self.open_last_dir)

    def choose_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if path:
            self.path_input.setText(path)

    def parse_product(self):
        url = self._get_first_url()

        if not url:
            QMessageBox.warning(self, "提示", "请先输入商品链接。")
            return

        self.product = None
        self.download_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.clear_product_info()

        self.log("开始解析任务...")
        self.parse_button.setEnabled(False)

        self.parse_worker = ParseWorker(url)
        self.parse_worker.log_signal.connect(self.log)
        self.parse_worker.success_signal.connect(self.on_parse_success)
        self.parse_worker.error_signal.connect(self.on_parse_error)
        self.parse_worker.finished.connect(lambda: self.parse_button.setEnabled(True))
        self.parse_worker.start()

    def download_images(self):
        if not self.product:
            QMessageBox.warning(self, "提示", "请先解析商品。")
            return

        base_dir = self.path_input.text().strip()
        if not base_dir:
            QMessageBox.warning(self, "提示", "请选择保存路径。")
            return

        selected_types = {
            "main": self.main_checkbox.isChecked(),
            "detail": self.detail_checkbox.isChecked(),
            "sku": self.sku_checkbox.isChecked(),
        }

        if not any(selected_types.values()):
            QMessageBox.warning(self, "提示", "请至少选择一种需要下载的图片类型。")
            return

        Path(base_dir).mkdir(parents=True, exist_ok=True)

        self.progress_bar.setValue(0)
        self.download_button.setEnabled(False)
        self.parse_button.setEnabled(False)

        self.log("开始下载任务...")

        self.download_worker = DownloadWorker(
            product=self.product,
            base_dir=base_dir,
            selected_types=selected_types,
        )

        self.download_worker.log_signal.connect(self.log)
        self.download_worker.progress_signal.connect(self.progress_bar.setValue)
        self.download_worker.success_signal.connect(self.on_download_success)
        self.download_worker.error_signal.connect(self.on_download_error)
        self.download_worker.finished.connect(self.on_download_finished)
        self.download_worker.start()

    def on_parse_success(self, product):
        self.product = product

        self.platform_label.setText(f"平台：{product.platform}")
        self.product_id_label.setText(f"商品ID：{product.product_id}")
        self.title_label.setText(f"商品标题：{product.title}")

        self.main_count_label.setText(f"主图：{len(product.main_images)} 张")
        self.detail_count_label.setText(f"详情图：{len(product.detail_images)} 张")
        self.sku_count_label.setText(f"SKU图：{len(product.sku_images)} 张")

        self.download_button.setEnabled(True)

        self.log("商品解析完成，可以开始下载。")

    def on_parse_error(self, message: str):
        self.log(message)
        QMessageBox.critical(self, "解析失败", message)

    def on_download_success(self, product_dir, result):
        self.last_product_dir = product_dir
        self.open_dir_button.setEnabled(True)

        QMessageBox.information(
            self,
            "下载完成",
            f"下载完成！\n"
            f"计划下载：{result.total} 张\n"
            f"成功：{result.success} 张\n"
            f"失败：{result.failed} 张\n"
            f"成功率：{result.success_rate}%",
        )

    def on_download_error(self, message: str):
        self.log(message)
        QMessageBox.critical(self, "下载失败", message)

    def on_download_finished(self):
        self.parse_button.setEnabled(True)
        self.download_button.setEnabled(True)

    def open_last_dir(self):
        if self.last_product_dir and Path(self.last_product_dir).exists():
            os.startfile(str(self.last_product_dir))

    def clear_product_info(self):
        self.platform_label.setText("平台：-")
        self.product_id_label.setText("商品ID：-")
        self.title_label.setText("商品标题：-")
        self.main_count_label.setText("主图：- 张")
        self.detail_count_label.setText("详情图：- 张")
        self.sku_count_label.setText("SKU图：- 张")

    def _get_first_url(self) -> str:
        """
        当前版本先取第一行非空链接。
        后续批量采集时这里可以改为返回 URL 列表。
        """
        text = self.url_input.toPlainText().strip()
        if not text:
            return ""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[0] if lines else ""

    def log(self, message: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{now}] {message}")
