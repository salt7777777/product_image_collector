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
class DuplicateImage:
    """
    重复图片处理记录。
    """

    original_path: str
    duplicate_path: str
    md5: str
    size: int = 0


@dataclass
class ConvertedImage:
    """
    图片格式转换记录。
    """

    original_path: str
    output_path: str
    backup_path: str
    source_format: str
    target_format: str
    success: bool = True
    reason: str = ""


@dataclass
class SmallImage:
    """
    小图过滤记录。
    """

    original_path: str
    backup_path: str
    width: int
    height: int
    reason: str


@dataclass
class DownloadResult:
    """
    下载结果统计。
    """

    total: int = 0
    success: int = 0
    failed: int = 0
    failed_items: list[FailedDownload] = field(default_factory=list)

    # MD5 去重统计
    duplicate_removed: int = 0
    duplicate_removed_bytes: int = 0
    duplicate_items: list[DuplicateImage] = field(default_factory=list)

    # 图片格式转换统计
    converted_count: int = 0
    convert_failed: int = 0
    converted_items: list[ConvertedImage] = field(default_factory=list)

    # 小图过滤统计
    small_filtered_count: int = 0
    small_filter_failed: int = 0
    small_image_items: list[SmallImage] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.success / self.total * 100, 2)
