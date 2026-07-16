from pathlib import Path
from datetime import datetime

from core.models import ProductData
from utils.text_utils import clean_filename


class FileManager:
    """
    负责创建商品目录和分类子目录。
    """

    PLATFORM_NAME_MAP = {
        "jd": "京东",
        "taobao": "淘宝",
        "tmall": "天猫",
        "pdd": "拼多多",
        "1688": "1688",
    }

    IMAGE_TYPE_DIR_MAP = {
        "main": "主图",
        "detail": "详情图",
        "sku": "SKU图",
    }

    @staticmethod
    def resolve_output_base_dir(
        base_dir: str,
        product: ProductData,
        organize_by_date: bool = False,
        organize_by_platform: bool = False,
    ) -> Path:
        """
        根据配置生成最终输出根目录。

        示例：
            output/
            output/2026-07-16/
            output/1688/
            output/2026-07-16/1688/
        """
        path = Path(base_dir)

        if organize_by_date:
            date_name = datetime.now().strftime("%Y-%m-%d")
            path = path / date_name

        if organize_by_platform:
            platform_name = FileManager.get_platform_display_name(product.platform)
            path = path / platform_name

        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def get_platform_display_name(platform: str) -> str:
        """
        平台名称转中文目录名。
        """
        return FileManager.PLATFORM_NAME_MAP.get(platform, platform or "未知平台")

    @staticmethod
    def create_product_dir(base_dir: str | Path, product: ProductData) -> Path:
        """
        根据商品标题和商品 ID 创建商品目录。
        """
        title = clean_filename(product.title or "未命名商品")
        product_id = product.product_id or "unknown"

        folder_name = f"{title}_{product_id}"
        product_dir = Path(base_dir) / folder_name

        if product_dir.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            product_dir = Path(base_dir) / f"{folder_name}_{timestamp}"

        product_dir.mkdir(parents=True, exist_ok=True)
        return product_dir

    @staticmethod
    def create_type_dirs(product_dir: str | Path, product: ProductData | None = None) -> dict:
        """
        创建图片分类目录。

        兼容两种调用：
            FileManager.create_type_dirs(product_dir)
            FileManager.create_type_dirs(product_dir, product)

        返回：
            {
                "main": 主图目录,
                "detail": 详情图目录,
                "sku": SKU图目录,
            }
        """
        product_dir = Path(product_dir)

        dirs = {}

        for image_type, dir_name in FileManager.IMAGE_TYPE_DIR_MAP.items():
            path = product_dir / dir_name
            path.mkdir(parents=True, exist_ok=True)
            dirs[image_type] = path

        return dirs

    @staticmethod
    def create_category_dirs(product_dir: str | Path, product: ProductData | None = None) -> dict:
        """
        兼容旧代码：创建分类目录。

        兼容两种调用：
            FileManager.create_category_dirs(product_dir)
            FileManager.create_category_dirs(product_dir, product)
        """
        return FileManager.create_type_dirs(product_dir, product)

    @staticmethod
    def get_type_dir(product_dir: str | Path, image_type: str) -> Path:
        """
        获取指定图片类型的保存目录。
        """
        product_dir = Path(product_dir)
        dir_name = FileManager.IMAGE_TYPE_DIR_MAP.get(image_type, image_type or "其他图片")

        path = product_dir / dir_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def ensure_dir(path: str | Path) -> Path:
        """
        确保目录存在。
        """
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        return path
