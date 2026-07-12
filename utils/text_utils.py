import re


def clean_filename(name: str, max_length: int = 80) -> str:
    """
    清洗 Windows 文件名非法字符。
    """
    if not name:
        return "未命名商品"

    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip()

    if len(name) > max_length:
        name = name[:max_length].strip()

    return name or "未命名商品"


def safe_sku_name(name: str) -> str:
    """
    清洗 SKU 名称。
    """
    return clean_filename(name, max_length=30)
