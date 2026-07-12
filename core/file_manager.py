from pathlib import Path
from datetime import datetime

from core.models import ProductData
from utils.text_utils import clean_filename


class FileManager:
    """
    负责创建商品目录和分类子目录。
    """

    @staticmethod
    def create_product_dir(base_dir: str, product: ProductData) -> Path:
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
