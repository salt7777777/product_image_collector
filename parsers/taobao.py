import re
import json
import html as html_lib

import httpx
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class TaobaoParser(BaseParser):
    """
    淘宝/天猫解析器。

    当前功能：
    1. 解析商品标题；
    2. 解析主图；
    3. 解析 SKU 图；
    4. 解析详情图。

    重点说明：
    淘宝/天猫详情图不能直接全页面抓 img 标签。
    否则会混入：
    - 天猫 Logo
    - 店铺图标
    - 会员图标
    - 备案图标
    - 认证图标
    - 页面装饰图
    - 推荐商品图
    - 广告图

    所以详情图解析策略为：
    1. 优先从 descUrl 详情接口中获取真实商品详情 HTML；
    2. 如果 descUrl 不存在，再从详情区域容器中提取；
    3. 不做整页 img 扫描。
    """

    def __init__(
        self,
        platform: str = "taobao",
        log_callback=None,
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
        """
        :param platform: taobao / tmall，用于隔离登录状态目录。
        :param log_callback: 日志回调函数，用于把解析器内部日志输出到 UI。
        """
        profile_platform = platform if platform in ["taobao", "tmall"] else "taobao"

        self.browser = BrowserClient(
            user_data_dir=f"browser_data/{profile_platform}",
            headless=headless,
            login_wait_seconds=login_wait_seconds,
            log_callback=log_callback,
        )



    def parse(self, url: str) -> ProductData:
        """
        解析淘宝/天猫商品链接。

        :param url: 商品链接
        :return: ProductData
        """
        platform, product_id = PlatformDetector.detect(url)

        html = self.browser.open_page(url)
        soup = BeautifulSoup(html, "lxml")

        title = self._parse_title(soup)
        main_images = self._parse_main_images(html)
        detail_images = self._parse_detail_images(html, soup)
        sku_images = self._parse_sku_images(html)

        return ProductData(
            platform=platform,
            product_id=product_id,
            title=title,
            url=url,
            main_images=main_images,
            detail_images=detail_images,
            sku_images=sku_images,
        )

    # ----------------------------------------------------------------------
    # 标题解析
    # ----------------------------------------------------------------------

    def _parse_title(self, soup: BeautifulSoup) -> str:
        """
        解析商品标题。
        """

        selectors = [
            "h1",
            ".tb-main-title",
            ".ItemHeader--mainTitle",
            ".tb-detail-hd h1",
            ".tb-item-title",
        ]

        for selector in selectors:
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text(strip=True)
                if text:
                    return self._clean_title(text)

        if soup.title:
            return self._clean_title(soup.title.get_text(strip=True))

        return "淘宝商品"

    def _clean_title(self, title: str) -> str:
        """
        清洗商品标题。
        """

        if not title:
            return "淘宝商品"

        title = title.strip()

        remove_parts = [
            "-tmall.com",
            "-淘宝网",
            "-天猫",
            "淘宝网",
            "天猫",
            "tmall.com",
            "taobao.com",
        ]

        for part in remove_parts:
            title = title.replace(part, "")

        title = re.sub(r"\s+", " ", title).strip()

        return title or "淘宝商品"

    # ----------------------------------------------------------------------
    # 主图解析
    # ----------------------------------------------------------------------

    def _parse_main_images(self, html: str) -> list[ImageItem]:
        """
        解析淘宝/天猫商品主图。

        优化点：
        1. 优先使用 auctionImages，这通常是真正的主图轮播；
        2. 不优先使用 images 这种宽泛字段，避免混入详情图/营销图；
        3. 主图限制数量，避免抓太多非主图。
        """

        urls = []

        # ------------------------------------------------------------
        # 1. 优先 auctionImages
        # ------------------------------------------------------------
        priority_patterns = [
            r'"auctionImages"\s*:\s*(\[[^\]]+\])',
            r'"auction_images"\s*:\s*(\[[^\]]+\])',
            r'"mainImages"\s*:\s*(\[[^\]]+\])',
            r'"main_images"\s*:\s*(\[[^\]]+\])',
        ]

        for pattern in priority_patterns:
            for match in re.findall(pattern, html, flags=re.S):
                urls.extend(self._extract_urls_from_json_array(match))

        urls = dedupe_urls(urls)
        urls = self._filter_product_images(urls)

        # ------------------------------------------------------------
        # 2. 如果优先字段没取到，再有限兜底 images
        # ------------------------------------------------------------
        if not urls:
            fallback_patterns = [
                r'"images"\s*:\s*(\[[^\]]+\])',
                r'"picGallery"\s*:\s*(\[[^\]]+\])',
                r'"gallery"\s*:\s*(\[[^\]]+\])',
            ]

            for pattern in fallback_patterns:
                for match in re.findall(pattern, html, flags=re.S):
                    urls.extend(self._extract_urls_from_json_array(match))

            urls = dedupe_urls(urls)
            urls = self._filter_product_images(urls)

        # 主图一般 5~12 张，避免异常字段混入太多
        urls = urls[:12]

        return [
            ImageItem(
                url=u,
                image_type="main",
                ext=get_url_ext(u),
                source="taobao_main",
            )
            for u in urls
        ]
        
        
    def _extract_urls_from_json_array(self, text: str) -> list[str]:
        """
        从 JSON 数组中提取图片 URL。

        兼容：
            ["//xxx.jpg", "..."]
            [{"url":"..."}, {"picUrl":"..."}]
        """
        urls = []

        if not text:
            return urls

        try:
            arr = json.loads(text)

            for item in arr:
                if isinstance(item, str):
                    url = self._clean_js_url(item)
                    url = normalize_image_url(url)
                    if url:
                        urls.append(url)

                elif isinstance(item, dict):
                    for key in [
                        "url",
                        "image",
                        "picUrl",
                        "imgUrl",
                        "imageUrl",
                        "src",
                        "thumbUrl",
                        "hdUrl",
                    ]:
                        value = item.get(key)
                        if value:
                            url = self._clean_js_url(value)
                            url = normalize_image_url(url)
                            if url:
                                urls.append(url)

        except Exception:
            pass

        return urls



    # ----------------------------------------------------------------------
    # 详情图解析
    # ----------------------------------------------------------------------

    def _parse_detail_images(self, html: str, soup: BeautifulSoup) -> list[ImageItem]:
        """
        解析淘宝/天猫详情图。

        重要原则：
        1. 优先从淘宝/天猫 descUrl 详情接口获取真正的商品详情 HTML；
        2. 如果 descUrl 不存在，再从详情区域容器中提取；
        3. 不允许全页面 soup.select("img") 抓图，否则会混入 Logo、图标、认证图、推荐图等。
        """

        urls = []

        # 1. 优先尝试提取 descUrl
        desc_url = self._extract_desc_url(html)

        if desc_url:
            try:
                self._log(f"检测到详情接口：{desc_url}")
                desc_html = self._fetch_desc_html(desc_url)

                if desc_html:
                    urls = self._parse_detail_images_from_desc_html(desc_html)

                    if urls:
                        self._log(f"从详情接口识别详情图：{len(urls)} 张")

            except Exception as e:
                self._log(f"详情接口解析失败，尝试页面容器解析：{e}")

        # 2. 如果 descUrl 没拿到或者没有解析出图，再走详情容器兜底
        if not urls:
            urls = self._parse_detail_images_from_containers(soup)
            self._log(f"从页面详情容器识别详情图：{len(urls)} 张")

        # 3. 去重 + 精准过滤
        urls = dedupe_urls(urls)
        urls = self._filter_detail_images(urls)

        return [
            ImageItem(
                url=u,
                image_type="detail",
                ext=get_url_ext(u),
                source="taobao_detail",
            )
            for u in urls
        ]

    def _extract_desc_url(self, html: str) -> str:
        """
        从淘宝/天猫页面中提取详情接口 descUrl。

        兼容：
            "descUrl":"//..."
            "httpsDescUrl":"https://..."
            descUrl: '//...'
            descUrl: "..."
            desc_url: "..."
        """
        if not html:
            return ""

        text = self._decode_text(html)

        patterns = [
            r'"descUrl"\s*:\s*"([^"]+)"',
            r'"httpsDescUrl"\s*:\s*"([^"]+)"',
            r'"desc_url"\s*:\s*"([^"]+)"',
            r"'descUrl'\s*:\s*'([^']+)'",
            r"descUrl\s*:\s*'([^']+)'",
            r'descUrl\s*:\s*"([^"]+)"',
            r'httpsDescUrl\s*:\s*"([^"]+)"',
            r'(https?:)?//(?:dsc|desc|assets|g\.alicdn|img)\S+?(?:desc|itemdesc|detail)\S*',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.I | re.S):
                if isinstance(match, tuple):
                    raw = "".join(match)
                else:
                    raw = match

                raw = self._clean_js_url(raw)
                raw = raw.strip().strip('"').strip("'")

                if not raw:
                    continue

                if raw.startswith("//"):
                    raw = "https:" + raw

                if raw.startswith("http"):
                    lower = raw.lower()

                    bad_words = [
                        "login",
                        "passport",
                        "cart",
                        "order",
                        "trade",
                        "pay",
                        "rate",
                        "review",
                    ]

                    if any(w in lower for w in bad_words):
                        continue

                    return raw

        return ""


    def _fetch_desc_html(self, desc_url: str) -> str:
        """
        请求淘宝/天猫详情接口 HTML。

        注意：
            请求头必须为纯 ASCII，不能包含中文。
        """
        if not desc_url:
            return ""

        desc_url = self._clean_js_url(desc_url)

        if desc_url.startswith("//"):
            desc_url = "https:" + desc_url

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://detail.tmall.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            response = client.get(desc_url)
            response.raise_for_status()

            text = response.text or ""

            # 有些接口返回编码识别不准，做一次兜底
            if not text and response.content:
                try:
                    text = response.content.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""

            return text


    def _parse_detail_images_from_desc_html(self, desc_html: str) -> list[str]:
        """
        从 descUrl 返回内容中提取详情图。

        兼容：
            1. 直接 HTML；
            2. var desc='...';
            3. JSON/JSONP content 字段；
            4. 转义字符串；
            5. img/data-src/srcset/background-image。
        """
        if not desc_html:
            return []

        text = self._decode_text(desc_html)

        content_candidates = []

        # ------------------------------------------------------------
        # 1. 提取常见字段
        # ------------------------------------------------------------
        patterns = [
            r'var\s+desc\s*=\s*[\'"](.+?)[\'"]\s*;',
            r'"content"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'content'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
            r'"desc"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'desc'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
            r'"detailContent"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r'"apiStack"\s*:\s*(\[[\s\S]+?\])',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.I | re.S):
                if match:
                    content_candidates.append(match)

        # 无论是否匹配到字段，都加入完整返回体兜底
        content_candidates.append(text)

        urls = []

        for content in content_candidates:
            content = self._decode_text(content)
            content = content.replace('\\"', '"')
            content = content.replace("\\'", "'")
            content = content.replace("\\n", "\n")
            content = content.replace("\\r", "\r")
            content = content.replace("\\t", "\t")

            try:
                soup = BeautifulSoup(content, "lxml")
            except Exception:
                soup = BeautifulSoup(content, "html.parser")

            # --------------------------------------------------------
            # img 标签
            # --------------------------------------------------------
            for img in soup.find_all("img"):
                for attr in [
                    "src",
                    "data-src",
                    "data-original",
                    "data-lazy-src",
                    "data-ks-lazyload",
                    "data-img",
                    "data-url",
                    "data-lazyload",
                    "srcset",
                    "data-srcset",
                ]:
                    value = img.get(attr)
                    if not value:
                        continue

                    if attr in ["srcset", "data-srcset"]:
                        for part in value.split(","):
                            u = part.strip().split(" ")[0]
                            if u:
                                urls.append(u)
                    else:
                        urls.append(value)

            # --------------------------------------------------------
            # background-image
            # --------------------------------------------------------
            urls.extend(self._extract_background_image_urls(content))

            # --------------------------------------------------------
            # 正则全文提取
            # --------------------------------------------------------
            urls.extend(self._extract_image_urls_from_text(content))

        urls = [self._clean_js_url(u) for u in urls if u]
        urls = [normalize_image_url(u) for u in urls if u]
        urls = dedupe_urls(urls)
        urls = self._filter_detail_images(urls)

        return urls


    def _parse_detail_images_from_containers(self, soup: BeautifulSoup) -> list[str]:
        """
        从页面详情容器中提取详情图。

        注意：
            只扫描详情容器，不扫描整页。
        """
        urls = []

        detail_container_selectors = [
            "#description",
            "#J_DivItemDesc",
            "#J_Detail",
            "#J_Desc",
            "#J_DetailMeta",
            "#J_DetailInside",
            ".tb-detail-bd",
            ".tb-desc",
            ".descV8-container",
            ".detail-content",
            ".item-detail",
            ".content-detail",
            ".ItemDetail--content",
            "[class*=desc]",
            "[class*=Desc]",
            "[class*=detail]",
            "[class*=Detail]",
        ]

        for selector in detail_container_selectors:
            try:
                containers = soup.select(selector)
            except Exception:
                containers = []

            for container in containers:
                html = str(container)

                # 排除明显非详情区域
                lower_html = html.lower()
                bad_area_words = [
                    "recommend",
                    "related",
                    "shop",
                    "seller",
                    "comment",
                    "rate",
                    "review",
                    "footer",
                    "header",
                    "navbar",
                ]

                if any(w in lower_html for w in bad_area_words):
                    continue

                for img in container.find_all("img"):
                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-ks-lazyload",
                        "data-img",
                        "data-url",
                        "data-lazyload",
                        "srcset",
                        "data-srcset",
                    ]:
                        value = img.get(attr)
                        if not value:
                            continue

                        if attr in ["srcset", "data-srcset"]:
                            for part in value.split(","):
                                u = part.strip().split(" ")[0]
                                if u:
                                    urls.append(u)
                        else:
                            urls.append(value)

                urls.extend(self._extract_background_image_urls(html))

        urls = [self._clean_js_url(u) for u in urls if u]
        urls = [normalize_image_url(u) for u in urls if u]
        urls = dedupe_urls(urls)

        return urls


    # ----------------------------------------------------------------------
    # SKU 图解析
    # ----------------------------------------------------------------------

    def _parse_sku_images(self, html: str) -> list[ImageItem]:
        """
        解析淘宝/天猫 SKU 图。

        常见字段：
        - picUrl
        - image
        - sku 图片字段
        """

        result = []
        urls = []

        # 尝试解析 SKU 图片 URL
        patterns = [
            r'"picUrl"\s*:\s*"([^"]+)"',
            r'"image"\s*:\s*"([^"]+)"',
            r'"imgUrl"\s*:\s*"([^"]+)"',
            r'"skuPic"\s*:\s*"([^"]+)"',
        ]

        for pattern in patterns:
            for u in re.findall(pattern, html, flags=re.I | re.S):
                u = self._clean_js_url(u)
                urls.append(normalize_image_url(u))

        urls = dedupe_urls(urls)
        urls = self._filter_product_images(urls)

        for u in urls:
            result.append(
                ImageItem(
                    url=u,
                    image_type="sku",
                    ext=get_url_ext(u),
                    source="taobao_sku",
                )
            )

        return result

    # ----------------------------------------------------------------------
    # 通用过滤与工具方法
    # ----------------------------------------------------------------------

    def _filter_product_images(self, urls: list[str]) -> list[str]:
        """
        商品图片通用过滤。

        用于主图、SKU 图。
        """

        result = []

        blacklist = [
            "logo",
            "icon",
            "avatar",
            "qrcode",
            "qr-code",
            "sprite",
            "shop",
            "store",
            "wangwang",
            "seller",
            "beian",
            "police",
            "gongshang",
            "cert",
            "license",
        ]

        for url in urls:
            if not url:
                continue

            lower = url.lower()

            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                continue

            if any(bad in lower for bad in blacklist):
                continue

            result.append(url)

        return dedupe_urls(result)

    def _filter_detail_images(self, urls: list[str]) -> list[str]:
        """
        过滤详情图。

        详情图允许营销图、参数图、长图；
        只过滤明显 UI、店铺、logo、头像、二维码、推荐商品等。
        """
        result = []

        blacklist = [
            "logo",
            "icon",
            "avatar",
            "qrcode",
            "qr_code",
            "sprite",
            "loading",
            "placeholder",
            "default",
            "transparent",
            "blank",
            "shop",
            "seller",
            "store",
            "wangwang",
            "aliww",
            "tmall.com/favicon",
            "taobao.com/favicon",
            "member",
            "rate",
            "review",
            "comment",
            "recommend",
            "related",
            "footer",
            "header",
            "service",
            "certificate",
            "license",
            "auth",
            "coupon",
            "activity",
            "promotion",
        ]

        for url in urls:
            if not url:
                continue

            lower = url.lower()

            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                continue

            if any(bad in lower for bad in blacklist):
                continue

            # 过滤明显小尺寸
            small_patterns = [
                "16x16",
                "24x24",
                "30x30",
                "32x32",
                "40x40",
                "48x48",
                "50x50",
                "60x60",
                "64x64",
                "80x80",
                "100x100",
                "120x120",
            ]

            if any(p in lower for p in small_patterns):
                continue

            result.append(url)

        return dedupe_urls(result)


    def _looks_like_small_icon(self, url: str) -> bool:
        """
        根据 URL 中的尺寸参数过滤小图标。

        例如：
        - 16x16
        - 32x32
        - 80x80
        - _60x60
        - .40x40.
        """

        small_size_patterns = [
            r"[_./-](1[0-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])x(1[0-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])",
            r"width=(1[0-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])",
            r"height=(1[0-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])",
        ]

        for pattern in small_size_patterns:
            if re.search(pattern, url):
                return True

        return False
        
    def _decode_text(self, text: str) -> str:
        """
        安全解码文本。

        注意：
        不要对整段 HTML 使用 unicode_escape，
        否则正常中文可能变成乱码。
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


    def _clean_js_url(self, url: str) -> str:
        """
        清洗 JS/HTML 中的图片 URL。
        """

        if not url:
            return ""

        url = url.strip()
        url = url.strip("'\"")
        url = url.replace("\\/", "/")
        url = html_lib.unescape(url)

        return url

    def _log(self, message: str):
        """
        解析器内部日志。

        如果 browser 中存在 log_callback，则输出到 UI。
        """

        try:
            if hasattr(self.browser, "log_callback") and self.browser.log_callback:
                self.browser.log_callback(message)
        except Exception:
            pass
            
            
    def _extract_background_image_urls(self, text: str) -> list[str]:
        """
        提取 background-image:url(...) 中的图片。
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
