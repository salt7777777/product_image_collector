from parsers.jd import JDParser
from parsers.taobao import TaobaoParser
from parsers.pdd import PddParser


def get_parser(platform: str, log_callback=None):
    """
    根据平台返回对应解析器。
    """
    if platform == "jd":
        return JDParser(log_callback=log_callback)

    if platform in ["taobao", "tmall"]:
        return TaobaoParser(log_callback=log_callback)

    if platform == "pdd":
        return PddParser(log_callback=log_callback)

    raise ValueError(f"暂不支持的平台：{platform}")
