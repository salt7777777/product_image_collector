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


@dataclass
class SmallImageItem:
    """
    小图过滤记录。
    """

    original_path: str
    backup_path: str
    width: int
    height: int
    reason: str


@dataclass
class SmallImageFilterResult:
    """
    小图过滤结果。
    """

    scanned_count: int = 0
    filtered_count: int = 0
    failed_count: int = 0
    items: list[SmallImageItem] = field(default_factory=list)


def filter_small_images(
    root_dir: str | Path,
    min_width: int = 300,
    min_height: int = 300,
    backup_dir_name: str = "_小图过滤",
    exclude_dir_names: set[str] | None = None,
    progress_callback=None,
) -> SmallImageFilterResult:
    """
    过滤小尺寸图片。

    规则：
        如果 width < min_width 或 height < min_height，则移动到 _小图过滤。

    注意：
        不直接删除，避免误处理。
    """

    result = SmallImageFilterResult()

    root_path = Path(root_dir)

    if not root_path.exists():
        return result

    exclude_dir_names = exclude_dir_names or {
        "_重复图片备份",
        "_格式转换备份",
        "_小图过滤",
    }

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

        try:
            width, height = _get_image_size(path)

            if width < min_width or height < min_height:
                reason = f"尺寸过小：{width}x{height}，阈值：{min_width}x{min_height}"

                backup_path = _build_backup_path(
                    root_path=root_path,
                    backup_root=backup_root,
                    original_path=path,
                )
                backup_path = _avoid_path_conflict(backup_path)
                backup_path.parent.mkdir(parents=True, exist_ok=True)

                shutil.move(str(path), str(backup_path))

                result.filtered_count += 1
                result.items.append(
                    SmallImageItem(
                        original_path=str(path),
                        backup_path=str(backup_path),
                        width=width,
                        height=height,
                        reason=reason,
                    )
                )

        except Exception:
            result.failed_count += 1

        _emit_progress(progress_callback, current, total, path)

    return result


def _emit_progress(progress_callback, current: int, total: int, path: Path) -> None:
    if progress_callback:
        try:
            progress_callback(current, total, path)
        except Exception:
            pass


def _is_candidate_image(path: Path, exclude_dir_names: set[str]) -> bool:
    if not path.is_file():
        return False

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return False

    for part in path.parts:
        if part in exclude_dir_names:
            return False

    return True


def _get_image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def _build_backup_path(
    root_path: Path,
    backup_root: Path,
    original_path: Path,
) -> Path:
    try:
        relative_path = original_path.relative_to(root_path)
    except Exception:
        relative_path = Path(original_path.name)

    return backup_root / relative_path


def _avoid_path_conflict(path: Path) -> Path:
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
