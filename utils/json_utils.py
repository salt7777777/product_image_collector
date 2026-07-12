import json
import re


def find_json_objects_by_keyword(html: str, keyword: str) -> list[str]:
    """
    简单从 HTML 中查找包含某个关键字的 JSON 片段。
    这只是辅助工具，不保证所有平台通用。
    """
    result = []

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S | re.I)

    for script in scripts:
        if keyword in script:
            result.append(script)

    return result


def try_load_json(text: str):
    """
    尝试加载 JSON。
    """
    try:
        return json.loads(text)
    except Exception:
        return None


def recursive_find_image_urls(data, result: list[str]):
    """
    递归扫描 JSON 中的图片 URL。
    注意：这个方法只能作为辅助，不能作为精准分类依据。
    """
    if isinstance(data, dict):
        for _, value in data.items():
            recursive_find_image_urls(value, result)

    elif isinstance(data, list):
        for item in data:
            recursive_find_image_urls(item, result)

    elif isinstance(data, str):
        lower = data.lower()
        if any(x in lower for x in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            if lower.startswith("http") or lower.startswith("//"):
                result.append(data)
