from pathlib import Path
from datetime import datetime

from core.models import ProductData
from utils.text_utils import clean_filename


class FileManager:
    """
    负责创建商品目录和分类子目录。
    """

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
            output/2026-07-15/
            output/京东/
            output/2026-07-15/京东/
        """
        path = Path(base_dir)

        if organize_by_date:
            date_name = datetime.now().strftime("%Y-%m-%d")
            path = path / date_name

        if organize_by_platform:
            path = path / FileManager.get_platform_display_name(product.platform)

        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def get_platform_display_name(platform: str) -> str:
        """
        平台名称转中文目录名。
        """
        mapping = {
            "jd": "京东",
            "taobao": "淘宝",
            "tmall": "天猫",
            "pdd": "拼多多",
        }

        return mapping.get(platform, platform or "未知平台")

    @staticmethod
    def create_product_dir(base_dir: str | Path, product: ProductData) -> Path:
        """
        根据商品标题和商品 ID 创建目录。
        """
        title = clean_filename(product.title)
        product_id = product.product_id or "unknown"

        folder_name = f"{title}_{product_id}"
        product_dir = Path(base_dir) / folder_name

        if product_dir.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            product_dir = Path(base_dir) / f"{folder_name}_{timestamp}"

        product_dir.mkdir(parents=True, exist_ok=True)
        return product_dir

    @staticmethod
    def create_type_dirs(product_dir: Path, selected_types: dict[str, bool]) -> dict[str, Path]:
        """
        按图片类型创建子目录。
        """
        mapping = {
            "main": "主图",
            "detail": "详情图",
            "sku": "SKU图",
        }

        dirs = {}

        for image_type, selected in selected_types.items():
            if selected:
                path = product_dir / mapping[image_type]
                path.mkdir(parents=True, exist_ok=True)
                dirs[image_type] = path

        return dirs
