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

    当前策略：
    1. 商品标题：优先商品标题字段，避免取公司名/店铺名；
    2. 主图：优先从页面左侧主图/缩略图 DOM 区域提取；
    3. 详情图：优先 descUrl 接口，兜底只取详情相关字段；
    4. SKU 图：从 SKU DOM / background-image / SKU JSON 中提取；
    5. 过滤服务图标、7天、48小时、店铺图标、UI 图标等。
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

        # 主图候选集：用于后面从详情图中排除主图。
        # 注意：这里直接从 DOM 拿一份候选，不代表最终主图全部使用这些。
        main_gallery_candidates = self._parse_main_images_from_dom(soup)
        main_urls = self._parse_main_image_urls(html, soup)

        self._log("正在解析 1688 SKU 图...")
        sku_urls = self._parse_sku_image_urls(html, soup)

        self._log("正在解析 1688 详情图...")
        detail_urls = self._parse_detail_image_urls(html, url)

        # 规范化 + 去重
        main_urls = self._dedupe_keep_order(main_urls)
        sku_urls = self._dedupe_keep_order(sku_urls)
        detail_urls = self._dedupe_keep_order(detail_urls)

        # 详情图排除主图与 SKU 图。
        # 这里不仅排除最终 main_urls，也排除 main_gallery_candidates，
        # 避免左侧主图缩略图被详情图兜底逻辑再次收进去。
        main_exclude_urls = []
        main_exclude_urls.extend(main_urls)
        main_exclude_urls.extend(main_gallery_candidates)

        main_set = set(self._image_dedupe_key(u) for u in main_exclude_urls)
        sku_set = set(self._image_dedupe_key(u) for u in sku_urls)

        detail_urls = [
            u for u in detail_urls
            if self._image_dedupe_key(u) not in main_set
            and self._image_dedupe_key(u) not in sku_set
        ]

        # 不从主图里排除 SKU 图。
        # 1688 很多商品 SKU 图本身就是主图之一。

        main_urls = main_urls[:8]
        detail_urls = detail_urls[:120]
        sku_urls = sku_urls[:40]

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

        优先采集页面左侧缩略图区域。

        注意：
            1688 左侧主图本身可能包含营销风格图，
            例如快充图、新品图、功能图。
            只要它出现在左侧主图缩略图区域，就应该算主图。
        """
        urls: list[str] = []

        # 1. 优先 DOM 主图区域
        dom_urls = self._parse_main_images_from_dom(soup)

        if dom_urls:
            dom_urls = self._normalize_urls(dom_urls)
            dom_urls = [
                u for u in dom_urls
                if self._is_likely_product_image(u, image_type="main")
            ]
            dom_urls = self._dedupe_keep_order(dom_urls)
            urls.extend(dom_urls)

        # 2. 如果 DOM 主图不足，再从严格 JSON 字段补充
        if len(urls) < 6:
            text = self._decode_text(html)

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
            ]

            for key in main_keys:
                for block in self._extract_json_like_blocks_by_key(text, key, max_len=5000):
                    block_urls = self._extract_image_urls_from_text(block)

                    for u in block_urls:
                        if not self._is_likely_product_image(u, image_type="main"):
                            continue

                        if self._is_service_or_ui_context(block, u):
                            continue

                        urls.append(u)

        urls = self._normalize_urls(urls)

        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="main")
        ]

        urls = self._dedupe_keep_order(urls)

        # 1688 左侧主图一般 5~8 张，包含视频封面/参数图时可能更多。
        return urls[:8]

    def _parse_main_images_from_dom(self, soup: BeautifulSoup) -> list[str]:
        """
        从页面左侧主图/缩略图 DOM 区域提取主图。

        这里不要过滤所谓“营销图”，因为 1688 左侧主图区域本身
        经常包含营销风格主图。
        """
        urls: list[str] = []

        selectors = [
            "[class*=detail-gallery]",
            "[class*=gallery]",
            "[class*=album]",
            "[class*=main-image]",
            "[class*=mainImage]",
            "[class*=image-list]",
            "[class*=imageList]",
            "[class*=preview]",
            "[class*=magnifier]",
            "[class*=vertical-img]",
            "[class*=verticalImg]",
            "[class*=thumb]",
            "[class*=thumbnail]",
        ]

        for selector in selectors:
            try:
                nodes = soup.select(selector)
            except Exception:
                nodes = []

            for node in nodes[:15]:
                node_text = str(node)
                lower_node_text = node_text.lower()

                # 排除明显详情/店铺/服务区域。
                # 但如果同时明显是主图容器，不排除。
                if any(k in lower_node_text for k in [
                    "description",
                    "detail-content",
                    "rich-text",
                    "service",
                    "guarantee",
                    "shop",
                    "seller",
                    "company",
                ]):
                    if not any(k in lower_node_text for k in [
                        "gallery",
                        "album",
                        "main-image",
                        "mainimage",
                        "preview",
                        "magnifier",
                        "thumb",
                        "thumbnail",
                        "image-list",
                        "imagelist",
                    ]):
                        continue

                # 1. img 标签
                for img in node.find_all("img"):
                    width = img.get("width") or ""
                    height = img.get("height") or ""

                    try:
                        w = int(re.sub(r"\D", "", str(width)) or "0")
                        h = int(re.sub(r"\D", "", str(height)) or "0")
                        if w and h and (w < 35 or h < 35):
                            continue
                    except Exception:
                        pass

                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-img",
                        "data-url",
                        "data-lazyload",
                    ]:
                        value = img.get(attr)
                        if not value:
                            continue

                        if self._is_service_or_ui_context(node_text, value):
                            continue

                        urls.append(value)

                # 2. background-image，部分主图缩略图可能是背景图
                style_urls = self._extract_background_image_urls(node_text)
                for value in style_urls:
                    if not value:
                        continue

                    if self._is_service_or_ui_context(node_text, value):
                        continue

                    urls.append(value)

        return urls

    # ------------------------------------------------------------------
    # SKU 图解析
    # ------------------------------------------------------------------

    def _parse_sku_image_urls(self, html: str, soup: BeautifulSoup) -> list[str]:
        """
        解析 1688 SKU 图。

        当前策略：
        1. 优先从页面 SKU DOM 区域提取；
        2. 支持 background-image；
        3. 再从 SKU JSON 字段兜底。
        """
        urls: list[str] = []

        dom_urls = self._parse_sku_images_from_dom(soup)
        if dom_urls:
            urls.extend(dom_urls)

        text = self._decode_text(html)

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

                    if self._is_detail_marketing_context(block, u):
                        continue

                    urls.append(u)

        urls = self._normalize_urls(urls)
        urls = [
            u for u in urls
            if self._is_likely_product_image(u, image_type="sku")
        ]
        urls = self._dedupe_keep_order(urls)

        return urls[:40]

    def _parse_sku_images_from_dom(self, soup: BeautifulSoup) -> list[str]:
        """
        从页面 SKU 区域提取 SKU 图。

        兼容：
        1. img 标签；
        2. background-image 背景图；
        3. 型号 C4 这类单规格商品。
        """
        urls: list[str] = []

        selectors = [
            "[class*=sku]",
            "[class*=Sku]",
            "[class*=sale-prop]",
            "[class*=saleProp]",
            "[class*=prop-item]",
            "[class*=propItem]",
            "[class*=spec]",
            "[class*=model]",
            "[class*=offer-attr]",
            "[class*=attribute]",
        ]

        for selector in selectors:
            try:
                nodes = soup.select(selector)
            except Exception:
                nodes = []

            for node in nodes[:50]:
                node_text = str(node)
                plain_text = node.get_text(" ", strip=True)

                if not self._looks_like_sku_node(plain_text, node_text):
                    continue

                # 1. 提取 background-image
                style_urls = self._extract_background_image_urls(node_text)
                for value in style_urls:
                    if not value:
                        continue

                    if self._is_service_or_ui_context(node_text, value):
                        continue

                    if self._is_detail_marketing_context(node_text, value):
                        continue

                    urls.append(value)

                # 2. 提取 img
                for img in node.find_all("img"):
                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-img",
                        "data-url",
                        "data-lazyload",
                    ]:
                        value = img.get(attr)
                        if not value:
                            continue

                        if self._is_service_or_ui_context(node_text, value):
                            continue

                        if self._is_detail_marketing_context(node_text, value):
                            continue

                        urls.append(value)

        return urls

    def _looks_like_sku_node(self, plain_text: str, html_text: str) -> bool:
        """
        判断 DOM 节点是否像 SKU 区域。
        """
        text = f"{plain_text} {html_text}".lower()

        good_words = [
            "sku",
            "规格",
            "型号",
            "颜色",
            "尺寸",
            "款式",
            "类型",
            "model",
            "spec",
            "prop",
            "attribute",
            "sale-prop",
            "saleprop",
        ]

        if any(w in text for w in good_words):
            return True

        # 识别类似 C4、A1、X10 这种短型号
        if re.search(r"\b[a-z]\d{1,3}\b", text, re.I):
            return True

        return False

    # ------------------------------------------------------------------
    # 详情图解析
    # ------------------------------------------------------------------

    def _parse_detail_image_urls(self, html: str, page_url: str) -> list[str]:
        """
        解析详情图。

        优先请求 descUrl 接口。
        如果 descUrl 不存在，再从严格详情相关 JSON 块里提取。

        注意：
            不再使用 description / offerDetail / productDetail 等过宽字段，
            避免把主图、SKU 图混进详情图。
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

        # 兜底只保留严格详情字段，避免详情图数量异常增多。
        if not urls:
            detail_keys = [
                "detailContent",
                "detailImages",
                "detail_images",
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

        return urls[:120]

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

            # 详情内容可能是转义字符串，再正则扫一次
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

    def _extract_background_image_urls(self, text: str) -> list[str]:
        """
        提取 style="background-image:url(...)" 里的图片。
        1688 SKU 小图、主图缩略图有时不是 img 标签，而是背景图。
        """
        if not text:
            return []

        text = self._decode_text(text)

        urls = []

        patterns = [
            r'background-image\s*:\s*url\(["\']?([^"\')]+)["\']?\)',
            r'background\s*:\s*url\(["\']?([^"\')]+)["\']?\)',
            r'url\(["\']?((?:https?:)?//[^"\')]+?\.(?:jpg|jpeg|png|webp)[^"\')]*)["\']?\)',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                urls.append(m.group(1))

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

        url = url.rstrip("\\")
        url = url.rstrip(",")
        url = url.rstrip(";")
        url = url.rstrip(")")
        url = url.rstrip("]")
        url = url.rstrip("}")

        # 还原阿里图片缩略图后缀
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

        url = re.sub(r'([?&])x-oss-process=image/resize[^&]*', r'\1', url)

        try:
            url = normalize_image_url(url)
        except Exception:
            pass

        return url

    def _is_likely_product_image(self, url: str, image_type: str = "") -> bool:
        """
        判断是否像商品图片。
        """
        if not url:
            return False

        u = url.lower()

        if not u.startswith("http"):
            return False

        if not re.search(r'\.(jpg|jpeg|png|webp)(?:$|\?|_)', u, re.I):
            return False

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

        bad_path_keywords = [
            "/tps/",
            "/tfs/",
            "/favicon",
        ]

        if any(k in u for k in bad_path_keywords):
            return False

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

    def _is_detail_marketing_context(self, text: str, url: str) -> bool:
        """
        判断 URL 附近上下文是否像详情营销图。
        主要用于 SKU / JSON 主图补充过滤，不用于左侧主图 DOM 过滤。
        """
        if not text or not url:
            return False

        lower_text = text.lower()
        lower_url = url.lower()

        pos = lower_text.find(lower_url)

        if pos < 0:
            ctx = lower_text[:1000]
        else:
            start = max(0, pos - 300)
            end = min(len(lower_text), pos + len(lower_url) + 300)
            ctx = lower_text[start:end]

        bad_words = [
            "detail",
            "description",
            "desc",
            "richtext",
            "rich-text",
            "content",
            "module",
            "营销",
            "详情",
            "卖点",
            "参数",
            "banner",
            "poster",
            "海报",
            "宣传",
        ]

        if any(w in ctx for w in bad_words):
            return True

        return False

    # ------------------------------------------------------------------
    # 解码 / 去重
    # ------------------------------------------------------------------

    def _decode_text(self, text: str) -> str:
        """
        安全解码文本。

        不对整段 HTML 做 unicode_escape，避免中文变成乱码。
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
        """
        if not url:
            return ""

        u = url.lower().strip()
        u = u.split("?")[0]

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
                "Chrome/124.0.0.0 Safari/537.36"
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
