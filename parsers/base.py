from abc import ABC, abstractmethod

from core.models import ProductData


class BaseParser(ABC):
    """
    所有平台解析器基类。
    """

    @abstractmethod
    def parse(self, url: str) -> ProductData:
        """
        解析商品链接，返回 ProductData。
        """
        raise NotImplementedError
