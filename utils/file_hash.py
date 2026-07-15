from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import shutil


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".bmp",
}


@dataclass
class DuplicateImageItem:
    """
    重复图片记录。

    original_path:
        被保留的原始文件路径。

    duplicate_path:
        重复文件的原始路径。

    backup_path:
        重复文件被移动到的备份路径。
    """

    original_path: str
    duplicate_path: str
    md5: str
    size: int = 0
    backup_path: str = ""


@dataclass
class ImageDedupeResult:
    """
    图片去重结果。
    """

    scanned_count: int = 0
    removed_count: int = 0
    removed_bytes: int = 0
    duplicate_items: list[DuplicateImageItem] = field(default_factory=list)


def file_md5(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """
    计算文件 MD5。
    """
    path = Path(path)

    md5 = hashlib.md5()

    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)

            if not chunk:
                break

            md5.update(chunk)

    return md5.hexdigest()


def is_image_file(path: str | Path) -> bool:
    """
    判断是否为支持的图片文件。
    """
    path = Path(path)
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def is_probably_valid_image(path: str | Path) -> bool:
    """
    简单判断文件是否像图片文件。

    目的：
    避免把 HTML 错误页、接口错误文本、防盗链文本等伪装成 .jpg/.png 的文件参与去重。

    不依赖 Pillow，只检查常见图片文件头。
    """
    path = Path(path)

    try:
        if not path.is_file():
            return False

        if path.stat().st_size <= 0:
            return False

        with path.open("rb") as f:
            header = f.read(16)

        # JPEG
        if header.startswith(b"\xff\xd8\xff"):
            return True

        # PNG
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return True

        # GIF
        if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            return True

        # WEBP
        if header.startswith(b"RIFF") and b"WEBP" in header:
            return True

        # BMP
        if header.startswith(b"BM"):
            return True

        # AVIF/HEIF 通常包含 ftyp
        if b"ftyp" in header:
            return True

        return False

    except Exception:
        return False


def dedupe_image_files(
    root_dir: str | Path,
    backup_dir_name: str = "_重复图片备份",
    same_folder_only: bool = True,
    move_to_backup: bool = True,
    min_file_size: int = 1024,
) -> ImageDedupeResult:
    """
    对指定目录下的图片文件进行 MD5 去重。

    安全策略：
    1. 默认只在同一目录内去重。
       例如：
           主图/ 内部去重
           详情图/ 内部去重
           SKU图/ 内部去重

       不会把 主图 和 详情图 互相比较。

    2. 默认不直接删除，而是移动到：
           商品目录/_重复图片备份/

    3. 跳过过小文件。
       避免小图标、错误响应、空文件造成异常去重。

    4. 跳过非真实图片文件。
       例如下载失败后保存下来的 HTML 错误页。

    参数：
        root_dir:
            商品目录。

        backup_dir_name:
            重复图片备份目录名。

        same_folder_only:
            True：只在同一个目录里去重，推荐。
            False：整个商品目录全局去重，不推荐。

        move_to_backup:
            True：移动到备份目录。
            False：直接删除。

        min_file_size:
            小于该大小的文件不参与去重，单位字节。
    """
    root_path = Path(root_dir)

    result = ImageDedupeResult()

    if not root_path.exists():
        return result

    backup_root = root_path / backup_dir_name

    image_files = [
        p for p in root_path.rglob("*")
        if is_image_file(p)
        and backup_dir_name not in p.parts
    ]

    # 保证处理顺序稳定
    image_files.sort(key=lambda p: str(p))

    # 分组去重
    #
    # same_folder_only=True 时：
    #   主图、详情图、SKU图分别去重。
    #
    # same_folder_only=False 时：
    #   整个商品目录全局去重。
    groups: dict[str, list[Path]] = {}

    for path in image_files:
        try:
            if path.stat().st_size < min_file_size:
                continue

            if not is_probably_valid_image(path):
                continue

            if same_folder_only:
                group_key = str(path.parent)
            else:
                group_key = "__global__"

            groups.setdefault(group_key, []).append(path)

        except Exception:
            continue

    for _, files in groups.items():
        md5_map: dict[str, Path] = {}

        for path in files:
            if not path.exists():
                continue

            result.scanned_count += 1

            try:
                digest = file_md5(path)
                file_size = path.stat().st_size

                if digest not in md5_map:
                    md5_map[digest] = path
                    continue

                original_path = md5_map[digest]
                duplicate_original_path = path

                backup_path = ""

                if move_to_backup:
                    backup_path_obj = _build_backup_path(
                        root_path=root_path,
                        backup_root=backup_root,
                        duplicate_path=duplicate_original_path,
                    )

                    backup_path_obj.parent.mkdir(parents=True, exist_ok=True)

                    shutil.move(
                        str(duplicate_original_path),
                        str(backup_path_obj),
                    )

                    backup_path = str(backup_path_obj)

                else:
                    duplicate_original_path.unlink()

                result.removed_count += 1
                result.removed_bytes += file_size

                result.duplicate_items.append(
                    DuplicateImageItem(
                        original_path=str(original_path),
                        duplicate_path=str(duplicate_original_path),
                        backup_path=backup_path,
                        md5=digest,
                        size=file_size,
                    )
                )

            except Exception:
                # 单个文件去重失败，不影响整体下载流程
                continue

    return result


def _build_backup_path(
    root_path: Path,
    backup_root: Path,
    duplicate_path: Path,
) -> Path:
    """
    构建重复图片备份路径。

    例如：
        商品目录/详情图/003.jpg

    移动到：
        商品目录/_重复图片备份/详情图/003.jpg

    如果重名，则自动追加序号。
    """
    try:
        relative_path = duplicate_path.relative_to(root_path)
    except Exception:
        relative_path = Path(duplicate_path.name)

    backup_path = backup_root / relative_path

    if not backup_path.exists():
        return backup_path

    stem = backup_path.stem
    suffix = backup_path.suffix
    parent = backup_path.parent

    index = 1

    while True:
        candidate = parent / f"{stem}_{index}{suffix}"

        if not candidate.exists():
            return candidate

        index += 1
