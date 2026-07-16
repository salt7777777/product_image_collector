from urllib.parse import urlparse, parse_qs
import re


class PlatformDetector:
    """
    根据 URL 判断所属平台和商品 ID。
    """

    @staticmethod
    def detect(url: str) -> tuple[str, str]:
        """
        返回：
            platform, product_id

        platform:
            jd / taobao / tmall / pdd / 1688 / unknown
        """
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        query = parse_qs(parsed.query)

        if "jd.com" in host:
            product_id = PlatformDetector._extract_jd_id(url)
            return "jd", product_id

        if "taobao.com" in host:
            product_id = query.get("id", [""])[0]
            return "taobao", product_id

        if "tmall.com" in host:
            product_id = query.get("id", [""])[0]
            return "tmall", product_id

        if "pinduoduo.com" in host or "yangkeduo.com" in host:
            product_id = query.get("goods_id", [""])[0]
            return "pdd", product_id

        if "1688.com" in host:
            product_id = PlatformDetector._extract_1688_id(url)
            return "1688", product_id

        return "unknown", ""

    @staticmethod
    def _extract_jd_id(url: str) -> str:
        """
        京东常见链接：
        https://item.jd.com/100000000000.html
        """
        match = re.search(r"/(\d+)\.html", url)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _extract_1688_id(url: str) -> str:
        """
        1688 常见链接：
        https://detail.1688.com/offer/123456789.html
        https://m.1688.com/offer/123456789.html
        """
        patterns = [
            r"/offer/(\d+)\.html",
            r"offerId=(\d+)",
            r"offerid=(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url, flags=re.I)
            if match:
                return match.group(1)

        return ""
