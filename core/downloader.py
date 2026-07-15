import re
import time
from pathlib import Path

import httpx

from core.models import ImageItem, DownloadResult, FailedDownload
from utils.url_utils import get_url_ext, dedupe_urls
from utils.text_utils import safe_sku_name


class ImageDownloader:
    """
    图片下载器。

    支持：
    1. 超时控制；
    2. 下载失败重试；
    3. 下载进度回调；
    4. 失败明细记录；
    5. 外部停止任务；
    6. 高清图优先下载，失败自动回退原图。
    """

    def __init__(
        self,
        timeout: int = 20,
        retries: int = 3,
        delay: float = 0.3,
        retry_delay: float = 0.5,
        high_quality: bool = False,
    ):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.retry_delay = retry_delay
        self.high_quality = high_quality

    def download_images(
        self,
        images_by_type: dict[str, list[ImageItem]],
        dirs_by_type: dict[str, Path],
        progress_callback=None,
        log_callback=None,
        cancel_callback=None,
    ) -> DownloadResult:
        """
        下载图片。

        cancel_callback:
            返回 True 表示请求停止任务。
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
            if cancel_callback and cancel_callback():
                if log_callback:
                    log_callback("下载任务已停止。")
                break

            image_type, index, item, save_dir = task

            filename = self._build_filename(image_type, index, item)
            save_path = save_dir / filename

            if log_callback:
                log_callback(f"开始下载：{filename}")

            ok, reason = self._download_one(
                url=item.url,
                save_path=save_path,
                filename=filename,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )

            if cancel_callback and cancel_callback():
                if log_callback:
                    log_callback("下载任务已停止。")
                break

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

    def _download_one(
        self,
        url: str,
        save_path: Path,
        filename: str = "",
        log_callback=None,
        cancel_callback=None,
    ) -> tuple[bool, str]:
        """
        下载单张图片，失败自动重试。

        如果 high_quality=True：
        1. 优先尝试高清候选 URL；
        2. 高清 URL 下载失败后，自动回退原始 URL。
        """

        if cancel_callback and cancel_callback():
            return False, "任务已停止"

        if not url:
            return False, "图片URL为空"

        if not url.startswith("http://") and not url.startswith("https://"):
            return False, "图片URL缺少 http:// 或 https:// 协议"

        candidate_urls = self._build_candidate_urls(url)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": self._guess_referer(url),
        }

        last_error = ""

        for candidate_index, candidate_url in enumerate(candidate_urls, start=1):
            if cancel_callback and cancel_callback():
                return False, "任务已停止"

            is_fallback = candidate_index > 1

            if self.high_quality and is_fallback and log_callback:
                log_callback(f"高清图下载失败，回退原图：{filename or save_path.name}")

            for attempt in range(1, self.retries + 1):
                if cancel_callback and cancel_callback():
                    return False, "任务已停止"

                try:
                    with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                        resp = client.get(candidate_url, headers=headers)

                    if cancel_callback and cancel_callback():
                        return False, "任务已停止"

                    if resp.status_code != 200:
                        last_error = f"HTTP状态码异常：{resp.status_code}"

                        if attempt < self.retries:
                            if log_callback:
                                log_callback(
                                    f"下载失败，准备重试：{filename or save_path.name} "
                                    f"({attempt}/{self.retries})，原因：{last_error}"
                                )
                            time.sleep(self.retry_delay)
                            continue

                        break

                    if not resp.content:
                        last_error = "响应内容为空"

                        if attempt < self.retries:
                            if log_callback:
                                log_callback(
                                    f"下载失败，准备重试：{filename or save_path.name} "
                                    f"({attempt}/{self.retries})，原因：{last_error}"
                                )
                            time.sleep(self.retry_delay)
                            continue

                        break

                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    save_path.write_bytes(resp.content)

                    return True, ""

                except Exception as e:
                    last_error = str(e)

                    if attempt < self.retries:
                        if log_callback:
                            log_callback(
                                f"下载失败，准备重试：{filename or save_path.name} "
                                f"({attempt}/{self.retries})，原因：{last_error}"
                            )
                        time.sleep(self.retry_delay)
                        continue

                    break

        return False, last_error or "未知下载错误"

    def _build_candidate_urls(self, url: str) -> list[str]:
        """
        构建候选下载 URL。

        high_quality=False：
            只返回原 URL。

        high_quality=True：
            返回 [高清 URL, 原 URL]，并去重。
        """

        if not self.high_quality:
            return [url]

        high_url = self._to_high_quality_url(url)

        return dedupe_urls([high_url, url])

    def _to_high_quality_url(self, url: str) -> str:
        """
        尽量将平台图片 URL 转换为高清版本。

        规则是保守策略：
        1. 京东 n1/n5 等缩略图尝试转 n0；
        2. 京东 s数字x数字_jfs 缩略图转 jfs 原路径；
        3. 淘宝/天猫去掉尺寸后缀；
        4. 失败时下载器会自动回退原图。
        """

        if not url:
            return url

        high_url = url.strip()
        lower = high_url.lower()

        # ------------------------------------------------------------
        # 京东图片高清化
        # ------------------------------------------------------------
        if "360buyimg.com" in lower or "jdimg.com" in lower:
            # /n1/ /n5/ /n7/ -> /n0/
            high_url = re.sub(r"/n\d+/", "/n0/", high_url, count=1, flags=re.I)

            # /n0/s450x450_jfs/... -> /n0/jfs/...
            high_url = re.sub(
                r"/n0/s\d+x\d+_jfs/",
                "/n0/jfs/",
                high_url,
                flags=re.I,
            )

            # /n1/s450x450_jfs/... -> /n0/jfs/...
            high_url = re.sub(
                r"/n\d+/s\d+x\d+_jfs/",
                "/n0/jfs/",
                high_url,
                flags=re.I,
            )

            # 去掉常见压缩参数
            high_url = re.sub(
                r"!(q\d+|cc_\d+x\d+|s\d+x\d+|cr_\d+x\d+_\d+_\d+).*?$",
                "",
                high_url,
                flags=re.I,
            )

            return high_url

        # ------------------------------------------------------------
        # 淘宝 / 天猫图片高清化
        # ------------------------------------------------------------
        if (
            "alicdn.com" in lower
            or "taobao" in lower
            or "tmall" in lower
            or "tbcdn" in lower
        ):
            # 处理 .jpg_430x430q90.jpg / .jpg_.webp 等
            high_url = re.sub(
                r"(\.(?:jpg|jpeg|png|webp))(?:_[^?]*)+$",
                r"\1",
                high_url,
                flags=re.I,
            )

            # 处理 .jpg_.webp
            high_url = re.sub(
                r"(\.(?:jpg|jpeg|png))_\.webp$",
                r"\1",
                high_url,
                flags=re.I,
            )

            # 处理 .jpg_220x220.jpg
            high_url = re.sub(
                r"(\.(?:jpg|jpeg|png))_\d+x\d+[^?]*(\.(?:jpg|jpeg|png|webp))?$",
                r"\1",
                high_url,
                flags=re.I,
            )

            return high_url

        return high_url

    def _guess_referer(self, url: str) -> str:
        """
        根据图片域名猜测 Referer。
        """

        lower = url.lower()

        if "360buyimg.com" in lower or "jdimg.com" in lower:
            return "https://www.jd.com/"

        if "alicdn.com" in lower or "taobao" in lower or "tmall" in lower:
            return "https://www.taobao.com/"

        if "pinduoduo" in lower or "yangkeduo" in lower:
            return "https://mobile.yangkeduo.com/"

        return "https://www.jd.com/"
