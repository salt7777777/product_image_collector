from pathlib import Path
import time

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


class LoginBrowserWorker(QThread):
    """
    登录浏览器线程。

    用途：
    1. 单独打开浏览器登录页；
    2. 使用 browser_data/{platform} 保存各平台独立登录状态；
    3. 用户登录完成后关闭浏览器，或在主程序中点击“结束登录浏览器”；
    4. 不阻塞主界面。
    """

    log_signal = Signal(str)
    error_signal = Signal(str)
    finished_signal = Signal()

    PLATFORM_URLS = {
        "taobao": "https://login.taobao.com/member/login.jhtml?redirectURL=https%3A%2F%2Fwww.taobao.com%2F",
        "tmall": "https://login.taobao.com/member/login.jhtml?redirectURL=https%3A%2F%2Fwww.tmall.com%2F",
        "jd": "https://passport.jd.com/new/login.aspx",
        "pdd": "https://mobile.yangkeduo.com/",
        "1688": "https://login.1688.com/member/signin.htm",
    }

    PLATFORM_NAMES = {
        "taobao": "淘宝",
        "tmall": "天猫",
        "jd": "京东",
        "pdd": "拼多多",
        "1688": "1688",
    }

    def __init__(
        self,
        platform: str,
        user_data_dir: str = "browser_data",
    ):
        super().__init__()

        self.platform = platform

        base_dir = Path(user_data_dir)

        # 兼容两种传法：
        # 1. user_data_dir="browser_data"
        #    => browser_data/{platform}
        #
        # 2. user_data_dir="browser_data/1688"
        #    => browser_data/1688
        if base_dir.name == platform:
            self.user_data_dir = base_dir
        else:
            self.user_data_dir = base_dir / platform

        self._stop_requested = False

    def stop(self):
        """
        请求停止登录浏览器线程。

        注意：
        不在这里直接 context.close()，
        因为 Playwright 对象是在 worker 线程中创建的，
        应尽量由 worker 线程自己关闭。
        """
        self._stop_requested = True

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def run(self):
        platform_name = self.PLATFORM_NAMES.get(self.platform, self.platform)
        url = self.PLATFORM_URLS.get(self.platform)

        if not url:
            self.error_signal.emit(f"不支持的平台：{self.platform}")
            self.finished_signal.emit()
            return

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        context = None
        main_page = None
        context_closed = {"value": False}
        page_closed = {"value": False}

        try:
            self.log_signal.emit(f"正在打开{platform_name}登录浏览器...")
            self.log_signal.emit(f"登录状态将保存到：{self.user_data_dir}")

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    headless=False,
                    viewport={"width": 1366, "height": 900},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                    ],
                )

                def mark_context_closed(*args):
                    context_closed["value"] = True

                try:
                    context.on("close", mark_context_closed)
                except Exception:
                    pass

                # 尽量复用已有页面，没有则新建
                try:
                    if context.pages:
                        main_page = context.pages[0]
                    else:
                        main_page = context.new_page()
                except Exception:
                    main_page = context.new_page()

                def mark_page_closed(*args):
                    page_closed["value"] = True

                try:
                    main_page.on("close", mark_page_closed)
                except Exception:
                    pass

                main_page.set_default_timeout(30000)

                try:
                    main_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightTimeoutError:
                    self.log_signal.emit("登录页加载超时，但浏览器已打开，可继续手动操作。")
                except Exception as e:
                    self.log_signal.emit(f"登录页打开异常：{e}")

                self.log_signal.emit(
                    f"{platform_name}登录浏览器已打开，请在浏览器中完成登录。"
                )
                self.log_signal.emit(
                    "登录完成后，请关闭浏览器窗口；如果按钮未恢复，可点击“结束登录浏览器”。"
                )

                # ------------------------------------------------------------
                # 等待用户关闭浏览器或主程序请求停止
                # ------------------------------------------------------------
                while not self.is_stop_requested():
                    try:
                        if context_closed["value"] or page_closed["value"]:
                            break

                        if main_page is not None:
                            try:
                                if main_page.is_closed():
                                    break
                            except Exception:
                                break

                        # 主动触发一次轻量状态检查。
                        # 如果浏览器已经关闭，这里通常会抛 TargetClosed 类异常。
                        try:
                            if main_page is not None and not main_page.is_closed():
                                _ = main_page.url
                        except Exception:
                            break

                        # 检查是否还有打开页面
                        open_pages = []

                        try:
                            for page in context.pages:
                                try:
                                    if not page.is_closed():
                                        open_pages.append(page)
                                except Exception:
                                    pass
                        except Exception:
                            break

                        if not open_pages:
                            break

                        time.sleep(0.5)

                    except Exception:
                        break

        except Exception as e:
            self.error_signal.emit(f"打开登录浏览器失败：{e}")

        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass

            self.log_signal.emit(f"{platform_name}登录浏览器已关闭。")
            self.finished_signal.emit()
