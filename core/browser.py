from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


class BrowserClient:
    """
    Playwright 浏览器封装。

    功能：
    1. 打开商品页面；
    2. 检测是否跳转到登录页；
    3. 如需登录，等待用户手动登录；
    4. 登录完成后继续采集；
    5. 尝试激活商品详情区域；
    6. 自动滚动触发懒加载；
    7. 支持执行 JS 获取渲染后的 DOM 数据；
    8. 可选捕获网络响应文本，用于京东京东详情图接口定位。
    """

    def __init__(
        self,
        user_data_dir: str = "browser_data",
        headless: bool = False,
        timeout: int = 30000,
        login_wait_seconds: int = 180,
        log_callback=None,
    ):
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self.timeout = timeout
        self.login_wait_seconds = login_wait_seconds
        self.log_callback = log_callback

    def log(self, message: str):
        """
        输出日志到 UI。
        """
        if self.log_callback:
            self.log_callback(message)

    def open_page(self, url: str, wait_until: str = "domcontentloaded") -> str:
        """
        打开页面并返回最终商品页面 HTML。
        """

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                viewport={"width": 1366, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )

            page = context.new_page()
            page.set_default_timeout(self.timeout)

            self.log("正在打开商品页面...")

            try:
                page.goto(url, wait_until=wait_until, timeout=self.timeout)
            except PlaywrightTimeoutError:
                self.log("页面加载超时，继续尝试读取当前页面...")
            except Exception as e:
                context.close()
                raise RuntimeError(f"页面打开失败：{e}")

            page.wait_for_timeout(1500)

            if self._is_login_page(page):
                self.log("检测到当前页面为登录页。")
                self.log("请在弹出的浏览器中手动完成登录。")
                self.log(f"程序最多等待 {self.login_wait_seconds} 秒，登录成功后会自动继续。")

                login_ok = self._wait_for_login_finished(page, original_url=url)

                if not login_ok:
                    context.close()
                    raise RuntimeError("登录等待超时，请确认已完成登录。")

                self.log("检测到登录完成，继续采集商品页面。")

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
                except Exception:
                    pass

                page.wait_for_timeout(2000)

            self._try_click_detail_tab(page)
            self._auto_scroll(page)

            html = page.content()

            if self._looks_like_login_html(html, page.url):
                context.close()
                raise RuntimeError("当前仍然是登录页，未获取到商品详情页数据。")

            context.close()

            return html
            
    def open_page_with_extracted_data(
        self,
        url: str,
        extract_script: str,
        wait_until: str = "domcontentloaded",
    ) -> dict:
        """
        打开页面，等待渲染后执行 JS，返回：
            {
                "html": 页面 HTML,
                "data": JS 提取结果,
                "final_url": 最终页面 URL,
            }

        主要用于 1688：
            主图 / SKU 图很多是前端渲染后的 DOM，
            不能只靠 page.content() 静态 HTML 解析。
        """

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                viewport={"width": 1366, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )

            page = context.new_page()
            page.set_default_timeout(self.timeout)

            self.log("正在打开商品页面...")

            try:
                page.goto(url, wait_until=wait_until, timeout=self.timeout)
            except PlaywrightTimeoutError:
                self.log("页面加载超时，继续尝试读取当前页面...")
            except Exception as e:
                context.close()
                raise RuntimeError(f"页面打开失败：{e}")

            page.wait_for_timeout(2000)

            if self._is_login_page(page):
                self.log("检测到当前页面为登录页。")
                self.log("请在弹出的浏览器中手动完成登录。")
                self.log(f"程序最多等待 {self.login_wait_seconds} 秒，登录成功后会自动继续。")

                login_ok = self._wait_for_login_finished(page, original_url=url)

                if not login_ok:
                    context.close()
                    raise RuntimeError("登录等待超时，请确认已完成登录。")

                self.log("检测到登录完成，继续采集商品页面。")

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
                except Exception:
                    pass

                page.wait_for_timeout(2500)

            try:
                self._try_click_detail_tab(page)
            except Exception:
                pass

            try:
                self._auto_scroll(page)
            except Exception:
                pass

            page.wait_for_timeout(1500)

            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(800)
            except Exception:
                pass

            data = {}

            try:
                data = page.evaluate(extract_script)
            except Exception as e:
                self.log(f"页面 JS 数据提取失败：{e}")
                data = {}

            html = page.content()

            if self._looks_like_login_html(html, page.url):
                context.close()
                raise RuntimeError("当前仍是登录页，请先完成登录后重试。")

            final_url = page.url

            context.close()

            return {
                "html": html,
                "data": data or {},
                "final_url": final_url,
            }


    def open_page_and_eval(
        self,
        url: str,
        js_script: str,
        wait_until: str = "domcontentloaded",
        collect_network: bool = False,
    ):
        """
        打开页面，处理登录，尝试激活详情区域，滚动懒加载，
        然后执行 JS 获取渲染后的页面数据。

        参数：
            collect_network:
                False：返回 html, data
                True ：返回 html, data, network_texts

        network_texts 格式：
            [
                {
                    "url": "接口地址",
                    "content_type": "响应类型",
                    "text": "响应文本"
                }
            ]
        """

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        network_texts = []
        seen_response_urls = set()

        def should_capture_response(response_url: str, content_type: str) -> bool:
            """
            判断是否捕获该网络响应。

            主要面向京东详情图：
            - description
            - desc
            - detail
            - ware
            - business
            - ssd
            - module
            - item
            - sku
            """

            if not response_url:
                return False

            lower_url = response_url.lower()
            lower_type = content_type.lower() if content_type else ""

            url_keywords = [
                "description",
                "desc",
                "detail",
                "ware",
                "business",
                "ssd",
                "module",
                "item",
                "sku",
                "pcpubliccms",
                "cd.jd.com",
                "dx.3.cn",
                "api.m.jd.com",
                "item-soa.jd.com",
            ]

            if not any(keyword in lower_url for keyword in url_keywords):
                return False

            allowed_types = [
                "text",
                "json",
                "javascript",
                "html",
                "plain",
            ]

            if lower_type and not any(t in lower_type for t in allowed_types):
                return False

            return True

        def handle_response(response):
            """
            捕获页面真实网络响应。

            注意：
            response.text() 在 Playwright sync API 中可以直接调用。
            个别响应可能无法读取，会被 try 忽略。
            """

            if not collect_network:
                return

            try:
                response_url = response.url

                if response_url in seen_response_urls:
                    return

                headers = response.headers or {}
                content_type = headers.get("content-type", "")

                if not should_capture_response(response_url, content_type):
                    return

                text = response.text()

                if not text or len(text) < 50:
                    return

                # 避免超大响应撑爆内存
                max_len = 500000
                saved_text = text[:max_len]

                network_texts.append(
                    {
                        "url": response_url,
                        "content_type": content_type,
                        "text": saved_text,
                    }
                )

                seen_response_urls.add(response_url)

                # self.log(f"捕获网络响应：{response_url}，长度：{len(text)}")

            except Exception:
                pass

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                viewport={"width": 1366, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )

            page = context.new_page()
            page.set_default_timeout(self.timeout)

            if collect_network:
                page.on("response", handle_response)

            self.log("正在打开商品页面...")

            try:
                page.goto(url, wait_until=wait_until, timeout=self.timeout)
            except PlaywrightTimeoutError:
                self.log("页面加载超时，继续尝试读取当前页面...")
            except Exception as e:
                context.close()
                raise RuntimeError(f"页面打开失败：{e}")

            page.wait_for_timeout(2500)

            if self._is_login_page(page):
                self.log("检测到当前页面为登录页。")
                self.log("请在弹出的浏览器中手动完成登录。")
                self.log(f"程序最多等待 {self.login_wait_seconds} 秒，登录成功后会自动继续。")

                login_ok = self._wait_for_login_finished(page, original_url=url)

                if not login_ok:
                    context.close()
                    raise RuntimeError("登录等待超时，请确认已完成登录。")

                self.log("检测到登录完成，继续采集商品页面。")

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
                except Exception:
                    pass

                page.wait_for_timeout(2500)

            # 先尝试点击/激活商品详情区域
            self._try_click_detail_tab(page)

            # 再滚动页面，触发懒加载
            self._auto_scroll(page)

            # 网络响应可能在滚动后继续出现，这里稍微等一下
            if collect_network:
                try:
                    page.wait_for_timeout(2500)
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

            html = page.content()

            if self._looks_like_login_html(html, page.url):
                context.close()
                raise RuntimeError("当前仍然是登录页，未获取到商品详情页数据。")

            data = {}

            try:
                self.log("开始从页面 DOM 提取渲染后数据...")
                data = page.evaluate(js_script)
            except Exception as e:
                self.log(f"页面 JS 数据提取失败：{e}")
                data = {}

            context.close()

            if collect_network:
                # self.log(f"网络响应采集完成，共捕获：{len(network_texts)} 条")
                return html, data, network_texts

            return html, data

    def _try_click_detail_tab(self, page):
        """
        尝试点击商品详情/商品介绍标签。

        主要用于京东：
        有些商品详情图不会一开始加载，
        需要点击“商品详情 / 商品介绍”后才出现。
        """

        self.log("尝试激活商品详情区域...")

        keywords = [
            "商品介绍",
            "商品详情",
            "图文详情",
        ]

        for keyword in keywords:
            try:
                locator = page.locator(f"text={keyword}").first

                if locator.count() > 0:
                    locator.click(timeout=1500)
                    page.wait_for_timeout(1500)
                    self.log(f"已尝试点击详情标签：{keyword}")
                    return

            except Exception:
                pass

        # 兜底：尝试滚动到详情区域
        try:
            page.evaluate(
                """
                () => {
                    const selectors = [
                        '#detail',
                        '#J-detail',
                        '#J-detail-content',
                        '.detail-content',
                        '.product-detail',
                        '.ssd-module-wrap',
                        '.ssd-module'
                    ];

                    for (const selector of selectors) {
                        const el = document.querySelector(selector);
                        if (el) {
                            el.scrollIntoView({
                                behavior: 'instant',
                                block: 'start'
                            });
                            return true;
                        }
                    }

                    return false;
                }
                """
            )

            page.wait_for_timeout(1500)

        except Exception:
            pass

    def _wait_for_login_finished(self, page, original_url: str) -> bool:
        """
        等待用户手动登录完成。
        """

        check_interval_ms = 1000
        max_count = self.login_wait_seconds

        for second in range(max_count):
            try:
                current_url = page.url
                title = page.title()

                if second > 0 and second % 5 == 0:
                    self.log(f"等待登录中... 已等待 {second} 秒")

                if not self._is_login_url(current_url) and "登录" not in title:
                    content = page.content()

                    if not self._looks_like_login_html(content, current_url):
                        return True

                page.wait_for_timeout(check_interval_ms)

            except Exception:
                page.wait_for_timeout(check_interval_ms)

        self.log("尝试重新打开原始商品链接以确认登录状态...")

        try:
            page.goto(original_url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(2000)

            if not self._is_login_page(page):
                return True

        except Exception:
            pass

        return False

    def _is_login_page(self, page) -> bool:
        """
        判断当前页面是否是登录页。
        """

        try:
            url = page.url
            title = page.title()
            html = page.content()

            if self._is_login_url(url):
                return True

            if title and "登录" in title:
                return True

            if self._looks_like_login_html(html, url):
                return True

            return False

        except Exception:
            return False

    def _is_login_url(self, url: str) -> bool:
        """
        判断 URL 是否是常见登录页。
        """

        if not url:
            return False

        lower = url.lower()

        login_keywords = [
            "login.taobao.com",
            "login.tmall.com",
            "login.m.taobao.com",
            "login.m.tmall.com",
            "passport.jd.com",
            "plogin.m.jd.com",
            "login.m.jd.com",
            "yangkeduo.com/login",
            "mobile.yangkeduo.com/login",
            "pinduoduo.com/login",
            "passport",
            "login",
        ]

        return any(keyword in lower for keyword in login_keywords)

    def _looks_like_login_html(self, html: str, url: str = "") -> bool:
        """
        根据 HTML 内容判断是否是登录页。
        """

        if not html:
            return False

        lower_html = html.lower()
        lower_url = url.lower() if url else ""

        if self._is_login_url(lower_url):
            return True

        login_signals = [
            "账号登录",
            "密码登录",
            "扫码登录",
            "手机淘宝扫码",
            "请输入账号",
            "请输入密码",
            "login-form",
            "login-box",
            "fm-login",
            "忘记密码",
        ]

        hit_count = 0

        for signal in login_signals:
            if signal.lower() in lower_html:
                hit_count += 1

        return hit_count >= 2

    def _auto_scroll(self, page):
        """
        自动滚动页面，触发懒加载。

        对京东尤其重要：
        商品详情图经常需要滚到“商品详情”区域后才加载。
        """

        self.log("开始滚动页面以触发懒加载...")

        try:
            # 先回到顶部
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            # 慢速滚动到底部，触发懒加载
            for _ in range(24):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(450)

            # 再滚回商品详情附近，方便 JS 根据详情边界读取真实 DOM
            page.evaluate(
                """
                () => {
                    const keywords = ['商品详情', '商品介绍', '图文详情'];

                    const all = Array.from(document.querySelectorAll('*'));

                    for (const el of all) {
                        const text = el.innerText ? el.innerText.trim() : '';

                        if (keywords.some(k => text.includes(k))) {
                            el.scrollIntoView({
                                behavior: 'instant',
                                block: 'start'
                            });
                            return true;
                        }
                    }

                    return false;
                }
                """
            )

            page.wait_for_timeout(1500)

            # 详情区域附近再滚几次，触发详情图懒加载
            for _ in range(12):
                page.mouse.wheel(0, 800)
                page.wait_for_timeout(550)

        except Exception:
            pass

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
