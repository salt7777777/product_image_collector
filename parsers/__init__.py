from parsers.jd import JDParser
from parsers.taobao import TaobaoParser
from parsers.pdd import PddParser
from parsers.alibaba1688 import Alibaba1688Parser


def get_parser(
    platform: str,
    log_callback=None,
    headless: bool = False,
    login_wait_seconds: int = 180,
):
    """
    根据平台返回对应解析器。
    """
    if platform == "jd":
        return JDParser(
            log_callback=log_callback,
            headless=headless,
            login_wait_seconds=login_wait_seconds,
        )

    if platform in ["taobao", "tmall"]:
        return TaobaoParser(
            log_callback=log_callback,
            headless=headless,
            login_wait_seconds=login_wait_seconds,
        )

    if platform == "pdd":
        return PddParser(
            log_callback=log_callback,
            headless=headless,
            login_wait_seconds=login_wait_seconds,
        )

    if platform == "1688":
        return Alibaba1688Parser(
            log_callback=log_callback,
            headless=headless,
            login_wait_seconds=login_wait_seconds,
        )

    raise ValueError(f"暂不支持的平台：{platform}")
