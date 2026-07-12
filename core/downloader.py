import time
from pathlib import Path

import httpx

from core.models import ImageItem, DownloadResult, FailedDownload
from utils.url_utils import get_url_ext
from utils.text_utils import safe_sku_name


class ImageDownloader:
    """
    图片下载器。
    """

    def __init__(
        self,
        timeout: int = 20,
        retries: int = 2,
        delay: float = 0.3,
    ):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay

    def download_images(
        self,
        images_by_type: dict[str, list[ImageItem]],
        dirs_by_type: dict[str, Path],
        progress_callback=None,
        log_callback=None,
    ) -> DownloadResult:
        """
        下载图片。

        images_by_type:
            {
                "main": [...],
                "detail": [...],
                "sku": [...]
            }
        """
        all_tasks = []

        for image_type, images in images_by_type.items():
            if image_type not in dirs_by_type:
                continue

            for index, item in enumerate(images, start=1):
                all_tasks.append((image_type, index, item, dirs_by_type[image_type]))

        result = DownloadResult(total=len(all_tasks))

        if result.total == 0:
            return result

        for current_index, task in enumerate(all_tasks, start=1):
            image_type, index, item, save_dir = task

            filename = self._build_filename(image_type, index, item)
            save_path = save_dir / filename

            if log_callback:
                log_callback(f"开始下载：{filename}")

            ok, reason = self._download_one(item.url, save_path)

            if ok:
                result.success += 1
                if log_callback:
                    log_callback(f"下载成功：{filename}")
            else:
                result.failed += 1
                result.failed_items.append(
                    FailedDownload(
                        image_type=image_type,
                        url=item.url,
                        reason=reason,
                        filename=filename,
                    )
                )
                if log_callback:
                    log_callback(f"下载失败：{filename}，原因：{reason}")

            if progress_callback:
                progress_callback(current_index, result.total)

            time.sleep(self.delay)

        return result

    def _build_filename(self, image_type: str, index: int, item: ImageItem) -> str:
        ext = item.ext or get_url_ext(item.url)

        if image_type == "sku" and item.sku_name:
            sku_name = safe_sku_name(item.sku_name)
            return f"{index:03d}_{sku_name}.{ext}"

        return f"{index:03d}.{ext}"

    def _download_one(self, url: str, save_path: Path) -> tuple[bool, str]:
        """
        下载单张图片。
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Referer": "https://www.jd.com/",
        }

        last_error = ""

        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(url, headers=headers)

                if resp.status_code != 200:
                    last_error = f"HTTP状态码异常：{resp.status_code}"
                    continue

                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type.lower():
                    # 有些平台可能不返回标准 content-type，所以这里不强制失败。
                    pass

                save_path.write_bytes(resp.content)
                return True, ""

            except Exception as e:
                last_error = str(e)

        return False, last_error or "未知下载错误"
