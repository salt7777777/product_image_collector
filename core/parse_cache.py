import json
import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.models import ProductData, ImageItem


class ParseCache:
    """
    商品解析结果缓存。

    作用：
        同一个商品短时间内重复解析时，直接读取本地缓存，
        避免重复打开浏览器、重复请求详情接口。

    默认目录：
        cache/parse_results/

    默认有效期：
        24 小时
    """

    def __init__(
        self,
        cache_dir: str | Path = "cache/parse_results",
        expire_hours: int = 24,
    ):
        self.cache_dir = Path(cache_dir)
        self.expire_hours = expire_hours
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load(
        self,
        platform: str,
        product_id: str,
        url: str,
    ) -> Optional[ProductData]:
        """
        读取缓存。

        返回：
            ProductData 或 None
        """
        cache_path = self._get_cache_path(platform, product_id, url)

        if not cache_path.exists():
            return None

        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        cached_at = data.get("cached_at", "")

        if not self._is_cache_valid(cached_at):
            return None

        try:
            return self._dict_to_product(data)
        except Exception:
            return None

    def save(self, product: ProductData):
        """
        保存解析结果缓存。
        """
        if not product:
            return

        cache_path = self._get_cache_path(
            product.platform,
            product.product_id,
            product.url,
        )

        data = self._product_to_dict(product)
        data["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear_expired(self):
        """
        清理过期缓存。
        """
        if not self.cache_dir.exists():
            return

        for path in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cached_at = data.get("cached_at", "")

                if not self._is_cache_valid(cached_at):
                    path.unlink(missing_ok=True)
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def clear_all(self):
        """
        清空所有解析缓存。
        """
        if not self.cache_dir.exists():
            return

        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def _is_cache_valid(self, cached_at: str) -> bool:
        if not cached_at:
            return False

        try:
            cached_time = datetime.strptime(cached_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False

        expire_time = cached_time + timedelta(hours=self.expire_hours)
        return datetime.now() <= expire_time

    def _get_cache_path(
        self,
        platform: str,
        product_id: str,
        url: str,
    ) -> Path:
        platform = platform or "unknown"
        product_id = product_id or ""

        if product_id:
            key = f"{platform}_{product_id}"
        else:
            url_hash = hashlib.md5((url or "").encode("utf-8")).hexdigest()
            key = f"{platform}_{url_hash}"

        safe_key = self._safe_filename(key)

        return self.cache_dir / f"{safe_key}.json"

    def _safe_filename(self, name: str) -> str:
        bad_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
        for ch in bad_chars:
            name = name.replace(ch, "_")
        return name.strip() or "unknown"

    def _product_to_dict(self, product: ProductData) -> dict:
        return {
            "platform": product.platform,
            "product_id": product.product_id,
            "title": product.title,
            "url": product.url,
            "main_images": [asdict(item) for item in product.main_images],
            "detail_images": [asdict(item) for item in product.detail_images],
            "sku_images": [asdict(item) for item in product.sku_images],
        }

    def _dict_to_product(self, data: dict) -> ProductData:
        main_images = [
            self._dict_to_image_item(item)
            for item in data.get("main_images", [])
        ]

        detail_images = [
            self._dict_to_image_item(item)
            for item in data.get("detail_images", [])
        ]

        sku_images = [
            self._dict_to_image_item(item)
            for item in data.get("sku_images", [])
        ]

        return ProductData(
            platform=data.get("platform", ""),
            product_id=data.get("product_id", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            main_images=main_images,
            detail_images=detail_images,
            sku_images=sku_images,
        )

    def _dict_to_image_item(self, data: dict) -> ImageItem:
        return ImageItem(
            url=data.get("url", ""),
            image_type=data.get("image_type", ""),
            name=data.get("name", ""),
            ext=data.get("ext", ""),
            sku_name=data.get("sku_name"),
            source=data.get("source", ""),
        )
