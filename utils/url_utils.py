from urllib.parse import urljoin, urlparse


def normalize_image_url(url: str, base_url: str = "https:") -> str:
    """
    规范化图片 URL。
    """
    if not url:
        return ""

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("http://") or url.startswith("https://"):
        return url

    if url.startswith("/"):
        return urljoin(base_url, url)

    return url


def get_url_ext(url: str) -> str:
    """
    从 URL 中推断图片扩展名。
    """
    path = urlparse(url).path.lower()

    for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".apng"]:
        if ext in path:
            if ext == ".apng":
                return "png"
            return ext.replace(".", "")

    return "jpg"


def dedupe_urls(urls: list[str]) -> list[str]:
    """
    URL 去重，保持顺序。
    """
    seen = set()
    result = []

    for url in urls:
        if not url:
            continue

        if url not in seen:
            seen.add(url)
            result.append(url)

    return result
