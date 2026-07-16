import re
import html as html_lib
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class Alibaba1688Parser(BaseParser):
    """
    1688 商品解析器。

    优化重点：
    1. 修复标题乱码问题；
    2. 避免把公司名/店铺名当成商品标题；
    3. 主图只从商品主图/相册相关区域提取；
    4. 详情图优先从 descUrl 接口提取；
    5. SKU 图只从 SKU 相关字段提取；
    6. 严格过滤 7天、48小时、锁图标、播放按钮、店铺图标、服务保障图标等；
    7. 主图、详情图、SKU 图之间去重。
    """

    def __init__(
        self,
        log_callback=None,
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
        self.log_callback = log_callback
        self.browser = BrowserClient(
            headless=headless,
            login_wait_seconds=login_wait_seconds,
            log_callback=log_callback,
        )

    def parse(self, url: str) -> ProductData:
        platform, product_id = PlatformDetector.detect(url)

        self._log("正在打开 1688 商品页面...")
        html = self.browser.open_page(url)

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        title = self._parse_title(soup, html)
        if not title:
            title = f"1688商品_{product_id or 'unknown'}"

        self._log("正在解析 1688 主图...")
        main_urls = self._parse_main_image_urls(html, soup)

        self._log("正在解析 1688 SKU 图...")
        sku_urls = self._parse_sku_image_urls(html)

        self._log("正在解析 1688 详情图...")
        detail_urls = self._parse_detail_image_urls(html, url)

        # 规范化 + 去重
        main_urls = self._dedupe_keep_order(main_urls)
        sku_urls = self._dedupe_keep_order(sku_urls)
        detail_urls = self._dedupe_keep_order(detail_urls)

        # 类型之间去重
        main_set = set(self._image_dedupe_key(u) for u in main_urls)
        sku_set = set(self._image_dedupe_key(u) for u in sku_urls)

        detail_urls = [
            u for u in detail_urls
            if self._image_dedupe_key(u) not in main_set
            and self._image_dedupe_key(u) not in sku_set
        ]

        # 主图里排除 SKU 图
        sku_set = set(self._image_dedupe_key(u) for u in sku_urls)
        main_urls = [
            u for u in main_urls
            if self._image_dedupe_key(u) not in sku_set
        ]

        # 数量限制，防止异常页面污染
        main_urls = main_urls[:12]
        detail_urls = detail_urls[:100]
        sku_urls = sku_urls[:60]

        main_images = self._build_images(main_urls, "main", "1688_main")
        detail_images = self._build_images(detail_urls, "detail", "1688_detail")
        sku_images = self._build_images(sku_urls, "sku", "1688_sku")

        self._log(f"1688商品标题：{title}")
        self._log(f"1688主图识别：{len(main_images)} 张")
        self._log(f"1688详情图识别：{len(detail_images)} 张")
        self._log(f"1688SKU图识别：{len(sku_images)} 张")

        return ProductData(
            platform=platform,
            product_id=product_id,
            title=title,
            url=url,
            main_images=main_images,
            detail_images=detail_images,
            sku_images=sku_images,
        )

    # ------------------------------------------------------------------
    # 标题解析
    # ------------------------------------------------------------------

    def _parse_title(self, soup: BeautifulSoup, html: str) -> str:
        """
        解析商品标题。

        1688 页面里经常有 companyName / shopName / sellerName，
        这些不是商品标题，需要排除。
        """

        # 1. 优先 meta 标题
        meta_selectors = [
            ("meta", {"property": "og:title"}),
            ("meta", {"name": "og:title"}),
            ("meta", {"name": "title"}),
            ("meta", {"itemprop": "name"}),
        ]

        for name, attrs in meta_selectors:
            tag = soup.find(name, attrs=attrs)
            if tag:
                title = tag.get("content", "").strip()
                title = self._clean_title(title)
                if self._is_valid_title(title):
                    return title

        # 2. h1 / 商品标题区域
        selectors = [
            "h1",
            ".offer-title",
            ".mod-detail-title",
            ".detail-title",
            ".title-text",
            "[class*=offer-title]",
            "[class*=detail-title]",
            "[class*=title-text]",
        ]

        for selector in selectors:
            try:
                tag = soup.select_one(selector)
            except Exception:
                tag = None

            if tag:
                title = tag.get_text(" ", strip=True)
                title = self._clean_title(title)
                if self._is_valid_title(title):
                    return title

        text = self._decode_text(html)

        # 3. JSON 字段
        patterns = [
            r'"subject"\s*:\s*"([^"]{3,300})"',
            r'"offerTitle"\s*:\s*"([^"]{3,300})"',
            r'"offer_title"\s*:\s*"([^"]{3,300})"',
            r'"productTitle"\s*:\s*"([^"]{3,300})"',
            r'"product_title"\s*:\s*"([^"]{3,300})"',
            r'"title"\s*:\s*"([^"]{3,300})"',
            r"'subject'\s*:\s*'([^']{3,300})'",
            r"'offerTitle'\s*:\s*'([^']{3,300})'",
            r"'title'\s*:\s*'([^']{3,300})'",
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                start = max(0, m.start() - 120)
                prefix = text[start:m.start()].lower()

                # 排除公司、店铺、卖家上下文
                if any(k in prefix for k in [
                    "company",
                    "companyname",
                    "shop",
                    "shopname",
                    "seller",
                    "sellername",
                    "supplier",
                    "suppliername",
                    "store",
                    "storename",
                    "corp",
                ]):
                    continue

                title = self._clean_title(m.group(1))
                if self._is_valid_title(title):
                    return title

        # 4. document title 兜底
        if soup.title:
            title = soup.title.get_text(" ", strip=True)
            title = self._clean_title(title)
            if self._is_valid_title(title):
                return title

        return ""

    def _clean_title(self, title: str) -> str:
        if not title:
            return ""

        title = self._decode_text(title)
        title = re.sub(r"\s+", " ", title).strip()

        remove_parts = [
            "- 阿里巴巴",
            "_阿里巴巴",
            "- 1688.com",
            "_1688.com",
            "- 1688",
            "_1688",
            "阿里巴巴",
        ]

        for part in remove_parts:
            title = title.replace(part, "")

        title = title.strip(" -_|,，。")

        return title

    def _is_valid_title(self, title: str) -> bool:
        if not title:
            return False

        if len(title) < 3:
            return False

        bad_words = [
            "有限公司",
            "公司首页",
            "诚信通",
            "旺铺",
            "供应商",
            "企业介绍",
            "公司介绍",
            "联系方式",
            "店铺首页",
            "实力商家",
            "工厂档案",
            "经营模式",
        ]

        # 短文本里出现这些词，大概率是公司名/店铺名
        if any(w in title for w in bad_words) and len(title) <= 60:
            return False

        bad_exact = [
            "1688",
            "阿里巴巴",
            "商品详情",
            "产品详情",
            "店铺首页",
        ]

        if title in bad_exact:
            return False

        return True

    # ------------------------------------------------------------------
    # 主图解析
    # ------------------------------------------------------------------

    def _parse_main_image_urls(self, html: str, soup: BeautifulSoup) -> list[str]:
        """
        解析 1688 主图。

        主图只从商品主图/相册相关字段和主图容器中取。
        不从全页面扫图，避免混入详情图、服务图标。
        """
        text = self._decode_text(html)

        urls: list[str] = []

        main_keys = [
            "offerImgList",
            "offerImageList",
            "mainImageList",
            "mainImages",
            "main_images",
            "albumImages",
            "albumImageList",
            "productImageList",
            "productImages",
            "imageList",
            "image_list",
        ]

        for key in main_keys:
            for block in self._extract_json_like_blocks_by_key(text, key, max_len=6000):
                block_urls = self._extract_image_urls_from_text(block)

                for u in block_urls:
                    if not self._is_likely_product_image(u, image_type="main"):
                        continue

                    if self._is_service_or_ui_context(block, u):
                        continue

                    urls.append(u)

        # 只从疑似主图容器取图
        container_selectors = [
            "[class*=main-image]",
            "[class*=mainImage]",
            "[class*=image-list]",
            "[class*=imageList]",
            "[class*=album]",
            "[class*=gallery]",
            "[class*=preview]",
            "[class*=magnifier]",
            "[class*=vertical-img]",
        ]

        for selector in container_selectors:
            try:
                nodes = soup.select(selector)
            except Exception:
                nodes = []

            for node in nodes[:8]:
                node_text = str(node)
                for img in node.find_all("img"):
                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-img",
                        "data-url",
                    ]:
                        value = img.get(attr)
                        if not value:
                            continue

                        if self._is_service_or_ui_context(node_text, value):
                            continue

                        urls.append(value)

        urls = self._normalize_urls(urls)

        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="main")
        ]

        urls = self._dedupe_keep_order(urls)

        return urls[:12]

    # ------------------------------------------------------------------
    # SKU 图解析
    # ------------------------------------------------------------------

    def _parse_sku_image_urls(self, html: str) -> list[str]:
        """
        解析 1688 SKU 图。

        只从 SKU 图片相关字段取图。
        避免从 saleProps / skuMap 这类宽泛字段误抓服务图标。
        """
        text = self._decode_text(html)

        urls: list[str] = []

        sku_keys = [
            "skuProps",
            "sku_props",
            "skuModel",
            "sku_model",
            "skuImages",
            "sku_images",
            "propertyPics",
            "property_pics",
            "skuPictures",
            "sku_pictures",
            "skuImageMap",
            "sku_image_map",
        ]

        for key in sku_keys:
            for block in self._extract_json_like_blocks_by_key(text, key, max_len=12000):
                block_urls = self._extract_image_urls_from_text(block)

                for u in block_urls:
                    if not self._is_likely_product_image(u, image_type="sku"):
                        continue

                    if self._is_service_or_ui_context(block, u):
                        continue

                    urls.append(u)

        urls = self._normalize_urls(urls)

        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="sku")
        ]

        urls = self._dedupe_keep_order(urls)

        return urls[:60]

    # ------------------------------------------------------------------
    # 详情图解析
    # ------------------------------------------------------------------

    def _parse_detail_image_urls(self, html: str, page_url: str) -> list[str]:
        """
        解析详情图。

        优先请求 descUrl 接口。
        如果 descUrl 不存在，再从详情相关 JSON 块里提取。
        """
        text = self._decode_text(html)

        urls: list[str] = []

        desc_urls = self._extract_desc_urls(text, page_url)

        for desc_url in desc_urls:
            self._log(f"尝试请求 1688 详情接口：{desc_url}")

            try:
                detail_html = self._request_text(desc_url)
            except Exception as e:
                self._log(f"1688详情接口请求失败：{e}")
                continue

            if not detail_html:
                continue

            detail_urls = self._extract_detail_images_from_desc_html(detail_html)
            urls.extend(detail_urls)

        # 如果 descUrl 没取到，再尝试详情相关 JSON 块
        if not urls:
            detail_keys = [
                "descUrl",
                "description",
                "offerDetail",
                "detailContent",
                "detailImages",
                "detail_images",
                "productDetail",
                "richText",
            ]

            for key in detail_keys:
                for block in self._extract_json_like_blocks_by_key(text, key, max_len=30000):
                    block_urls = self._extract_image_urls_from_text(block)

                    for u in block_urls:
                        if not self._is_likely_product_image(u, image_type="detail"):
                            continue

                        if self._is_service_or_ui_context(block, u):
                            continue

                        urls.append(u)

        urls = self._normalize_urls(urls)

        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="detail")
        ]

        urls = self._dedupe_keep_order(urls)

        return urls[:100]

    def _extract_desc_urls(self, text: str, page_url: str) -> list[str]:
        urls = []

        patterns = [
            r'"descUrl"\s*:\s*"([^"]+)"',
            r"'descUrl'\s*:\s*'([^']+)'",
            r'"detailUrl"\s*:\s*"([^"]+)"',
            r"'detailUrl'\s*:\s*'([^']+)'",
            r'((?:https?:)?//[^"\']+/offer/desc/[^"\']+)',
            r'((?:https?:)?//[^"\']+desc[^"\']+offer[^"\']+)',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                raw = m.group(1)
                if not raw:
                    continue

                raw = self._decode_text(raw)
                raw = raw.strip().strip('"').strip("'")

                if not raw:
                    continue

                if raw.startswith("//"):
                    raw = "https:" + raw
                elif raw.startswith("/"):
                    raw = urljoin(page_url, raw)

                if raw.startswith("http") and raw not in urls:
                    urls.append(raw)

        return urls[:5]

    def _extract_detail_images_from_desc_html(self, detail_html: str) -> list[str]:
        text = self._decode_text(detail_html)

        content_candidates = []

        # desc 接口常见返回 content 字段
        patterns = [
            r'"content"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'content'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
            r'"desc"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'desc'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                content_candidates.append(m.group(1))

        if not content_candidates:
            content_candidates.append(text)

        urls = []

        for content in content_candidates:
            content = self._decode_text(content)

            try:
                soup = BeautifulSoup(content, "lxml")
            except Exception:
                soup = BeautifulSoup(content, "html.parser")

            for img in soup.find_all("img"):
                for attr in [
                    "src",
                    "data-src",
                    "data-original",
                    "data-lazy-src",
                    "data-img",
                    "data-url",
                ]:
                    value = img.get(attr)
                    if value:
                        urls.append(value)

            # 有些详情内容是转义字符串，再正则扫一次
            urls.extend(self._extract_image_urls_from_text(content))

        urls = self._normalize_urls(urls)

        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="detail")
        ]

        return self._dedupe_keep_order(urls)

    # ------------------------------------------------------------------
    # JSON / 文本提取工具
    # ------------------------------------------------------------------

    def _extract_json_like_blocks_by_key(
        self,
        text: str,
        key: str,
        max_len: int = 10000,
    ) -> list[str]:
        """
        按 key 提取附近文本块。

        不尝试完整解析整页 JS，因为 1688 页面 JS 结构经常变化。
        """
        blocks = []

        key_patterns = [
            f'"{key}"',
            f"'{key}'",
            key,
        ]

        for key_pattern in key_patterns:
            start = 0

            while True:
                idx = text.find(key_pattern, start)
                if idx < 0:
                    break

                left = max(0, idx - 800)
                right = min(len(text), idx + max_len)
                block = text[left:right]

                if block and block not in blocks:
                    blocks.append(block)

                start = idx + len(key_pattern)

                if len(blocks) >= 30:
                    break

        return blocks

    def _extract_image_urls_from_text(self, text: str) -> list[str]:
        """
        从文本中提取图片 URL。
        """
        if not text:
            return []

        text = self._decode_text(text)

        urls = []

        patterns = [
            r'(?:https?:)?//[^"\'\s<>\\]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s<>\\]*)?',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.jpg_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.jpeg_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.png_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.webp_[^"\'\s<>\\]+',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                urls.append(m.group(0))

        return urls

    # ------------------------------------------------------------------
    # URL 标准化 / 过滤
    # ------------------------------------------------------------------

    def _normalize_urls(self, urls: list[str]) -> list[str]:
        result = []

        for url in urls:
            url = self._normalize_image_url(url)
            if url:
                result.append(url)

        return self._dedupe_keep_order(result)

    def _normalize_image_url(self, url: str) -> str:
        if not url:
            return ""

        url = self._decode_text(url)
        url = url.strip().strip('"').strip("'").strip()

        if not url:
            return ""

        if url.startswith("//"):
            url = "https:" + url

        if not url.startswith("http"):
            return ""

        # 去掉尾部非法字符
        url = url.rstrip("\\")
        url = url.rstrip(",")
        url = url.rstrip(";")
        url = url.rstrip(")")
        url = url.rstrip("]")
        url = url.rstrip("}")

        # 还原阿里图片缩略图后缀
        # xxx.jpg_300x300.jpg -> xxx.jpg
        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?\.(?:jpg|jpeg|png|webp)$',
            r'\1',
            url,
            flags=re.I,
        )

        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?$',
            r'\1',
            url,
            flags=re.I,
        )

        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+.*$',
            r'\1',
            url,
            flags=re.I,
        )

        # 去掉 OSS 处理参数
        url = re.sub(r'([?&])x-oss-process=image/resize[^&]*', r'\1', url)

        try:
            url = normalize_image_url(url)
        except Exception:
            pass

        return url

    def _is_likely_product_image(self, url: str, image_type: str = "") -> bool:
        """
        判断是否像商品图片。

        这里是 1688 解析准确率的关键。
        """
        if not url:
            return False

        u = url.lower()

        if not u.startswith("http"):
            return False

        # 必须是图片 URL
        if not re.search(r'\.(jpg|jpeg|png|webp)(?:$|\?|_)', u, re.I):
            return False

        # 常见非商品图关键词
        bad_keywords = [
            "icon",
            "logo",
            "avatar",
            "qrcode",
            "qr_code",
            "qr-",
            "loading",
            "placeholder",
            "default",
            "sprite",
            "button",
            "btn",
            "play",
            "video",
            "collect",
            "favorite",
            "share",
            "service",
            "security",
            "guarantee",
            "credit",
            "member",
            "shop",
            "seller",
            "wangwang",
            "aliww",
            "favicon",
            "transparent",
            "blank",
            "empty",
            "grey",
            "gray",
            "arrow",
            "close",
            "search",
            "cart",
            "login",
            "rank",
            "medal",
            "badge",
            "coupon",
            "discount",
            "activity",
            "promotion",
            "insurance",
            "promise",
            "protect",
            "safe",
            "seal",
            "cert",
            "certificate",
            "license",
            "company",
            "store",
            "factory",
            "supplier",
            "contact",
            "phone",
            "tel",
            "map",
            "location",
            "auth",
            "authen",
            "weixin",
            "wechat",
            "alipay",
            "taobao",
            "tmall",
            "return",
            "refund",
            "7day",
            "seven",
            "48h",
            "48hour",
            "assurance",
            "buyer",
            "trade",
            "chengxin",
            "chengxintong",
        ]

        if any(k in u for k in bad_keywords):
            return False

        # 过滤明显小尺寸 URL
        small_patterns = [
            "12x12",
            "16x16",
            "20x20",
            "24x24",
            "30x30",
            "32x32",
            "36x36",
            "40x40",
            "48x48",
            "50x50",
            "60x60",
            "64x64",
            "70x70",
            "72x72",
            "80x80",
            "88x88",
            "90x90",
            "100x100",
            "110x110",
            "120x120",
        ]

        if any(p in u for p in small_patterns):
            return False

        # 阿里 UI 素材路径，很多不是商品图
        bad_path_keywords = [
            "/tps/",
            "/tfs/",
            "/favicon",
        ]

        if any(k in u for k in bad_path_keywords):
            return False

        # 1688 商品图通常在这些域/路径
        good_keywords = [
            "cbu01.alicdn.com/img/ibank",
            "cbu01.alicdn.com/img",
            "cbu01.alicdn.com/",
            "img.alicdn.com/imgextra",
            "img.alicdn.com/bao/uploaded",
            "alicdn.com",
        ]

        if not any(k in u for k in good_keywords):
            return False

        return True

    def _is_service_or_ui_context(self, text: str, url: str) -> bool:
        """
        根据 URL 附近上下文过滤服务图标 / UI 图标。

        1688 的 7天、48小时、保障服务图标，有时 URL 本身也像商品图，
        所以需要结合上下文判断。
        """
        if not text or not url:
            return False

        lower_text = text.lower()
        lower_url = url.lower()

        pos = lower_text.find(lower_url)

        if pos < 0:
            ctx = lower_url
        else:
            start = max(0, pos - 300)
            end = min(len(lower_text), pos + len(lower_url) + 300)
            ctx = lower_text[start:end]

        bad_context_words = [
            "icon",
            "logo",
            "sprite",
            "button",
            "btn",
            "play",
            "video",
            "avatar",
            "qrcode",
            "qr",
            "shop",
            "seller",
            "company",
            "store",
            "supplier",
            "service",
            "guarantee",
            "promise",
            "protect",
            "security",
            "credit",
            "auth",
            "cert",
            "certificate",
            "license",
            "return",
            "refund",
            "7day",
            "seven",
            "48h",
            "48hour",
            "insurance",
            "trade",
            "buyer",
            "coupon",
            "discount",
            "activity",
            "promotion",
            "wangwang",
            "aliww",
            "favorite",
            "collect",
            "share",
            "cart",
        ]

        if any(w in ctx for w in bad_context_words):
            return True

        bad_cn_words = [
            "七天",
            "7天",
            "退货",
            "退款",
            "包退",
            "包换",
            "48小时",
            "保障",
            "买家保障",
            "交易保障",
            "服务",
            "诚信通",
            "实力商家",
            "认证",
            "证书",
            "店铺",
            "公司",
            "供应商",
            "收藏",
            "分享",
        ]

        if any(w in ctx for w in bad_cn_words):
            return True

        return False

    # ------------------------------------------------------------------
    # 解码 / 去重
    # ------------------------------------------------------------------

    def _decode_text(self, text: str) -> str:
        """
        安全解码文本。

        不能对整段 HTML 执行：
            text.encode("utf-8").decode("unicode_escape")

        否则正常中文会被解坏，出现 å¥³ 这种乱码。
        """
        if not text:
            return ""

        try:
            text = html_lib.unescape(text)
        except Exception:
            pass

        text = text.replace("\\/", "/")
        text = text.replace("\\u002F", "/")
        text = text.replace("\\u002f", "/")
        text = text.replace("&amp;", "&")

        # 只替换标准 unicode 转义，不破坏正常中文
        def replace_unicode(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)

        text = re.sub(r"\\u([0-9a-fA-F]{4})", replace_unicode, text)

        return text

    def _dedupe_keep_order(self, urls: list[str]) -> list[str]:
        result = []
        seen = set()

        for url in urls:
            if not url:
                continue

            key = self._image_dedupe_key(url)

            if key in seen:
                continue

            seen.add(key)
            result.append(url)

        try:
            result = dedupe_urls(result)
        except Exception:
            pass

        return result

    def _image_dedupe_key(self, url: str) -> str:
        """
        生成图片去重 key。

        1688 同一张图经常有：
            xxx.jpg
            xxx.jpg_60x60.jpg
            xxx.jpg_300x300.jpg
            xxx.jpg_460x460q90.jpg
        """
        if not url:
            return ""

        u = url.lower().strip()

        # 去查询参数
        u = u.split("?")[0]

        # 去阿里图片缩略图后缀
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?\.(?:jpg|jpeg|png|webp)$",
            r"\1",
            u,
            flags=re.I,
        )

        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?$",
            r"\1",
            u,
            flags=re.I,
        )

        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+.*$",
            r"\1",
            u,
            flags=re.I,
        )

        return u

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    def _request_text(self, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://detail.1688.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # ImageItem 构造
    # ------------------------------------------------------------------

    def _build_images(
        self,
        urls: list[str],
        image_type: str,
        source: str,
    ) -> list[ImageItem]:
        images = []

        for index, url in enumerate(urls, start=1):
            ext = ""

            try:
                ext = get_url_ext(url)
            except Exception:
                ext = ""

            if not ext:
                ext = ".jpg"

            name = f"{index:03d}{ext}"

            images.append(
                ImageItem(
                    url=url,
                    image_type=image_type,
                    name=name,
                    ext=ext,
                    sku_name=None,
                    source=source,
                )
            )

        return images

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
