import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from core.models import ReviewItem, ReviewMedia
from utils.url_utils import normalize_image_url, get_url_ext, dedupe_urls


class TaobaoReviewParser:
    """
    淘宝/天猫评价图/视频采集器。

    第一版策略：
        1. 打开商品页；
        2. 尝试点击评价入口；
        3. 尝试点击“图/视频”筛选；
        4. 滚动评价弹窗/页面；
        5. 从 DOM 提取有图/视频的评价；
        6. 返回 ReviewItem 列表。
    """

    def __init__(
        self,
        platform: str = "taobao",
        headless: bool = False,
        login_wait_seconds: int = 180,
        timeout: int = 30000,
        log_callback=None,
    ):
        self.platform = platform if platform in ["taobao", "tmall"] else "taobao"
        self.user_data_dir = Path("browser_data") / self.platform
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds
        self.timeout = timeout
        self.log_callback = log_callback

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def parse_reviews(
        self,
        url: str,
        limit: int = 50,
        include_video: bool = True,
        cancel_callback=None,
    ) -> list[ReviewItem]:
        if not url:
            return []

        limit = max(1, int(limit or 50))

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        reviews: list[ReviewItem] = []

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

            try:
                self.log("正在打开淘宝/天猫商品页，准备采集评价...")
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            except PlaywrightTimeoutError:
                self.log("商品页加载超时，继续尝试采集评价...")
            except Exception as e:
                context.close()
                raise RuntimeError(f"打开商品页失败：{e}")

            page.wait_for_timeout(2500)

            if self._is_login_page(page):
                self.log("检测到淘宝/天猫登录页，请在浏览器中完成登录。")
                ok = self._wait_for_login_finished(page, original_url=url)

                if not ok:
                    context.close()
                    raise RuntimeError("登录等待超时，无法采集评价。")

                self.log("登录完成，继续采集评价。")
                page.wait_for_timeout(2500)

            if cancel_callback and cancel_callback():
                context.close()
                return reviews

            self._try_click_review_entry(page)

            if cancel_callback and cancel_callback():
                context.close()
                return reviews

            self._try_click_media_filter(page)

            seen_keys = set()

            max_scroll_rounds = 30
            stable_rounds = 0
            last_count = 0

            for round_index in range(max_scroll_rounds):
                if cancel_callback and cancel_callback():
                    break

                extracted = self._extract_reviews_from_page(
                    page=page,
                    include_video=include_video,
                )

                added = 0

                for item in extracted:
                    if not item.images and not item.videos:
                        continue

                    key = self._build_review_key(item)

                    if key in seen_keys:
                        continue

                    seen_keys.add(key)
                    item.index = len(reviews) + 1
                    reviews.append(item)
                    added += 1

                    if len(reviews) >= limit:
                        break

                self.log(
                    f"评价采集中：第 {round_index + 1}/{max_scroll_rounds} 轮，"
                    f"本轮新增 {added} 条，累计 {len(reviews)} 条。"
                )

                if len(reviews) >= limit:
                    break

                if len(reviews) == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                last_count = len(reviews)

                if stable_rounds >= 5:
                    self.log("连续多轮未发现新评价，停止继续滚动。")
                    break

                self._scroll_review_area(page)
                page.wait_for_timeout(1200)

            context.close()

        self.log(f"淘宝/天猫评价采集完成：{len(reviews)} 条有图/视频评价。")

        return reviews

    def _try_click_review_entry(self, page):
        self.log("尝试打开评价区域...")

        keywords = [
            "用户评价",
            "累计评价",
            "宝贝评价",
            "评价",
        ]

        for keyword in keywords:
            try:
                locator = page.locator(f"text={keyword}").first

                if locator.count() > 0:
                    locator.click(timeout=2500)
                    page.wait_for_timeout(2500)
                    self.log(f"已尝试点击评价入口：{keyword}")
                    return
            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                () => {
                    const keywords = ['用户评价', '累计评价', '宝贝评价', '评价'];
                    const nodes = Array.from(document.querySelectorAll('button, a, div, span'));

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '').trim();

                        if (!text) continue;

                        if (keywords.some(k => text.includes(k))) {
                            node.scrollIntoView({block: 'center'});
                            node.click();
                            return text;
                        }
                    }

                    return '';
                }
                """
            )

            if clicked:
                page.wait_for_timeout(2500)
                self.log(f"已通过 JS 点击评价入口：{clicked}")

        except Exception:
            pass

    def _try_click_media_filter(self, page):
        self.log("尝试点击“图/视频”评价筛选...")

        keywords = [
            "图/视频",
            "图片",
            "有图",
            "视频",
        ]

        for keyword in keywords:
            try:
                locator = page.locator(f"text={keyword}").first

                if locator.count() > 0:
                    locator.click(timeout=2500)
                    page.wait_for_timeout(2000)
                    self.log(f"已尝试点击评价筛选：{keyword}")
                    return
            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                () => {
                    const keywords = ['图/视频', '图片', '有图', '视频'];
                    const nodes = Array.from(document.querySelectorAll('button, a, div, span'));

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '').trim();

                        if (!text) continue;

                        if (keywords.some(k => text.includes(k))) {
                            node.scrollIntoView({block: 'center'});
                            node.click();
                            return text;
                        }
                    }

                    return '';
                }
                """
            )

            if clicked:
                page.wait_for_timeout(2000)
                self.log(f"已通过 JS 点击评价筛选：{clicked}")

        except Exception:
            pass

    def _scroll_review_area(self, page):
        try:
            page.evaluate(
                """
                () => {
                    const candidates = Array.from(document.querySelectorAll('*'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            const overflowY = style.overflowY;
                            return (
                                el.scrollHeight > el.clientHeight + 200 &&
                                ['auto', 'scroll'].includes(overflowY)
                            );
                        })
                        .sort((a, b) => b.scrollHeight - a.scrollHeight);

                    if (candidates.length > 0) {
                        candidates[0].scrollTop += 900;
                        return true;
                    }

                    window.scrollBy(0, 900);
                    return false;
                }
                """
            )
        except Exception:
            try:
                page.mouse.wheel(0, 900)
            except Exception:
                pass

    def _extract_reviews_from_page(
        self,
        page,
        include_video: bool = True,
    ) -> list[ReviewItem]:
        try:
            raw_items = page.evaluate(
                """
                (includeVideo) => {
                    function cleanText(text) {
                        return (text || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                    }

                    function collectImageUrls(root) {
                        const urls = [];

                        const imgs = Array.from(root.querySelectorAll('img'));
                        for (const img of imgs) {
                            const candidates = [
                                img.currentSrc,
                                img.src,
                                img.getAttribute('data-src'),
                                img.getAttribute('data-ks-lazyload'),
                                img.getAttribute('data-lazyload'),
                                img.getAttribute('data-original')
                            ];

                            for (const u of candidates) {
                                if (u) urls.push(u);
                            }
                        }

                        const bgNodes = Array.from(root.querySelectorAll('*'));
                        for (const node of bgNodes) {
                            const style = window.getComputedStyle(node);
                            const bg = style.backgroundImage || '';

                            const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
                            if (match && match[1]) {
                                urls.push(match[1]);
                            }
                        }

                        return urls;
                    }

                    function collectVideoUrls(root) {
                        if (!includeVideo) return [];

                        const urls = [];

                        const videos = Array.from(root.querySelectorAll('video'));
                        for (const video of videos) {
                            if (video.currentSrc) urls.push(video.currentSrc);
                            if (video.src) urls.push(video.src);
                            if (video.poster) urls.push(video.poster);

                            const sources = Array.from(video.querySelectorAll('source'));
                            for (const source of sources) {
                                if (source.src) urls.push(source.src);
                            }
                        }

                        const nodes = Array.from(root.querySelectorAll('*'));
                        for (const node of nodes) {
                            for (const attr of ['src', 'data-src', 'data-video', 'data-url']) {
                                const v = node.getAttribute(attr);
                                if (v && (
                                    v.includes('.mp4') ||
                                    v.includes('.m3u8') ||
                                    v.startsWith('blob:')
                                )) {
                                    urls.push(v);
                                }
                            }
                        }

                        return urls;
                    }

                    function looksLikeReviewBlock(el) {
                        const text = cleanText(el.innerText || el.textContent || '');

                        if (!text || text.length < 2) return false;

                        const imgCount = el.querySelectorAll('img').length;
                        const videoCount = el.querySelectorAll('video').length;

                        if (imgCount <= 0 && videoCount <= 0) return false;

                        const badWords = ['店铺', '客服', '推荐', '猜你喜欢', '广告', '直播'];

                        if (badWords.some(w => text.includes(w)) && text.length < 20) {
                            return false;
                        }

                        return true;
                    }

                    const nodes = Array.from(document.querySelectorAll('div, li, section, article'));
                    const blocks = [];

                    for (const node of nodes) {
                        if (!looksLikeReviewBlock(node)) continue;

                        const rect = node.getBoundingClientRect();

                        if (rect.width < 120 || rect.height < 80) continue;

                        blocks.push(node);
                    }

                    const result = [];

                    for (const block of blocks) {
                        const text = cleanText(block.innerText || block.textContent || '');

                        const imageUrls = collectImageUrls(block);
                        const videoUrls = collectVideoUrls(block);

                        result.push({
                            text,
                            imageUrls,
                            videoUrls
                        });
                    }

                    return result;
                }
                """,
                include_video,
            )
        except Exception as e:
            self.log(f"评价 DOM 提取失败：{e}")
            raw_items = []

        reviews = []

        for raw in raw_items or []:
            text = str(raw.get("text", "") or "").strip()

            image_urls = self._filter_image_urls(raw.get("imageUrls", []) or [])
            video_urls = self._filter_video_urls(raw.get("videoUrls", []) or [])

            # video poster 可能是图片，将图片链接归到 images
            fixed_video_urls = []
            for u in video_urls:
                lower = u.lower()
                if any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                    image_urls.append(u)
                else:
                    fixed_video_urls.append(u)

            image_urls = dedupe_urls(image_urls)
            fixed_video_urls = dedupe_urls(fixed_video_urls)

            if not image_urls and not fixed_video_urls:
                continue

            user_name = self._extract_user_name(text)
            date = self._extract_date(text)
            sku_info = self._extract_sku_info(text)
            content = self._extract_content(text)

            review = ReviewItem(
                user_name=user_name,
                date=date,
                sku_info=sku_info,
                content=content,
                source="taobao_review_dom",
                images=[
                    ReviewMedia(
                        url=u,
                        media_type="image",
                        ext=get_url_ext(u),
                        source="review_image",
                    )
                    for u in image_urls
                ],
                videos=[
                    ReviewMedia(
                        url=u,
                        media_type="video",
                        ext=self._guess_video_ext(u),
                        source="review_video",
                    )
                    for u in fixed_video_urls
                ],
            )

            reviews.append(review)

        return reviews

    def _filter_image_urls(self, urls: list[str]) -> list[str]:
        result = []

        blacklist = [
            "avatar",
            "logo",
            "icon",
            "sprite",
            "qrcode",
            "default",
            "loading",
        ]

        for url in urls:
            url = normalize_image_url(str(url).strip())

            if not url:
                continue

            lower = url.lower()

            if not lower.startswith("http"):
                continue

            if any(bad in lower for bad in blacklist):
                continue

            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"]):
                continue

            result.append(url)

        return dedupe_urls(result)

    def _filter_video_urls(self, urls: list[str]) -> list[str]:
        result = []

        for url in urls:
            url = normalize_image_url(str(url).strip())

            if not url:
                continue

            lower = url.lower()

            if lower.startswith("blob:"):
                result.append(url)
                continue

            if not lower.startswith("http"):
                continue

            if (
                ".mp4" in lower
                or ".m3u8" in lower
                or ".mov" in lower
                or ".m4v" in lower
                or ".webm" in lower
                or ".jpg" in lower
                or ".jpeg" in lower
                or ".png" in lower
                or ".webp" in lower
            ):
                result.append(url)

        return dedupe_urls(result)

    def _build_review_key(self, item: ReviewItem) -> str:
        image_part = "|".join([m.url for m in item.images])
        video_part = "|".join([m.url for m in item.videos])
        return f"{item.content}|{image_part}|{video_part}"

    def _extract_user_name(self, text: str) -> str:
        if not text:
            return ""

        # 常见开头：昵称 2026年...
        match = re.match(r"^(.{1,20}?)(?:\s+\d{4}年|\s+\d{4}-\d{1,2}-\d{1,2})", text)
        if match:
            return match.group(1).strip()

        return ""

    def _extract_date(self, text: str) -> str:
        patterns = [
            r"\d{4}年\d{1,2}月\d{1,2}日",
            r"\d{4}-\d{1,2}-\d{1,2}",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)

        return ""

    def _extract_sku_info(self, text: str) -> str:
        patterns = [
            r"已购[:：]?\s*([^。；;\n]+)",
            r"规格[:：]?\s*([^。；;\n]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        return ""

    def _extract_content(self, text: str) -> str:
        if not text:
            return ""

        content = text

        content = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", " ", content)
        content = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", content)
        content = re.sub(r"已购[:：]?\s*[^。；;\n]+", " ", content)
        content = re.sub(r"规格[:：]?\s*[^。；;\n]+", " ", content)
        content = re.sub(r"\s+", " ", content)

        return content.strip()[:500]

    def _guess_video_ext(self, url: str) -> str:
        lower = (url or "").lower()

        for ext in ["mp4", "mov", "m4v", "webm"]:
            if f".{ext}" in lower:
                return ext

        return "mp4"

    def _wait_for_login_finished(self, page, original_url: str) -> bool:
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
                try:
                    page.wait_for_timeout(check_interval_ms)
                except Exception:
                    time.sleep(1)

        try:
            page.goto(original_url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(2000)

            if not self._is_login_page(page):
                return True

        except Exception:
            pass

        return False

    def _is_login_page(self, page) -> bool:
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
        if not url:
            return False

        lower = url.lower()

        keywords = [
            "login.taobao.com",
            "login.tmall.com",
            "login.m.taobao.com",
            "login.m.tmall.com",
            "passport",
            "login",
        ]

        return any(keyword in lower for keyword in keywords)

    def _looks_like_login_html(self, html: str, url: str = "") -> bool:
        if not html:
            return False

        lower_html = html.lower()
        lower_url = url.lower() if url else ""

        if self._is_login_url(lower_url):
            return True

        signals = [
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

        for signal in signals:
            if signal.lower() in lower_html:
                hit_count += 1

        return hit_count >= 2
