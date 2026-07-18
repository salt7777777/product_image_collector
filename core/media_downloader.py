import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from core.models import ReviewItem, ReviewMedia


class MediaDownloader:
    """
    评价媒体下载器。

    用于下载淘宝/天猫评价中的图片和视频。
    """

    IMAGE_EXTS = {
        "jpg",
        "jpeg",
        "png",
        "webp",
        "gif",
        "avif",
    }

    VIDEO_EXTS = {
        "mp4",
        "mov",
        "m4v",
        "webm",
    }

    def __init__(
        self,
        timeout: int = 20,
        retries: int = 3,
        delay: float = 0.25,
        retry_delay: float = 0.5,
    ):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.retry_delay = retry_delay

    def download_review_media(
        self,
        reviews: list[ReviewItem],
        image_dir: str | Path,
        video_dir: str | Path,
        include_video: bool = True,
        log_callback=None,
        progress_callback=None,
        cancel_callback=None,
    ) -> dict:
        image_dir = Path(image_dir)
        video_dir = Path(video_dir)

        image_dir.mkdir(parents=True, exist_ok=True)
        video_dir.mkdir(parents=True, exist_ok=True)

        tasks = []

        for review in reviews:
            review_index = review.index or 0

            for media_index, media in enumerate(review.images, start=1):
                tasks.append(
                    {
                        "review": review,
                        "media": media,
                        "review_index": review_index,
                        "media_index": media_index,
                        "media_type": "image",
                        "save_dir": image_dir,
                    }
                )

            if include_video:
                for media_index, media in enumerate(review.videos, start=1):
                    tasks.append(
                        {
                            "review": review,
                            "media": media,
                            "review_index": review_index,
                            "media_index": media_index,
                            "media_type": "video",
                            "save_dir": video_dir,
                        }
                    )

        summary = {
            "review_count": len(reviews),
            "image_total": sum(len(r.images) for r in reviews),
            "image_success": 0,
            "image_failed": 0,
            "video_total": sum(len(r.videos) for r in reviews) if include_video else 0,
            "video_success": 0,
            "video_failed": 0,
        }

        total = len(tasks)

        if total == 0:
            return summary

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.taobao.com/",
        }

        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for current, task in enumerate(tasks, start=1):
                if cancel_callback and cancel_callback():
                    if log_callback:
                        log_callback("评价媒体下载已停止。")
                    break

                media: ReviewMedia = task["media"]
                media_type = task["media_type"]
                save_dir: Path = task["save_dir"]

                filename = self._build_filename(
                    review_index=task["review_index"],
                    media_index=task["media_index"],
                    media=media,
                    media_type=media_type,
                )

                save_path = save_dir / filename

                if log_callback:
                    log_callback(f"开始下载评价{self._type_label(media_type)}：{filename}")

                ok, reason = self._download_one(
                    client=client,
                    url=media.url,
                    save_path=save_path,
                    media_type=media_type,
                    cancel_callback=cancel_callback,
                )

                media.local_path = str(save_path) if ok else ""
                media.download_success = ok
                media.download_reason = reason

                if ok:
                    if media_type == "image":
                        summary["image_success"] += 1
                    else:
                        summary["video_success"] += 1

                    if log_callback:
                        log_callback(f"评价{self._type_label(media_type)}下载成功：{filename}")

                else:
                    if media_type == "image":
                        summary["image_failed"] += 1
                    else:
                        summary["video_failed"] += 1

                    if log_callback:
                        log_callback(
                            f"评价{self._type_label(media_type)}下载失败：{filename}，原因：{reason}"
                        )

                if progress_callback:
                    try:
                        progress_callback(current, total)
                    except Exception:
                        pass

                time.sleep(self.delay)

        return summary

    def _download_one(
        self,
        client: httpx.Client,
        url: str,
        save_path: Path,
        media_type: str,
        cancel_callback=None,
    ) -> tuple[bool, str]:
        if not url:
            return False, "媒体URL为空"

        if url.startswith("blob:"):
            return False, "blob 视频地址无法直接下载"

        if ".m3u8" in url.lower():
            return False, "m3u8 视频暂不支持直接下载"

        if not url.startswith("http://") and not url.startswith("https://"):
            return False, "媒体URL缺少 http:// 或 https://"

        last_error = ""

        for attempt in range(1, self.retries + 1):
            if cancel_callback and cancel_callback():
                return False, "任务已停止"

            try:
                resp = client.get(url)

                if cancel_callback and cancel_callback():
                    return False, "任务已停止"

                if resp.status_code != 200:
                    last_error = f"HTTP状态码异常：{resp.status_code}"

                    if attempt < self.retries:
                        time.sleep(self.retry_delay)
                        continue

                    return False, last_error

                if not resp.content:
                    last_error = "响应内容为空"

                    if attempt < self.retries:
                        time.sleep(self.retry_delay)
                        continue

                    return False, last_error

                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(resp.content)

                return True, ""

            except Exception as e:
                last_error = str(e)

                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue

        return False, last_error or "未知下载错误"

    def _build_filename(
        self,
        review_index: int,
        media_index: int,
        media: ReviewMedia,
        media_type: str,
    ) -> str:
        ext = media.ext or self._guess_ext(media.url, media_type)

        if media_type == "image":
            if ext not in self.IMAGE_EXTS:
                ext = "jpg"
        else:
            if ext not in self.VIDEO_EXTS:
                ext = "mp4"

        return f"{review_index:03d}_{media_index:02d}.{ext}"

    def _guess_ext(self, url: str, media_type: str) -> str:
        try:
            path = urlparse(url).path.lower()

            candidates = self.IMAGE_EXTS if media_type == "image" else self.VIDEO_EXTS

            for ext in candidates:
                if f".{ext}" in path:
                    return ext

        except Exception:
            pass

        return "jpg" if media_type == "image" else "mp4"

    def _type_label(self, media_type: str) -> str:
        return "图片" if media_type == "image" else "视频"
