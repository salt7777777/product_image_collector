import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from core.models import ReviewItem, ReviewMedia
from utils.url_utils import normalize_image_url, get_url_ext, dedupe_urls


class TaobaoReviewParser:
    """
    淘宝/天猫评价图/视频采集器。

    当前采集策略：
        1. 打开商品页；
        2. 检测登录；
        3. 点击“用户评价 / 累计评价 / 宝贝评价”；
        4. 滚动到评价区域；
        5. 点击“查看全部评价”；
        6. 点击“图/视频”筛选；
        7. 优先从网络响应中提取评价；
        8. 再从页面结构化数据中提取评价；
        9. 再从 DOM 可见评价卡片中兜底提取；
        10. 合并去重，只保留有图/视频的评价。
    """

    def __init__(
        self,
        platform: str = "taobao",
        headless: bool = False,
        login_wait_seconds: int = 180,
        timeout: int = 30000,
        log_callback=None,
        debug: bool = True,
    ):
        self.platform = platform if platform in ["taobao", "tmall"] else "taobao"
        self.user_data_dir = Path("browser_data") / self.platform
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds
        self.timeout = timeout
        self.log_callback = log_callback
        self.debug = debug

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def parse_reviews(
        self,
        url: str,
        limit: int = 50,
        include_video: bool = True,
        cancel_callback=None,
    ) -> list[ReviewItem]:
        """
        采集淘宝/天猫商品图/视频评价。
        """
        if not url:
            return []

        limit = max(1, int(limit or 50))
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        reviews: list[ReviewItem] = []
        seen_keys = set()

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

            # 网络接口候选评价池
            network_reviews: list[ReviewItem] = []
            self._bind_review_response_collector(
                page=page,
                reviews=network_reviews,
                include_video=include_video,
            )

            try:
                self.log("正在打开淘宝/天猫商品页，准备采集评价...")
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            except PlaywrightTimeoutError:
                self.log("商品页加载超时，继续尝试采集评价...")
            except Exception as e:
                context.close()
                raise RuntimeError(f"打开商品页失败：{e}")

            page.wait_for_timeout(2500)

            # 登录检测
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

            # ------------------------------------------------------------
            # 进入完整评价列表
            # ------------------------------------------------------------

            self._try_click_review_entry(page)
            page.wait_for_timeout(1000)

            if cancel_callback and cancel_callback():
                context.close()
                return reviews

            self._scroll_to_review_section(page)
            page.wait_for_timeout(1000)

            if cancel_callback and cancel_callback():
                context.close()
                return reviews

            self._try_click_view_all_reviews(page)
            page.wait_for_timeout(3000)

            if cancel_callback and cancel_callback():
                context.close()
                return reviews

            self._try_click_media_filter(page)
            page.wait_for_timeout(3500)

            if self.debug:
                self._save_debug_page(page, "after_open_all_reviews")

            # ------------------------------------------------------------
            # 1. 合并点击过程中捕获到的网络评价
            # ------------------------------------------------------------

            self._merge_reviews(
                target_reviews=reviews,
                new_reviews=network_reviews,
                seen_keys=seen_keys,
                limit=limit,
                source_label="网络响应初始",
            )

            if len(reviews) >= limit:
                context.close()
                self.log(f"淘宝/天猫评价采集完成：{len(reviews)} 条有图/视频评价。")
                return reviews

            # ------------------------------------------------------------
            # 2. 页面结构化数据
            # ------------------------------------------------------------

            state_reviews = self._extract_reviews_from_page_state(
                page=page,
                include_video=include_video,
            )

            self._merge_reviews(
                target_reviews=reviews,
                new_reviews=state_reviews,
                seen_keys=seen_keys,
                limit=limit,
                source_label="页面结构化数据",
            )

            if len(reviews) >= limit:
                context.close()
                self.log(f"淘宝/天猫评价采集完成：{len(reviews)} 条有图/视频评价。")
                return reviews

            # ------------------------------------------------------------
            # 3. DOM + 网络滚动采集
            # ------------------------------------------------------------

            max_scroll_rounds = 35
            stable_rounds = 0
            last_count = len(reviews)

            for round_index in range(max_scroll_rounds):
                if cancel_callback and cancel_callback():
                    break

                if len(reviews) >= limit:
                    break

                # 3.1 当前 DOM 可见评价
                dom_reviews = self._extract_reviews_from_page(
                    page=page,
                    include_video=include_video,
                )

                before_dom = len(reviews)

                self._merge_reviews(
                    target_reviews=reviews,
                    new_reviews=dom_reviews,
                    seen_keys=seen_keys,
                    limit=limit,
                    source_label="DOM",
                    silent=True,
                )

                dom_added = len(reviews) - before_dom

                # 3.2 合并滚动/点击期间捕获到的网络评价
                before_network = len(reviews)

                self._merge_reviews(
                    target_reviews=reviews,
                    new_reviews=network_reviews,
                    seen_keys=seen_keys,
                    limit=limit,
                    source_label="网络响应",
                    silent=True,
                )

                network_added = len(reviews) - before_network

                self.log(
                    f"评价采集中：第 {round_index + 1}/{max_scroll_rounds} 轮，"
                    f"DOM新增 {dom_added} 条，网络新增 {network_added} 条，累计 {len(reviews)} 条。"
                )

                if reviews and round_index == 0:
                    self._log_review_samples(reviews)

                if len(reviews) >= limit:
                    break

                if len(reviews) == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                last_count = len(reviews)

                if stable_rounds >= 10:
                    self.log("连续多轮未发现新评价，停止继续滚动。")
                    break

                scroll_result = self._scroll_review_area(page)

                if scroll_result:
                    try:
                        self.log(
                            f"评价列表滚动：before={scroll_result.get('before')}，"
                            f"after={scroll_result.get('after')}，"
                            f"container={scroll_result.get('usedContainer')}"
                        )
                    except Exception:
                        pass

                page.wait_for_timeout(2500)

            context.close()

        self.log(f"淘宝/天猫评价采集完成：{len(reviews)} 条有图/视频评价。")
        return reviews

    # ------------------------------------------------------------------
    # 页面交互
    # ------------------------------------------------------------------

    def _try_click_review_entry(self, page) -> bool:
        """
        尝试点击评价入口。
        """
        self.log("尝试打开评价区域...")

        keywords = [
            "用户评价",
            "累计评价",
            "宝贝评价",
        ]

        for keyword in keywords:
            try:
                locators = page.locator(f"text={keyword}")
                count = locators.count()

                for i in range(min(count, 10)):
                    locator = locators.nth(i)

                    try:
                        text = locator.inner_text(timeout=1000).strip()
                    except Exception:
                        text = ""

                    if text and len(text) > 40:
                        continue

                    locator.scroll_into_view_if_needed(timeout=2500)
                    page.wait_for_timeout(500)
                    locator.click(timeout=2500)
                    page.wait_for_timeout(2000)

                    self.log(f"已尝试点击评价入口：{text or keyword}")
                    return True

            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                () => {
                    const keywords = ['用户评价', '累计评价', '宝贝评价'];
                    const nodes = Array.from(document.querySelectorAll('button, a, div, span'));

                    const candidates = [];

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim();

                        if (!text) continue;
                        if (text.length > 40) continue;

                        if (!keywords.some(k => text.includes(k))) continue;

                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);

                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (style.display === 'none' || style.visibility === 'hidden') continue;

                        candidates.push({
                            node,
                            text,
                            area: rect.width * rect.height
                        });
                    }

                    if (!candidates.length) return '';

                    candidates.sort((a, b) => a.area - b.area);

                    const target = candidates[0].node;
                    target.scrollIntoView({block: 'center'});
                    target.click();

                    return candidates[0].text;
                }
                """
            )

            if clicked:
                page.wait_for_timeout(2000)
                self.log(f"已通过 JS 点击评价入口：{clicked}")
                return True

        except Exception:
            pass

        self.log("未找到明确评价入口，后续尝试直接滚动到评价区域。")
        return False

    def _scroll_to_review_section(self, page) -> bool:
        """
        滚动到用户评价区域附近。
        """
        self.log("尝试滚动到用户评价区域...")

        try:
            found = page.evaluate(
                """
                () => {
                    const keywords = [
                        '用户评价',
                        '累计评价',
                        '宝贝评价',
                        '查看全部评价'
                    ];

                    const nodes = Array.from(
                        document.querySelectorAll('div, section, h1, h2, h3, span, a, button')
                    );

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim();

                        if (!text) continue;

                        if (keywords.some(k => text.includes(k))) {
                            node.scrollIntoView({block: 'center'});
                            return text;
                        }
                    }

                    return '';
                }
                """
            )

            if found:
                page.wait_for_timeout(1200)
                self.log(f"已滚动到评价区域附近：{found[:40]}")
                return True

        except Exception:
            pass

        try:
            for _ in range(5):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(600)
        except Exception:
            pass

        return False

    def _try_click_view_all_reviews(self, page) -> bool:
        """
        点击“查看全部评价”。
        """
        self.log("尝试点击“查看全部评价”...")

        keywords = [
            "查看全部评价",
            "全部评价",
            "查看更多评价",
            "查看所有评价",
            "展开全部评价",
        ]

        for keyword in keywords:
            try:
                locators = page.locator(f"text={keyword}")
                count = locators.count()

                for i in range(min(count, 10)):
                    locator = locators.nth(i)

                    try:
                        text = locator.inner_text(timeout=1000).strip()
                    except Exception:
                        text = ""

                    if text and len(text) > 50:
                        continue

                    locator.scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_timeout(500)
                    locator.click(timeout=3000)
                    page.wait_for_timeout(2500)

                    self.log(f"已点击评价完整列表入口：{text or keyword}")
                    return True

            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                () => {
                    const keywords = [
                        '查看全部评价',
                        '全部评价',
                        '查看更多评价',
                        '查看所有评价',
                        '展开全部评价'
                    ];

                    const nodes = Array.from(document.querySelectorAll('button, a, div, span'));

                    const candidates = [];

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim();

                        if (!text) continue;

                        if (!keywords.some(k => text.includes(k))) continue;

                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);

                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (style.visibility === 'hidden' || style.display === 'none') continue;

                        candidates.push({
                            node,
                            text,
                            area: rect.width * rect.height
                        });
                    }

                    if (!candidates.length) {
                        return '';
                    }

                    candidates.sort((a, b) => {
                        const lenDiff = a.text.length - b.text.length;
                        if (lenDiff !== 0) return lenDiff;
                        return a.area - b.area;
                    });

                    const target = candidates[0].node;
                    target.scrollIntoView({block: 'center'});
                    target.click();

                    return candidates[0].text;
                }
                """
            )

            if clicked:
                page.wait_for_timeout(2500)
                self.log(f"已通过 JS 点击评价完整列表入口：{clicked}")
                return True

        except Exception as e:
            self.log(f"JS 点击查看全部评价失败：{e}")

        self.log("未找到“查看全部评价”入口，可能当前页面已处于完整评价列表，或页面结构不同。")
        return False

    def _try_click_media_filter(self, page) -> bool:
        """
        点击“图/视频”评价筛选。
        """
        self.log("尝试点击“图/视频”评价筛选...")

        keywords = [
            "图/视频",
            "有图",
            "晒图",
        ]

        for keyword in keywords:
            try:
                locators = page.locator(f"text={keyword}")
                count = locators.count()

                for i in range(min(count, 10)):
                    locator = locators.nth(i)

                    try:
                        text = locator.inner_text(timeout=1000).strip()
                    except Exception:
                        text = ""

                    if text and len(text) > 30:
                        continue

                    locator.scroll_into_view_if_needed(timeout=2000)
                    page.wait_for_timeout(300)
                    locator.click(timeout=2500)
                    page.wait_for_timeout(2000)

                    self.log(f"已点击评价筛选：{text or keyword}")
                    return True

            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                () => {
                    const keywords = ['图/视频', '有图', '晒图'];

                    const nodes = Array.from(document.querySelectorAll('button, a, div, span'));

                    const candidates = [];

                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim();

                        if (!text) continue;
                        if (text.length > 30) continue;

                        if (!keywords.some(k => text.includes(k))) continue;

                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);

                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (style.display === 'none' || style.visibility === 'hidden') continue;

                        candidates.push({
                            node,
                            text,
                            area: rect.width * rect.height
                        });
                    }

                    if (!candidates.length) return '';

                    candidates.sort((a, b) => a.area - b.area);

                    const target = candidates[0].node;
                    target.scrollIntoView({block: 'center'});
                    target.click();

                    return candidates[0].text;
                }
                """
            )

            if clicked:
                page.wait_for_timeout(2000)
                self.log(f"已通过 JS 点击评价筛选：{clicked}")
                return True

        except Exception:
            pass

        self.log("未找到“图/视频”评价筛选，继续采集当前评价列表。")
        return False

    def _scroll_review_area(self, page):
        """
        滚动完整评价列表。
        """
        try:
            result = page.evaluate(
                """
                () => {
                    function cleanText(text) {
                        return (text || '').replace(/\\s+/g, ' ').trim();
                    }

                    function isVisible(el) {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return (
                            rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                    }

                    const keywords = [
                        '用户评价',
                        '图/视频',
                        '默认排序',
                        '款式筛选',
                        '为你展示真实评价',
                        '商家回复',
                        '追评',
                        '已购'
                    ];

                    const candidates = Array.from(document.querySelectorAll('*'))
                        .filter(el => {
                            if (!isVisible(el)) return false;

                            if (el.scrollHeight <= el.clientHeight + 100) return false;

                            const text = cleanText(el.innerText || el.textContent || '');
                            if (!text) return false;

                            const hit = keywords.some(k => text.includes(k));
                            if (!hit) return false;

                            const rect = el.getBoundingClientRect();

                            if (rect.width < 300 || rect.height < 300) return false;

                            return true;
                        })
                        .map(el => {
                            const rect = el.getBoundingClientRect();
                            const text = cleanText(el.innerText || el.textContent || '');

                            let score = 0;
                            if (text.includes('用户评价')) score += 20;
                            if (text.includes('图/视频')) score += 20;
                            if (text.includes('默认排序')) score += 10;
                            if (text.includes('已购')) score += 10;

                            score += Math.min(el.scrollHeight - el.clientHeight, 3000) / 100;

                            return {
                                el,
                                score,
                                top: rect.top,
                                height: rect.height,
                                before: el.scrollTop,
                                maxScroll: el.scrollHeight - el.clientHeight
                            };
                        })
                        .sort((a, b) => b.score - a.score);

                    if (candidates.length > 0) {
                        const target = candidates[0].el;
                        const before = target.scrollTop;

                        target.scrollTop = Math.min(
                            target.scrollTop + 1200,
                            target.scrollHeight
                        );

                        target.dispatchEvent(new WheelEvent('wheel', {
                            deltaY: 1200,
                            bubbles: true,
                            cancelable: true
                        }));

                        return {
                            usedContainer: true,
                            before,
                            after: target.scrollTop,
                            maxScroll: target.scrollHeight - target.clientHeight
                        };
                    }

                    const beforeWindow = window.scrollY || document.documentElement.scrollTop || 0;
                    window.scrollBy(0, 1200);
                    const afterWindow = window.scrollY || document.documentElement.scrollTop || 0;

                    return {
                        usedContainer: false,
                        before: beforeWindow,
                        after: afterWindow,
                        maxScroll: document.documentElement.scrollHeight
                    };
                }
                """
            )

            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                pass

            return result

        except Exception:
            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------
    # 合并去重
    # ------------------------------------------------------------------

    def _merge_reviews(
        self,
        target_reviews: list[ReviewItem],
        new_reviews: list[ReviewItem],
        seen_keys: set,
        limit: int,
        source_label: str = "",
        silent: bool = False,
    ) -> int:
        added = 0

        for item in new_reviews or []:
            if len(target_reviews) >= limit:
                break

            if not item.images and not item.videos:
                continue

            if not self._is_valid_review_item(item):
                continue

            key = self._build_review_key(item)

            if key in seen_keys:
                continue

            seen_keys.add(key)
            item.index = len(target_reviews) + 1
            target_reviews.append(item)
            added += 1

        if added and not silent:
            self.log(f"{source_label}加入评价：{added} 条，累计 {len(target_reviews)} 条。")

        return added

    # ------------------------------------------------------------------
    # 网络响应捕获
    # ------------------------------------------------------------------

    def _bind_review_response_collector(
        self,
        page,
        reviews: list[ReviewItem],
        include_video: bool = True,
    ):
        """
        捕获淘宝/天猫评价接口响应。
        """

        def handle_response(response):
            try:
                response_url = response.url or ""

                if not self._is_review_response_url(response_url):
                    return

                content_type = ""
                try:
                    content_type = response.headers.get("content-type", "")
                except Exception:
                    pass

                lower_content_type = content_type.lower()

                if (
                    "json" not in lower_content_type
                    and "javascript" not in lower_content_type
                    and "text" not in lower_content_type
                    and "html" not in lower_content_type
                    and "plain" not in lower_content_type
                ):
                    return

                try:
                    text = response.text()
                except Exception:
                    return

                if not text:
                    return

                parsed_reviews = self._extract_reviews_from_response_text(
                    text=text,
                    response_url=response_url,
                    include_video=include_video,
                )

                if parsed_reviews:
                    reviews.extend(parsed_reviews)

                    self.log(
                        f"捕获评价接口数据：候选 {len(parsed_reviews)} 条，"
                        f"网络候选累计 {len(reviews)} 条。"
                    )

            except Exception:
                pass

        try:
            page.on("response", handle_response)
            self.log("已启用淘宝/天猫评价接口响应捕获。")
        except Exception as e:
            self.log(f"绑定评价接口响应捕获失败：{e}")

    def _is_review_response_url(self, url: str) -> bool:
        if not url:
            return False

        lower = url.lower()

        ignore_keywords = [
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
            ".css",
            ".woff",
            ".svg",
            "log.mmstat",
            "arms-retcode",
            "g.alicdn.com",
            "cnzz",
            "aplus",
            "umeng",
            "beacon",
        ]

        if any(x in lower for x in ignore_keywords):
            return False

        review_keywords = [
            "mtop",
            "rate",
            "review",
            "comment",
            "feed",
            "feedback",
            "list_detail_rate",
            "detailrate",
            "auctionrate",
            "itemrate",
            "ratejson",
            "rate.htm",
            "queryrate",
        ]

        if not any(x in lower for x in review_keywords):
            return False

        domain_keywords = [
            "taobao.com",
            "tmall.com",
            "alicdn.com",
            "aliyun.com",
            "alibaba",
        ]

        return any(x in lower for x in domain_keywords)

    def _extract_reviews_from_response_text(
        self,
        text: str,
        response_url: str = "",
        include_video: bool = True,
    ) -> list[ReviewItem]:
        data = self._try_parse_json_like_text(text)

        if data is None:
            return []

        raw_items = self._find_review_items_in_data(data)

        reviews: list[ReviewItem] = []

        for raw in raw_items:
            review = self._build_review_from_raw_item(
                raw=raw,
                include_video=include_video,
                source="taobao_review_network",
            )

            if review and self._is_valid_review_item(review):
                reviews.append(review)

        return reviews

    def _try_parse_json_like_text(self, text: str):
        import json
        import html as html_lib

        if not text:
            return None

        text = text.strip()
        text = html_lib.unescape(text)

        # JSONP: callback({...})
        jsonp_match = re.search(r"^[\w.$]+\((.*)\)\s*;?$", text, flags=re.S)
        if jsonp_match:
            text = jsonp_match.group(1).strip()

        try:
            return json.loads(text)
        except Exception:
            pass

        # 截取第一个 JSON 对象
        first = text.find("{")
        last = text.rfind("}")

        if first >= 0 and last > first:
            candidate = text[first:last + 1]

            try:
                return json.loads(candidate)
            except Exception:
                pass

        return None

    def _find_review_items_in_data(self, data, depth: int = 0) -> list[dict]:
        if data is None or depth > 12:
            return []

        result = []

        # 有些 mtop 的 data 里面会把 JSON 放成字符串
        if isinstance(data, str):
            parsed = self._try_parse_json_like_text(data)
            if parsed is not None and parsed is not data:
                return self._find_review_items_in_data(parsed, depth + 1)
            return []

        if isinstance(data, list):
            for item in data:
                result.extend(self._find_review_items_in_data(item, depth + 1))
            return result

        if not isinstance(data, dict):
            return []

        if self._looks_like_review_raw_item(data):
            return [data]

        candidate_keys = [
            "items",
            "list",
            "comments",
            "rates",
            "rateList",
            "feedList",
            "feeds",
            "result",
            "data",
            "rateVO",
            "group",
            "module",
            "rateData",
            "reviewData",
        ]

        for key, value in data.items():
            if key in candidate_keys:
                result.extend(self._find_review_items_in_data(value, depth + 1))
            else:
                if depth < 6 and isinstance(value, (dict, list, str)):
                    result.extend(self._find_review_items_in_data(value, depth + 1))

        return result

    def _looks_like_review_raw_item(self, item: dict) -> bool:
        if not isinstance(item, dict):
            return False

        text_keys = [
            "content",
            "feedback",
            "comment",
            "rateContent",
            "reviewContent",
            "commentContent",
        ]

        user_keys = [
            "userName",
            "nick",
            "nickName",
            "displayUserNick",
            "userNick",
        ]

        media_keys = [
            "media",
            "images",
            "imageList",
            "pics",
            "photos",
            "videos",
            "videoList",
        ]

        has_text = any(item.get(k) for k in text_keys)
        has_user = any(item.get(k) for k in user_keys)
        has_media = any(item.get(k) for k in media_keys)
        has_sku = bool(
            item.get("skuInfo")
            or item.get("sku")
            or item.get("auctionSku")
            or item.get("skuText")
        )

        return has_media and (has_text or has_user or has_sku)

    def _build_review_from_raw_item(
        self,
        raw: dict,
        include_video: bool = True,
        source: str = "taobao_review_network",
    ) -> ReviewItem | None:
        if not isinstance(raw, dict):
            return None

        user_name = (
            raw.get("userName")
            or raw.get("nick")
            or raw.get("nickName")
            or raw.get("displayUserNick")
            or raw.get("userNick")
            or ""
        )

        content = (
            raw.get("content")
            or raw.get("feedback")
            or raw.get("comment")
            or raw.get("rateContent")
            or raw.get("reviewContent")
            or raw.get("commentContent")
            or ""
        )

        date = (
            raw.get("dateTime")
            or raw.get("createTime")
            or raw.get("creationTime")
            or raw.get("rateDate")
            or raw.get("gmtCreate")
            or ""
        )

        date = self._normalize_date(str(date or ""))

        sku_info = (
            raw.get("skuInfo")
            or raw.get("sku")
            or raw.get("auctionSku")
            or raw.get("skuText")
            or ""
        )

        sku_info = str(sku_info or "").strip()
        sku_info = re.sub(r"^商品规格[:：]?", "", sku_info).strip()

        image_urls, video_urls = self._extract_media_urls_from_raw_item(
            raw=raw,
            include_video=include_video,
        )

        image_urls = self._filter_image_urls(image_urls)
        video_urls = self._filter_video_urls(video_urls)

        if not image_urls and not video_urls:
            return None

        return ReviewItem(
            user_name=str(user_name or "").strip(),
            date=date,
            sku_info=sku_info,
            content=str(content or "").strip(),
            like_count=self._extract_like_count_from_raw_item(raw),
            source=source,
            images=[
                ReviewMedia(
                    url=u,
                    media_type="image",
                    ext=get_url_ext(u),
                    source="review_image_network",
                )
                for u in image_urls
            ],
            videos=[
                ReviewMedia(
                    url=u,
                    media_type="video",
                    ext=self._guess_video_ext(u),
                    source="review_video_network",
                )
                for u in video_urls
            ],
        )

    def _extract_media_urls_from_raw_item(
        self,
        raw: dict,
        include_video: bool = True,
    ) -> tuple[list[str], list[str]]:
        images = []
        videos = []

        def add_image(u):
            if u:
                images.append(str(u))

        def add_video(u):
            if u:
                videos.append(str(u))

        # 1. media 数组
        media = raw.get("media")

        if isinstance(media, list):
            for m in media:
                if not isinstance(m, dict):
                    continue

                m_type = str(m.get("type") or "").lower()

                image_url = (
                    m.get("imageUrl")
                    or m.get("picUrl")
                    or m.get("thumbnail")
                    or m.get("coverUrl")
                    or m.get("cover")
                    or ""
                )

                video_url = (
                    m.get("videoUrl")
                    or m.get("videoURL")
                    or m.get("video")
                    or m.get("videoPath")
                    or m.get("url")
                    or ""
                )

                if m_type == "video":
                    if include_video:
                        add_video(video_url)

                    # 视频封面也作为图片保留
                    add_image(image_url)
                else:
                    add_image(image_url or m.get("url"))

        # 2. images / imageList / pics / photos
        for key in ["images", "imageList", "pics", "photos"]:
            value = raw.get(key)

            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        add_image(item)
                    elif isinstance(item, dict):
                        add_image(
                            item.get("imageUrl")
                            or item.get("url")
                            or item.get("picUrl")
                            or item.get("src")
                        )

        # 3. videos / videoList
        if include_video:
            for key in ["videos", "videoList"]:
                value = raw.get(key)

                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            add_video(item)
                        elif isinstance(item, dict):
                            add_video(
                                item.get("videoUrl")
                                or item.get("url")
                                or item.get("src")
                                or item.get("video")
                            )

        return dedupe_urls(images), dedupe_urls(videos)

    def _extract_like_count_from_raw_item(self, raw: dict) -> int:
        for key in ["likeCount", "usefulCount", "helpfulCount", "likedCount"]:
            value = raw.get(key)

            if value is None:
                continue

            try:
                return int(value)
            except Exception:
                pass

        return 0

    # ------------------------------------------------------------------
    # 页面结构化评价数据
    # ------------------------------------------------------------------

    def _extract_reviews_from_page_state(
        self,
        page,
        include_video: bool = True,
    ) -> list[ReviewItem]:
        try:
            raw_items = page.evaluate(
                """
                (includeVideo) => {
                    function normalizeUrl(url) {
                        if (!url) return '';
                        url = String(url).trim();
                        if (!url) return '';
                        if (url.startsWith('//')) return 'https:' + url;
                        return url;
                    }

                    function findRateItems(obj, depth = 0) {
                        if (!obj || depth > 10) return [];

                        if (Array.isArray(obj)) {
                            let result = [];
                            for (const item of obj) {
                                result = result.concat(findRateItems(item, depth + 1));
                            }
                            return result;
                        }

                        if (typeof obj !== 'object') return [];

                        if (
                            obj.rateVO &&
                            obj.rateVO.group &&
                            Array.isArray(obj.rateVO.group.items)
                        ) {
                            return obj.rateVO.group.items;
                        }

                        if (
                            obj.group &&
                            Array.isArray(obj.group.items) &&
                            (
                                obj.totalCount !== undefined ||
                                obj.items.some(x => x && x.media && x.content)
                            )
                        ) {
                            return obj.group.items;
                        }

                        if (
                            Array.isArray(obj.items) &&
                            obj.items.some(x => x && x.media && x.content)
                        ) {
                            return obj.items;
                        }

                        let result = [];

                        for (const key of Object.keys(obj)) {
                            const value = obj[key];

                            if (!value) continue;

                            if (
                                key === 'rateVO' ||
                                key === 'loaderData' ||
                                key === 'data' ||
                                key === 'res' ||
                                key === 'home' ||
                                key === 'group' ||
                                key === 'items'
                            ) {
                                result = result.concat(findRateItems(value, depth + 1));
                            }
                        }

                        return result;
                    }

                    const roots = [];

                    try {
                        if (window.__ICE_APP_CONTEXT__) roots.push(window.__ICE_APP_CONTEXT__);
                    } catch (e) {}

                    try {
                        if (window.__INITIAL_STATE__) roots.push(window.__INITIAL_STATE__);
                    } catch (e) {}

                    try {
                        if (window.__INIT_DATA__) roots.push(window.__INIT_DATA__);
                    } catch (e) {}

                    try {
                        if (window.__APOLLO_STATE__) roots.push(window.__APOLLO_STATE__);
                    } catch (e) {}

                    let items = [];

                    for (const root of roots) {
                        items = items.concat(findRateItems(root));
                    }

                    const result = [];

                    for (const item of items) {
                        if (!item || typeof item !== 'object') continue;

                        const media = Array.isArray(item.media) ? item.media : [];

                        const images = [];
                        const videos = [];

                        for (const m of media) {
                            if (!m || typeof m !== 'object') continue;

                            const type = String(m.type || '').toLowerCase();

                            const imageUrl = normalizeUrl(
                                m.imageUrl ||
                                m.picUrl ||
                                m.thumbnail ||
                                m.coverUrl ||
                                ''
                            );

                            const videoUrl = normalizeUrl(
                                m.videoUrl ||
                                m.videoURL ||
                                m.video ||
                                m.videoPath ||
                                m.url ||
                                ''
                            );

                            if (type === 'video') {
                                if (includeVideo && videoUrl) {
                                    videos.push(videoUrl);
                                }

                                if (imageUrl) {
                                    images.push(imageUrl);
                                }
                            } else {
                                if (imageUrl) {
                                    images.push(imageUrl);
                                }
                            }
                        }

                        if (!images.length && !videos.length) continue;

                        result.push({
                            feedId: item.feedId || item.id || '',
                            userName: item.userName || item.nick || '',
                            content: item.content || item.feedback || '',
                            dateTime: item.dateTime || item.createTime || item.creationTime || '',
                            skuInfo: item.skuInfo || item.sku || '',
                            mediaSize: item.mediaSize || '',
                            images,
                            videos
                        });
                    }

                    return result;
                }
                """,
                include_video,
            )
        except Exception as e:
            self.log(f"页面结构化评价数据提取失败：{e}")
            raw_items = []

        reviews: list[ReviewItem] = []

        for raw in raw_items or []:
            image_urls = self._filter_image_urls(raw.get("images", []) or [])
            video_urls = self._filter_video_urls(raw.get("videos", []) or [])

            if not image_urls and not video_urls:
                continue

            sku_info = str(raw.get("skuInfo", "") or "").strip()
            sku_info = re.sub(r"^商品规格[:：]?", "", sku_info).strip()

            date = self._normalize_date(str(raw.get("dateTime", "") or ""))

            review = ReviewItem(
                user_name=str(raw.get("userName", "") or "").strip(),
                date=date,
                sku_info=sku_info,
                content=str(raw.get("content", "") or "").strip(),
                source="taobao_review_page_state",
                images=[
                    ReviewMedia(
                        url=u,
                        media_type="image",
                        ext=get_url_ext(u),
                        source="review_image_state",
                    )
                    for u in image_urls
                ],
                videos=[
                    ReviewMedia(
                        url=u,
                        media_type="video",
                        ext=self._guess_video_ext(u),
                        source="review_video_state",
                    )
                    for u in video_urls
                ],
            )

            if self._is_valid_review_item(review):
                reviews.append(review)

        if reviews:
            self.log(f"从页面结构化数据提取评价：{len(reviews)} 条。")

        return reviews

    # ------------------------------------------------------------------
    # DOM 评价提取
    # ------------------------------------------------------------------

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
                        return (text || '').replace(/\\s+/g, ' ').trim();
                    }

                    function normalizeUrl(url) {
                        if (!url) return '';
                        url = String(url).trim();
                        if (!url) return '';
                        if (url.startsWith('//')) return 'https:' + url;
                        return url;
                    }

                    function isVisible(el) {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return (
                            rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                    }

                    function isBadImageUrl(url) {
                        const u = normalizeUrl(url).toLowerCase();

                        const badWords = [
                            'avatar', 'logo', 'icon', 'sprite', 'qrcode',
                            'default', 'loading', 'blank', 'transparent',
                            'shop', 'store', 'seller', 'wangwang', 'service',
                            'rate-star', 'star', 'tmall-logo', 'taobao-logo',
                            'favicon', 'badge', 'medal', 'level',
                            'placeholder', 'no_pic', 'nopic'
                        ];

                        if (badWords.some(w => u.includes(w))) return true;
                        if (u.includes('tps-')) return true;
                        if (/[-_/](?:1|2)-tps-\\d+-\\d+/.test(u)) return true;

                        if (
                            u.includes('145-145') ||
                            u.includes('145x145') ||
                            u.includes('160-160') ||
                            u.includes('160x160')
                        ) {
                            return true;
                        }

                        return false;
                    }

                    function isImageUrl(url) {
                        const u = normalizeUrl(url).toLowerCase();

                        if (!u.startsWith('http')) return false;

                        if (!(
                            u.includes('.jpg') ||
                            u.includes('.jpeg') ||
                            u.includes('.png') ||
                            u.includes('.webp') ||
                            u.includes('.gif') ||
                            u.includes('.avif') ||
                            u.includes('.apng')
                        )) {
                            return false;
                        }

                        if (isBadImageUrl(u)) return false;

                        const isRateImage =
                            u.includes('-rate') ||
                            u.includes('/rate/') ||
                            u.includes('rate.');

                        const isAlImage =
                            u.includes('img.alicdn.com/imgextra') ||
                            u.includes('gw.alicdn.com/bao/uploaded') ||
                            u.includes('alicdn.com');

                        return isRateImage || isAlImage;
                    }

                    function isVideoUrl(url) {
                        const u = normalizeUrl(url).toLowerCase();

                        if (!u) return false;
                        if (u.startsWith('blob:')) return true;
                        if (!u.startsWith('http')) return false;

                        return (
                            u.includes('.mp4') ||
                            u.includes('.m3u8') ||
                            u.includes('.mov') ||
                            u.includes('.m4v') ||
                            u.includes('.webm')
                        );
                    }

                    function hasReviewSemantic(text) {
                        if (!text) return false;

                        const hasDate =
                            /\\d{4}年\\d{1,2}月\\d{1,2}日/.test(text) ||
                            /\\d{4}-\\d{1,2}-\\d{1,2}/.test(text);

                        const hasBought = text.includes('已购');

                        return hasDate || hasBought;
                    }

                    function getTextLines(root) {
                        const lines = [];

                        const walker = document.createTreeWalker(
                            root,
                            NodeFilter.SHOW_TEXT,
                            {
                                acceptNode(node) {
                                    const text = cleanText(node.nodeValue || '');
                                    if (!text) return NodeFilter.FILTER_REJECT;

                                    const parent = node.parentElement;
                                    if (!parent) return NodeFilter.FILTER_REJECT;

                                    const style = window.getComputedStyle(parent);
                                    if (style.display === 'none' || style.visibility === 'hidden') {
                                        return NodeFilter.FILTER_REJECT;
                                    }

                                    return NodeFilter.FILTER_ACCEPT;
                                }
                            }
                        );

                        let node;

                        while ((node = walker.nextNode())) {
                            const text = cleanText(node.nodeValue || '');

                            if (!text) continue;
                            if (text.length > 300) continue;

                            const skipTexts = [
                                '全部', '图/视频', '追评', '默认排序',
                                '图集', '款式筛选', '为你展示真实评价'
                            ];

                            if (skipTexts.includes(text)) continue;

                            if (lines.length === 0 || lines[lines.length - 1] !== text) {
                                lines.push(text);
                            }
                        }

                        return lines.slice(0, 80);
                    }

                    function collectImages(card) {
                        const urls = [];
                        const imgs = Array.from(card.querySelectorAll('img'));

                        for (const img of imgs) {
                            const rect = img.getBoundingClientRect();

                            if (rect.width > 0 && rect.height > 0) {
                                if (rect.width < 50 || rect.height < 50) {
                                    continue;
                                }
                            }

                            const candidates = [
                                img.currentSrc,
                                img.src,
                                img.getAttribute('data-src'),
                                img.getAttribute('data-ks-lazyload'),
                                img.getAttribute('data-lazyload'),
                                img.getAttribute('data-original'),
                                img.getAttribute('data-img'),
                                img.getAttribute('data-url'),
                                img.getAttribute('srcset')
                            ];

                            for (let u of candidates) {
                                if (!u) continue;

                                if (u.includes(' ')) {
                                    u = u.split(',')[0].trim().split(' ')[0];
                                }

                                u = normalizeUrl(u);

                                if (isImageUrl(u)) {
                                    urls.push(u);
                                }
                            }
                        }

                        return Array.from(new Set(urls)).slice(0, 15);
                    }

                    function collectVideos(card) {
                        if (!includeVideo) return [];

                        const urls = [];

                        const videos = Array.from(card.querySelectorAll('video'));

                        for (const video of videos) {
                            const candidates = [video.currentSrc, video.src];

                            for (const u of candidates) {
                                if (isVideoUrl(u)) {
                                    urls.push(normalizeUrl(u));
                                }
                            }

                            const sources = Array.from(video.querySelectorAll('source'));

                            for (const source of sources) {
                                const u = source.src || source.getAttribute('src');

                                if (isVideoUrl(u)) {
                                    urls.push(normalizeUrl(u));
                                }
                            }
                        }

                        const nodes = Array.from(card.querySelectorAll('*'));

                        for (const node of nodes) {
                            for (const attr of [
                                'data-video',
                                'data-video-url',
                                'data-url',
                                'data-src',
                                'data-mp4'
                            ]) {
                                const u = node.getAttribute(attr);

                                if (isVideoUrl(u)) {
                                    urls.push(normalizeUrl(u));
                                }
                            }
                        }

                        return Array.from(new Set(urls)).slice(0, 8);
                    }

                    function findReviewRoot() {
                        const all = Array.from(document.querySelectorAll('div, section, main'));
                        const candidates = [];

                        for (const el of all) {
                            if (!isVisible(el)) continue;

                            const text = cleanText(el.innerText || el.textContent || '');
                            if (!text) continue;

                            const hasReviewWords =
                                text.includes('用户评价') ||
                                text.includes('宝贝评价') ||
                                text.includes('累计评价') ||
                                text.includes('图/视频') ||
                                text.includes('默认排序') ||
                                text.includes('款式筛选') ||
                                text.includes('为你展示真实评价');

                            if (!hasReviewWords) continue;

                            const imgCount = el.querySelectorAll('img').length;
                            const videoCount = el.querySelectorAll('video').length;

                            if (imgCount + videoCount <= 0) continue;

                            const rect = el.getBoundingClientRect();

                            if (rect.width < 300 || rect.height < 200) continue;

                            candidates.push({
                                el,
                                score:
                                    (text.includes('用户评价') ? 20 : 0) +
                                    (text.includes('图/视频') ? 20 : 0) +
                                    (text.includes('默认排序') ? 10 : 0) +
                                    (text.includes('款式筛选') ? 10 : 0) +
                                    (text.includes('已购') ? 10 : 0) +
                                    Math.min(imgCount, 30)
                            });
                        }

                        candidates.sort((a, b) => b.score - a.score);
                        return candidates.length ? candidates[0].el : document.body;
                    }

                    function findReviewCards(root) {
                        const nodes = Array.from(root.querySelectorAll('li, article, section, div'));
                        const candidates = [];

                        for (const node of nodes) {
                            if (!isVisible(node)) continue;

                            const text = cleanText(node.innerText || node.textContent || '');

                            if (!text || text.length < 8) continue;

                            const rect = node.getBoundingClientRect();

                            if (rect.width < 220 || rect.height < 90) continue;
                            if (rect.height > 950) continue;
                            if (!hasReviewSemantic(text)) continue;

                            const images = collectImages(node);
                            const videos = collectVideos(node);

                            if (images.length === 0 && videos.length === 0) continue;

                            const lines = getTextLines(node);

                            candidates.push({
                                node,
                                text,
                                lines,
                                images,
                                videos,
                                area: rect.width * rect.height
                            });
                        }

                        const filtered = [];

                        for (const item of candidates) {
                            let containsOther = false;

                            for (const other of candidates) {
                                if (item === other) continue;

                                if (
                                    item.node !== other.node &&
                                    item.node.contains(other.node) &&
                                    item.area > other.area * 1.4
                                ) {
                                    containsOther = true;
                                    break;
                                }
                            }

                            if (!containsOther) {
                                filtered.push(item);
                            }
                        }

                        return filtered;
                    }

                    const root = findReviewRoot();
                    const cards = findReviewCards(root);

                    return cards.map(card => ({
                        text: card.text,
                        lines: card.lines,
                        imageUrls: card.images,
                        videoUrls: card.videos
                    }));
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
            lines = raw.get("lines", []) or []

            image_urls = self._filter_image_urls(raw.get("imageUrls", []) or [])
            video_urls = self._filter_video_urls(raw.get("videoUrls", []) or [])

            image_urls = dedupe_urls(image_urls)
            video_urls = dedupe_urls(video_urls)

            if not image_urls and not video_urls:
                continue

            fields = self._extract_review_fields_from_lines(lines, text)

            review = ReviewItem(
                user_name=fields.get("user_name", ""),
                date=fields.get("date", ""),
                sku_info=fields.get("sku_info", ""),
                content=fields.get("content", ""),
                like_count=fields.get("like_count", 0),
                source="taobao_review_dom_lines",
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
                    for u in video_urls
                ],
            )

            if self._is_valid_review_item(review):
                reviews.append(review)

        self.log(f"DOM候选评价：{len(raw_items or [])} 条，过滤后：{len(reviews)} 条")
        return reviews

    # ------------------------------------------------------------------
    # URL 过滤
    # ------------------------------------------------------------------

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
            "blank",
            "transparent",
            "shop",
            "store",
            "seller",
            "wangwang",
            "service",
            "rate-star",
            "star",
            "tmall-logo",
            "taobao-logo",
            "favicon",
            "badge",
            "medal",
            "level",
            "placeholder",
            "no_pic",
            "nopic",
        ]

        for url in urls:
            url = normalize_image_url(str(url).strip())

            if not url:
                continue

            lower = url.lower()

            if not lower.startswith("http"):
                continue

            if not any(
                ext in lower
                for ext in [
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    ".gif",
                    ".avif",
                    ".apng",
                ]
            ):
                continue

            if any(bad in lower for bad in blacklist):
                continue

            if "tps-" in lower:
                continue

            if re.search(r"[-_/](?:1|2)-tps-\d+-\d+", lower):
                continue

            if any(x in lower for x in ["145-145", "145x145", "160-160", "160x160"]):
                continue

            is_rate_image = (
                "-rate" in lower
                or "/rate/" in lower
                or "rate." in lower
            )

            is_alicdn_image = (
                "img.alicdn.com/imgextra" in lower
                or "gw.alicdn.com/bao/uploaded" in lower
                or "alicdn.com" in lower
            )

            if not is_rate_image and not is_alicdn_image:
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
            ):
                result.append(url)

        return dedupe_urls(result)

    # ------------------------------------------------------------------
    # 文本解析
    # ------------------------------------------------------------------

    def _extract_review_fields_from_lines(self, lines: list[str], full_text: str = "") -> dict:
        cleaned_lines = []

        for line in lines or []:
            line = str(line or "").strip()
            line = re.sub(r"\s+", " ", line)

            if not line:
                continue

            if line in [
                "全部",
                "图/视频",
                "追评",
                "默认排序",
                "图集",
                "款式筛选",
                "为你展示真实评价",
            ]:
                continue

            if len(line) > 300:
                continue

            cleaned_lines.append(line)

        if not cleaned_lines and full_text:
            cleaned_lines = [
                x.strip()
                for x in re.split(r"[\n\r]+", full_text)
                if x.strip()
            ]

        user_name = ""
        date = ""
        sku_info = ""
        content_parts = []
        like_count = 0

        date_pattern = re.compile(
            r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}"
        )

        for line in cleaned_lines:
            m = date_pattern.search(line)
            if m:
                date = m.group(0)
                break

        if date:
            for i, line in enumerate(cleaned_lines):
                if date in line:
                    before = line.split(date)[0].strip()
                    before = re.sub(
                        r"(88VIP|VIP|V\d+|蓝钻|红钻|黄钻)$",
                        "",
                        before,
                        flags=re.I,
                    ).strip()

                    if before and len(before) <= 30:
                        user_name = before
                        break

                    for prev in reversed(cleaned_lines[:i]):
                        if len(prev) <= 30 and not date_pattern.search(prev):
                            if not any(x in prev for x in ["已购", "规格", "图/视频", "默认排序"]):
                                user_name = prev.strip()
                                break

                    break

        if not user_name:
            for line in cleaned_lines[:3]:
                if len(line) <= 30 and not date_pattern.search(line):
                    if not any(x in line for x in ["已购", "规格", "用户评价", "图/视频"]):
                        user_name = line
                        break

        for line in cleaned_lines:
            if "已购" in line:
                sku = re.sub(r".*?已购[:：]?", "", line).strip()
                sku = date_pattern.sub("", sku).strip()
                sku = re.sub(r"\s+\d+\s+\d+\s*$", "", sku).strip()
                sku = re.sub(r"\s+\d+\s*$", "", sku).strip()

                if sku:
                    sku_info = sku[:120]
                    break

        for line in cleaned_lines:
            if user_name and line == user_name:
                continue

            if date and date in line and "已购" in line:
                continue

            if "已购" in line:
                continue

            if date_pattern.fullmatch(line):
                continue

            if line in ["0", "1", "2", "3", "4", "5"]:
                continue

            if any(x in line for x in ["默认排序", "款式筛选", "图/视频", "为你展示真实评价"]):
                continue

            line = date_pattern.sub("", line).strip()

            if user_name and line.startswith(user_name):
                line = line[len(user_name):].strip()

            line = re.sub(
                r"^(88VIP|VIP|V\d+|蓝钻|红钻|黄钻)\s*",
                "",
                line,
                flags=re.I,
            ).strip()

            line = re.sub(r"\s+\d+\s+\d+\s*$", "", line).strip()

            if len(line) < 2:
                continue

            if line == user_name:
                continue

            content_parts.append(line)

        content = " ".join(content_parts)
        content = re.sub(r"\s+", " ", content).strip()

        if not content and full_text:
            content = self._extract_content(full_text)

        if user_name and content.startswith(user_name):
            content = content[len(user_name):].strip()

        content = content[:500]

        for line in cleaned_lines:
            m = re.search(r"点赞\s*(\d+)", line)
            if m:
                try:
                    like_count = int(m.group(1))
                except Exception:
                    pass

        return {
            "user_name": user_name,
            "date": date,
            "sku_info": sku_info,
            "content": content,
            "like_count": like_count,
            "lines": cleaned_lines,
        }

    def _extract_content(self, text: str) -> str:
        if not text:
            return ""

        content = text

        content = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", " ", content)
        content = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", content)
        content = re.sub(r"已购[:：]?\s*[^。；;\n]+", " ", content)
        content = re.sub(r"规格[:：]?\s*[^。；;\n]+", " ", content)

        remove_words = [
            "为你展示真实评价",
            "默认排序",
            "款式筛选",
            "图集",
            "全部",
            "图/视频",
            "追评",
        ]

        for word in remove_words:
            content = content.replace(word, " ")

        content = re.sub(r"\s+", " ", content)

        return content.strip()[:500]

    def _normalize_date(self, date: str) -> str:
        date = str(date or "").strip()

        if not date:
            return ""

        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date):
            parts = date.split("-")

            try:
                return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"
            except Exception:
                return date

        return date

    def _guess_video_ext(self, url: str) -> str:
        lower = (url or "").lower()

        for ext in ["mp4", "mov", "m4v", "webm"]:
            if f".{ext}" in lower:
                return ext

        if ".m3u8" in lower:
            return "m3u8"

        return "mp4"

    # ------------------------------------------------------------------
    # 有效性 / 去重
    # ------------------------------------------------------------------

    def _is_valid_review_item(self, review: ReviewItem) -> bool:
        if not review.images and not review.videos:
            return False

        text = " ".join(
            [
                review.user_name or "",
                review.date or "",
                review.sku_info or "",
                review.content or "",
            ]
        )

        if review.date:
            return True

        signals = [
            "已购",
            "规格",
            "颜色",
            "尺码",
            "追评",
            "商家回复",
            "好评",
            "中评",
            "差评",
        ]

        if any(s in text for s in signals):
            return True

        if len(review.content or "") < 8:
            return False

        return True

    def _build_review_key(self, item: ReviewItem) -> str:
        media_part = "|".join(
            [m.url for m in item.images] +
            [m.url for m in item.videos]
        )

        if media_part:
            return media_part

        return "|".join(
            [
                item.user_name or "",
                item.date or "",
                item.sku_info or "",
                item.content or "",
            ]
        )

    # ------------------------------------------------------------------
    # 调试
    # ------------------------------------------------------------------

    def _save_debug_page(self, page, name: str):
        try:
            debug_dir = Path("debug") / "taobao_review"
            debug_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")

            html_path = debug_dir / f"{name}_{timestamp}.html"
            png_path = debug_dir / f"{name}_{timestamp}.png"

            html_path.write_text(page.content(), encoding="utf-8")

            try:
                page.screenshot(path=str(png_path), full_page=True)
            except Exception:
                pass

            self.log(f"评价调试 HTML 已保存：{html_path}")
            self.log(f"评价调试截图已保存：{png_path}")

        except Exception as e:
            self.log(f"保存评价调试页面失败：{e}")

    def _log_review_samples(self, reviews: list[ReviewItem]):
        try:
            for i, review in enumerate(reviews[:3], start=1):
                self.log(
                    f"评价样例{i}：用户={review.user_name or '-'}，"
                    f"日期={review.date or '-'}，"
                    f"SKU={review.sku_info[:30] if review.sku_info else '-'}，"
                    f"内容={review.content[:50] if review.content else '-'}，"
                    f"图片={len(review.images)}，视频={len(review.videos)}"
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 登录检测
    # ------------------------------------------------------------------

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
