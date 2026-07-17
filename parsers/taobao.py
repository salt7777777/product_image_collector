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

        常见主图字段：
        - auctionImages
        - images
        """

        urls = []

        patterns = [
            r'"auctionImages"\s*:\s*(\[[^\]]+\])',
            r'"images"\s*:\s*(\[[^\]]+\])',
            r'"mainImages"\s*:\s*(\[[^\]]+\])',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, html, flags=re.S):
                try:
                    arr = json.loads(match)

                    for item in arr:
                        if isinstance(item, str):
                            url = self._clean_js_url(item)
                            urls.append(normalize_image_url(url))

                        elif isinstance(item, dict):
                            for key in ["url", "image", "picUrl", "imgUrl"]:
                                if item.get(key):
                                    url = self._clean_js_url(item[key])
                                    urls.append(normalize_image_url(url))

                except Exception:
                    pass

        urls = dedupe_urls(urls)
        urls = self._filter_product_images(urls)

        return [
            ImageItem(
                url=u,
                image_type="main",
                ext=get_url_ext(u),
                source="taobao_main",
            )
            for u in urls
        ]

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

        常见字段：
        - "descUrl":"//dscnew.taobao.com/..."
        - "httpsDescUrl":"https://..."
        - descUrl: '//...'
        """

        patterns = [
            r'"descUrl"\s*:\s*"([^"]+)"',
            r'"httpsDescUrl"\s*:\s*"([^"]+)"',
            r"'descUrl'\s*:\s*'([^']+)'",
            r"descUrl\s*:\s*'([^']+)'",
            r'descUrl\s*:\s*"([^"]+)"',
            r'"desc_url"\s*:\s*"([^"]+)"',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, flags=re.S)
            if match:
                url = match.group(1)
                url = self._clean_js_url(url)
                url = normalize_image_url(url)
                return url

        return ""

    def _fetch_desc_html(self, desc_url: str) -> str:
        """
        请求淘宝/天猫详情接口，返回详情 HTML。

        descUrl 返回的一般是真正的商品详情内容，比整页 HTML 精准很多。
        """

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Referer": "https://detail.tmall.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(desc_url, headers=headers)

        if response.status_code != 200:
            raise RuntimeError(f"详情接口请求失败，状态码：{response.status_code}")

        text = response.text

        # HTML 反转义
        text = html_lib.unescape(text)

        # 有些接口返回 JS 包裹内容，例如：
        # var desc='...';
        # callback(...)
        # 这里不强制截取，后面统一用 BeautifulSoup 和正则解析。
        return text

    def _parse_detail_images_from_desc_html(self, desc_html: str) -> list[str]:
        """
        从详情接口返回的 HTML 中提取详情图。
        """

        urls = []

        soup = BeautifulSoup(desc_html, "lxml")

        # 详情接口里的 img 通常比较干净
        for img in soup.select("img"):
            src = (
                img.get("data-src")
                or img.get("data-ks-lazyload")
                or img.get("data-lazyload")
                or img.get("data-original")
                or img.get("src")
            )

            if src:
                src = self._clean_js_url(src)
                urls.append(normalize_image_url(src))

        # 部分详情图可能在 style background-image 中
        style_patterns = [
            r'background-image\s*:\s*url\(["\']?(.*?)["\']?\)',
            r'background\s*:\s*url\(["\']?(.*?)["\']?\)',
        ]

        for pattern in style_patterns:
            for src in re.findall(pattern, desc_html, flags=re.I | re.S):
                if src:
                    src = self._clean_js_url(src)
                    urls.append(normalize_image_url(src))

        # 有些接口返回字符串中包含转义后的 img
        regex_patterns = [
            r'<img[^>]+src=["\'](//[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
            r'<img[^>]+data-src=["\'](//[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
            r'data-ks-lazyload=["\'](//[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
            r'(//[^"\'>\s]+\.(?:jpg|jpeg|png|webp)[^"\'>\s]*)',
        ]

        for pattern in regex_patterns:
            for src in re.findall(pattern, desc_html, flags=re.I | re.S):
                if src:
                    src = self._clean_js_url(src)
                    urls.append(normalize_image_url(src))

        return dedupe_urls(urls)

    def _parse_detail_images_from_containers(self, soup: BeautifulSoup) -> list[str]:
        """
        从页面详情区域容器中提取详情图。

        注意：
        这里不再使用 soup.select("img") 全局抓图。
        只允许从疑似详情容器里抓。
        """

        urls = []

        detail_container_selectors = [
            "#description",
            "#J_DivItemDesc",
            "#J_Detail",
            "#J_Desc",
            "#J_DetailMeta",
            "#attributes",
            ".tb-detail-bd",
            ".tb-desc",
            ".descV8-container",
            ".detail-content",
            ".item-detail",
            ".content-detail",
            ".rax-view-v2",
            ".MainContent--mainContent",
            ".ItemDetail--content",
        ]

        containers = []

        for selector in detail_container_selectors:
            found = soup.select(selector)
            if found:
                containers.extend(found)

        # 如果没有找到详情容器，直接返回空，不做整页 img 扫描
        if not containers:
            return []

        for container in containers:
            for img in container.select("img"):
                src = (
                    img.get("data-src")
                    or img.get("data-ks-lazyload")
                    or img.get("data-lazyload")
                    or img.get("data-original")
                    or img.get("src")
                )

                if src:
                    src = self._clean_js_url(src)
                    urls.append(normalize_image_url(src))

            # 详情容器里有时也有 background-image
            html = str(container)

            style_patterns = [
                r'background-image\s*:\s*url\(["\']?(.*?)["\']?\)',
                r'background\s*:\s*url\(["\']?(.*?)["\']?\)',
            ]

            for pattern in style_patterns:
                for src in re.findall(pattern, html, flags=re.I | re.S):
                    if src:
                        src = self._clean_js_url(src)
                        urls.append(normalize_image_url(src))

        return dedupe_urls(urls)

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
        过滤明显不属于商品详情图的图片。

        主要过滤：
        - 天猫/淘宝 Logo
        - 店铺图标
        - 会员图标
        - 认证/备案图标
        - sprite/icon
        - qrcode
        - avatar
        - 装饰性小图
        """

        result = []

        blacklist_keywords = [
            # logo/icon/sprite
            "logo",
            "icon",
            "sprite",
            "tb-logo",

            # 注意：这里不要简单过滤 tmall/taobao，
            # 因为很多商品详情图本身 CDN 域名可能包含 taobao/tmall。
            # 所以这里不加入 "tmall" 和 "taobao"。

            # 店铺和账号相关
            "shop",
            "store",
            "seller",
            "avatar",
            "wangwang",

            # 二维码/认证/备案
            "qrcode",
            "qr-code",
            "beian",
            "police",
            "gongshang",
            "cert",
            "license",
            "credit",

            # 会员/服务/装饰图
            "vip",
            "service",
            "promise",
            "guarantee",
            "badge",
            "medal",

            # 广告/推荐
            "ad",
            "banner",
            "recommend",
            "promotion",
        ]

        for url in urls:
            if not url:
                continue

            lower = url.lower()

            # 必须是常见图片格式
            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                continue

            # 过滤明显无关图片
            if any(keyword in lower for keyword in blacklist_keywords):
                continue

            # 过滤明显小图标
            if self._looks_like_small_icon(lower):
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
