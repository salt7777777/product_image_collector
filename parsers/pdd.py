import re
import json
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class PddParser(BaseParser):
    """
    拼多多解析器初版。

    拼多多推荐优先使用 mobile.yangkeduo.com 商品页。
    页面数据结构可能变化，需要根据实际测试继续增强。
    """

    def __init__(
        self,
        log_callback=None,
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
        self.browser = BrowserClient(
            user_data_dir="browser_data/pdd",
            headless=headless,
            login_wait_seconds=login_wait_seconds,
            log_callback=log_callback,
        )


    def parse(self, url: str) -> ProductData:
        platform, product_id = PlatformDetector.detect(url)

        html = self.browser.open_page(url)
        soup = BeautifulSoup(html, "lxml")

        title = self._parse_title(soup, html)
        main_images = self._parse_main_images(html)
        detail_images = self._parse_detail_images(html)
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

    def _parse_title(self, soup: BeautifulSoup, html: str) -> str:
        if soup.title:
            title = soup.title.get_text(strip=True)
            if title:
                return title.replace("拼多多", "").strip()

        patterns = [
            r'"goodsName"\s*:\s*"([^"]+)"',
            r'"goods_name"\s*:\s*"([^"]+)"',
        ]

        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1).replace("\\/", "/")

        return "拼多多商品"

    def _parse_main_images(self, html: str) -> list[ImageItem]:
        urls = []

        patterns = [
            r'"thumbUrl"\s*:\s*"([^"]+)"',
            r'"hdThumbUrl"\s*:\s*"([^"]+)"',
            r'"carouselGallery"\s*:\s*(\[[^\]]+\])',
            r'"gallery"\s*:\s*(\[[^\]]+\])',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, html, flags=re.S):
                if match.startswith("["):
                    try:
                        arr = json.loads(match)
                        for item in arr:
                            if isinstance(item, str):
                                urls.append(normalize_image_url(item.replace("\\/", "/")))
                            elif isinstance(item, dict):
                                for key in ["url", "imageUrl", "thumbUrl", "hdUrl"]:
                                    if item.get(key):
                                        urls.append(normalize_image_url(item[key].replace("\\/", "/")))
                    except Exception:
                        pass
                else:
                    urls.append(normalize_image_url(match.replace("\\/", "/")))

        urls = self._filter_product_images(dedupe_urls(urls))

        return [
            ImageItem(
                url=u,
                image_type="main",
                ext=get_url_ext(u),
                source="pdd_main",
            )
            for u in urls
        ]

    def _parse_detail_images(self, html: str) -> list[ImageItem]:
        urls = []

        patterns = [
            r'"detailGallery"\s*:\s*(\[[^\]]+\])',
            r'"detail_gallery"\s*:\s*(\[[^\]]+\])',
            r'"detailPicList"\s*:\s*(\[[^\]]+\])',
            r'"url"\s*:\s*"([^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, html, flags=re.S | re.I):
                if match.startswith("["):
                    try:
                        arr = json.loads(match)
                        for item in arr:
                            if isinstance(item, str):
                                urls.append(normalize_image_url(item.replace("\\/", "/")))
                            elif isinstance(item, dict):
                                for key in ["url", "imageUrl"]:
                                    if item.get(key):
                                        urls.append(normalize_image_url(item[key].replace("\\/", "/")))
                    except Exception:
                        pass
                else:
                    urls.append(normalize_image_url(match.replace("\\/", "/")))

        urls = self._filter_product_images(dedupe_urls(urls))

        return [
            ImageItem(
                url=u,
                image_type="detail",
                ext=get_url_ext(u),
                source="pdd_detail",
            )
            for u in urls
        ]

    def _parse_sku_images(self, html: str) -> list[ImageItem]:
        urls = []

        patterns = [
            r'"skuThumbUrl"\s*:\s*"([^"]+)"',
            r'"skuImageUrl"\s*:\s*"([^"]+)"',
            r'"thumb_url"\s*:\s*"([^"]+)"',
        ]

        for pattern in patterns:
            for u in re.findall(pattern, html, flags=re.I):
                urls.append(normalize_image_url(u.replace("\\/", "/")))

        urls = self._filter_product_images(dedupe_urls(urls))

        return [
            ImageItem(
                url=u,
                image_type="sku",
                ext=get_url_ext(u),
                source="pdd_sku",
            )
            for u in urls
        ]

    def _filter_product_images(self, urls: list[str]) -> list[str]:
        result = []

        blacklist = [
            "logo",
            "icon",
            "avatar",
            "qrcode",
            "sprite",
            "mall",
            "ad",
        ]

        for url in urls:
            lower = url.lower()

            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                continue

            if any(bad in lower for bad in blacklist):
                continue

            result.append(url)

        return result
