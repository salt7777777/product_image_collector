from dataclasses import dataclass, field
from pathlib import Path
import shutil

from PIL import Image


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
}


SUPPORTED_TARGET_FORMATS = {
    "jpg": ".jpg",
    "png": ".png",
    "webp": ".webp",
}


@dataclass
class ImageConvertItem:
    """
    单张图片格式转换记录。
    """

    original_path: str
    output_path: str
    backup_path: str
    source_format: str
    target_format: str
    success: bool = True
    reason: str = ""


@dataclass
class ImageConvertResult:
    """
    图片格式转换结果。
    """

    scanned_count: int = 0
    converted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    items: list[ImageConvertItem] = field(default_factory=list)


def convert_image_files(
    root_dir: str | Path,
    target_format: str,
    backup_dir_name: str = "_格式转换备份",
    exclude_dir_names: set[str] | None = None,
    quality: int = 92,
    progress_callback=None,
    log_every: int = 5,
) -> ImageConvertResult:
    """
    批量转换图片格式。

    参数：
        root_dir:
            商品目录。

        target_format:
            jpg / png / webp / original

        backup_dir_name:
            原图备份目录。

        exclude_dir_names:
            排除目录，例如 _重复图片备份、_格式转换备份。

        quality:
            JPG / WEBP 保存质量。

        progress_callback:
            转换进度回调。
            参数：
                current, total, path

        log_every:
            每处理多少张触发一次外部进度提示。

    行为：
        1. target_format=original 时不做任何转换。
        2. 已经是目标格式的图片跳过，不重复压缩。
        3. 转换成功后，原图移动到 _格式转换备份。
        4. 转换失败时保留原图。
        5. PNG 使用较快压缩参数，避免长时间无响应。
    """
    result = ImageConvertResult()

    target_format = (target_format or "original").lower().strip()

    if target_format == "original":
        return result

    if target_format not in SUPPORTED_TARGET_FORMATS:
        return result

    root_path = Path(root_dir)

    if not root_path.exists():
        return result

    exclude_dir_names = exclude_dir_names or {
        "_重复图片备份",
        "_格式转换备份",
        "_小图过滤",
    }

    target_ext = SUPPORTED_TARGET_FORMATS[target_format]
    backup_root = root_path / backup_dir_name

    image_files = [
        path
        for path in root_path.rglob("*")
        if _is_candidate_image(path, exclude_dir_names)
    ]

    image_files.sort(key=lambda p: str(p))

    total = len(image_files)

    for current, path in enumerate(image_files, start=1):
        if not path.exists():
            _emit_progress(progress_callback, current, total, path)
            continue

        result.scanned_count += 1

        source_ext = path.suffix.lower()

        try:
            # 已经是目标格式，跳过
            if _is_same_format(source_ext, target_format):
                result.skipped_count += 1
                _emit_progress(progress_callback, current, total, path)
                continue

            output_path = _build_output_path(path, target_ext)
            output_path = _avoid_path_conflict(output_path)

            backup_path = _build_backup_path(
                root_path=root_path,
                backup_root=backup_root,
                original_path=path,
            )
            backup_path = _avoid_path_conflict(backup_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)

            _convert_one_image(
                source_path=path,
                output_path=output_path,
                target_format=target_format,
                quality=quality,
            )

            # 转换成功后移动原图到备份
            shutil.move(str(path), str(backup_path))

            result.converted_count += 1
            result.items.append(
                ImageConvertItem(
                    original_path=str(path),
                    output_path=str(output_path),
                    backup_path=str(backup_path),
                    source_format=source_ext.replace(".", ""),
                    target_format=target_format,
                    success=True,
                )
            )

        except Exception as e:
            result.failed_count += 1
            result.items.append(
                ImageConvertItem(
                    original_path=str(path),
                    output_path="",
                    backup_path="",
                    source_format=source_ext.replace(".", ""),
                    target_format=target_format,
                    success=False,
                    reason=str(e),
                )
            )

        _emit_progress(progress_callback, current, total, path)

    return result


def _emit_progress(progress_callback, current: int, total: int, path: Path) -> None:
    """
    触发转换进度回调。
    """
    if progress_callback:
        try:
            progress_callback(current, total, path)
        except Exception:
            pass


def _is_candidate_image(path: Path, exclude_dir_names: set[str]) -> bool:
    """
    判断是否为候选图片文件。
    """
    if not path.is_file():
        return False

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return False

    for part in path.parts:
        if part in exclude_dir_names:
            return False

    return True


def _is_same_format(source_ext: str, target_format: str) -> bool:
    """
    判断源文件是否已经是目标格式。
    """
    source_ext = source_ext.lower()

    if target_format == "jpg":
        return source_ext in [".jpg", ".jpeg"]

    if target_format == "png":
        return source_ext == ".png"

    if target_format == "webp":
        return source_ext == ".webp"

    return False


def _convert_one_image(
    source_path: Path,
    output_path: Path,
    target_format: str,
    quality: int = 92,
) -> None:
    """
    转换单张图片。

    性能优化：
        JPG:
            不使用 optimize=True，速度更快。

        PNG:
            不使用 optimize=True。
            使用 compress_level=3，速度和体积折中。

        WEBP:
            method=4，比 method=6 快。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as img:
        img.load()

        if target_format == "jpg":
            converted = _convert_to_jpg_compatible(img)
            converted.save(
                output_path,
                format="JPEG",
                quality=quality,
                optimize=False,
                progressive=False,
            )
            return

        if target_format == "png":
            converted = _convert_to_png_compatible(img)
            converted.save(
                output_path,
                format="PNG",
                optimize=False,
                compress_level=3,
            )
            return

        if target_format == "webp":
            converted = _convert_to_webp_compatible(img)
            converted.save(
                output_path,
                format="WEBP",
                quality=quality,
                method=4,
            )
            return

        raise ValueError(f"不支持的目标格式：{target_format}")


def _convert_to_jpg_compatible(img: Image.Image) -> Image.Image:
    """
    JPG 不支持透明通道，因此透明背景转白底。
    """
    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.getchannel("A")
        background.paste(img.convert("RGB"), mask=alpha)
        return background

    if img.mode == "P":
        return img.convert("RGB")

    if img.mode != "RGB":
        return img.convert("RGB")

    return img


def _convert_to_png_compatible(img: Image.Image) -> Image.Image:
    """
    转 PNG。
    """
    if img.mode in ("RGBA", "RGB"):
        return img

    return img.convert("RGBA")


def _convert_to_webp_compatible(img: Image.Image) -> Image.Image:
    """
    转 WEBP。
    """
    if img.mode in ("RGBA", "RGB"):
        return img

    return img.convert("RGBA")


def _build_output_path(path: Path, target_ext: str) -> Path:
    """
    构建输出路径。
    """
    return path.with_suffix(target_ext)


def _build_backup_path(
    root_path: Path,
    backup_root: Path,
    original_path: Path,
) -> Path:
    """
    构建原图备份路径。

    示例：
        商品目录/详情图/001.png

    备份到：
        商品目录/_格式转换备份/详情图/001.png
    """
    try:
        relative_path = original_path.relative_to(root_path)
    except Exception:
        relative_path = Path(original_path.name)

    return backup_root / relative_path


def _avoid_path_conflict(path: Path) -> Path:
    """
    如果目标路径已存在，自动追加序号。
    """
    if not path.exists():
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix

    index = 1

    while True:
        candidate = parent / f"{stem}_{index}{suffix}"

        if not candidate.exists():
            return candidate

        index += 1
