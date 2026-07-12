from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImageItem:
    """
    单张图片信息。
    """
    url: str
    image_type: str  # main / detail / sku
    name: str = ""
    ext: str = ""
    sku_name: Optional[str] = None
    source: str = ""


@dataclass
class ProductData:
    """
    商品解析结果。
    """
    platform: str
    product_id: str
    title: str
    url: str

    main_images: list[ImageItem] = field(default_factory=list)
    detail_images: list[ImageItem] = field(default_factory=list)
    sku_images: list[ImageItem] = field(default_factory=list)

    def total_count(self) -> int:
        return len(self.main_images) + len(self.detail_images) + len(self.sku_images)


@dataclass
class FailedDownload:
    """
    下载失败记录。
    """
    image_type: str
    url: str
    reason: str
    filename: str = ""


@dataclass
class DownloadResult:
    """
    下载结果统计。
    """
    total: int = 0
    success: int = 0
    failed: int = 0
    failed_items: list[FailedDownload] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.success / self.total * 100, 2)
