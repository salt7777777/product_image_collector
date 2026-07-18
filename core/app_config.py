import json
from dataclasses import dataclass, asdict
from pathlib import Path


CONFIG_PATH = Path("config.json")


@dataclass
class AppConfig:
    """
    应用配置。

    配置会保存到项目根目录 config.json。
    """

    save_dir: str = str(Path("output").absolute())

    download_main: bool = True
    download_detail: bool = True
    download_sku: bool = True
    high_quality: bool = True

    download_timeout: int = 20
    download_retries: int = 3

    headless: bool = False
    login_wait_seconds: int = 180

    organize_by_date: bool = False
    organize_by_platform: bool = False

    dedupe_images: bool = False

    image_output_format: str = "original"

    # 小图过滤
    filter_small_images: bool = False
    min_image_width: int = 300
    min_image_height: int = 300
    
    # 淘宝/天猫评价图/视频采集
    download_review_media: bool = False
    review_limit: int = 50
    review_include_video: bool = True


    @classmethod
    def load(cls) -> "AppConfig":
        """
        从 config.json 加载配置。

        如果配置文件不存在、损坏，或者字段不完整，则自动使用默认配置。
        """
        if not CONFIG_PATH.exists():
            return cls()

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

            default_config = cls()
            default_data = asdict(default_config)

            merged = {}

            for key, default_value in default_data.items():
                merged[key] = data.get(key, default_value)

            return cls(**merged)

        except Exception:
            return cls()

    def save(self) -> None:
        """
        保存配置到 config.json。
        """
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
