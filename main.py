import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def load_qss(app: QApplication):
    """
    加载 QSS 样式文件。
    """
    qss_path = Path("styles/dark.qss")
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))


def main():
    Path("output").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)
    Path("debug").mkdir(parents=True, exist_ok=True)
    Path("browser_data").mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("商品图片采集工具")

    load_qss(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
