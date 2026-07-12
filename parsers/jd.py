import re
import html as html_lib

from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class JDParser(BaseParser):
    """
    京东商品解析器 - 详情边界识别版。

    当前策略：

    1. 主图：
       根据 debug 文件，主图位于：
       .image-carousel-track.vertical
       只从该区域及明确主图缩略图区域提取。

    2. SKU 图：
       根据 debug 文件，SKU 图位于：
       .specification-item-sku-image

    3. 详情图：
       使用页面视觉边界识别：
       起点：
           商品详情 / 商品介绍 / 图文详情
       终点：
           正品行货 / 权利声明 / 价格说明 / 售后保障 / 包装清单 / 推荐区域

       只抓起点和终点之间的图片。
    """

    def __init__(self, log_callback=None):
        self.browser = BrowserClient(
            headless=False,
            login_wait_seconds=180,
            log_callback=log_callback,
        )

    def parse(self, url: str) -> ProductData:
        """
        解析京东商品。
        """

        platform, product_id = PlatformDetector.detect(url)

        html, rendered_data = self.browser.open_page_and_eval(
            url,
            js_script=self._build_jd_collect_js(),
        )

        soup = BeautifulSoup(html, "lxml")

        title = rendered_data.get("title") or self._parse_title_from_html(soup)
        title = self._clean_title(title)

        main_urls = rendered_data.get("main_images") or []
        sku_items = rendered_data.get("sku_images") or []
        detail_urls = rendered_data.get("detail_images") or []

        main_images = self._build_main_images(main_urls)
        sku_images = self._build_sku_images(sku_items)
        detail_images = self._build_detail_images(
            detail_urls,
            source="jd_detail_boundary",
        )

        if not detail_images:
            self._log("京东页面未识别到准确详情图。可能该商品无图文详情，或详情图未暴露在当前页面 DOM。")

        self._log(f"京东商品标题：{title}")
        self._log(f"京东主图识别：{len(main_images)} 张")
        self._log(f"京东详情图识别：{len(detail_images)} 张")
        self._log(f"京东SKU图识别：{len(sku_images)} 张")

        return ProductData(
            platform=platform,
            product_id=product_id,
            title=title,
            url=url,
            main_images=main_images,
            detail_images=detail_images,
            sku_images=sku_images,
        )

    def _build_jd_collect_js(self) -> str:
        """
        京东页面 DOM 精准提取 JS。
        """

        return r"""
        () => {
            const result = {
                title: "",
                main_images: [],
                sku_images: [],
                detail_images: []
            };

            const cleanUrl = (url) => {
                if (!url) return "";

                url = String(url).trim();

                if (url.startsWith("//")) {
                    url = "https:" + url;
                }

                return url;
            };

            const isImageUrl = (url) => {
                if (!url) return false;

                const lower = String(url).toLowerCase();

                return (
                    lower.includes(".jpg") ||
                    lower.includes(".jpeg") ||
                    lower.includes(".png") ||
                    lower.includes(".webp") ||
                    lower.includes(".avif")
                );
            };

            const isJdImage = (url) => {
                if (!url) return false;
                return String(url).toLowerCase().includes("360buyimg.com");
            };

            const getImgUrl = (img) => {
                if (!img) return "";

                const attrs = [
                    "data-origin",
                    "data-url",
                    "data-lazy-img",
                    "data-lazyload",
                    "data-src",
                    "data-original",
                    "src"
                ];

                for (const attr of attrs) {
                    const val = img.getAttribute(attr);
                    if (val) return cleanUrl(val);
                }

                const srcset = img.getAttribute("srcset");

                if (srcset) {
                    const first = srcset.split(",")[0].trim().split(" ")[0];
                    if (first) return cleanUrl(first);
                }

                return "";
            };

            const getBgUrl = (el) => {
                if (!el) return "";

                const style = window.getComputedStyle(el);
                const bg = style && style.backgroundImage ? style.backgroundImage : "";

                if (!bg || bg === "none") return "";

                const match = bg.match(/url\(["']?(.*?)["']?\)/);

                if (match && match[1]) {
                    return cleanUrl(match[1]);
                }

                return "";
            };

            const addUnique = (arr, url) => {
                url = cleanUrl(url);

                if (!url) return;
                if (!isImageUrl(url)) return;
                if (!isJdImage(url)) return;

                if (!arr.includes(url)) {
                    arr.push(url);
                }
            };

            const getClassText = (el) => {
                if (!el) return "";

                const p = el.parentElement;
                const g = p ? p.parentElement : null;

                return [
                    el.className || "",
                    el.id || "",
                    p ? p.className || "" : "",
                    p ? p.id || "" : "",
                    g ? g.className || "" : "",
                    g ? g.id || "" : ""
                ].join(" ").toLowerCase();
            };

            const isNoiseByClass = (el) => {
                const text = getClassText(el);

                const badWords = [
                    "recommend",
                    "comment",
                    "evaluate",
                    "shop",
                    "store",
                    "seller",
                    "logo",
                    "icon",
                    "arrow",
                    "play",
                    "video",
                    "service",
                    "promise",
                    "badge",
                    "medal",
                    "kefu",
                    "customer",
                    "dongdong",
                    "calculator",
                    "elevator",
                    "lachine",
                    "try",
                    "trial",
                    "certificate",
                    "qualification"
                ];

                return badWords.some(w => text.includes(w));
            };

            // ------------------------------------------------------------
            // 1. 标题
            // ------------------------------------------------------------

            const titleSelectors = [
                ".sku-title-name",
                ".sku-name",
                ".itemInfo-wrap .sku-name",
                ".product-intro .sku-name"
            ];

            for (const selector of titleSelectors) {
                const el = document.querySelector(selector);

                if (el && el.innerText && el.innerText.trim()) {
                    const text = el.innerText.trim();

                    if (
                        text.length >= 5 &&
                        !text.includes("最小单价") &&
                        !text.includes("计算器") &&
                        !text.includes("客服") &&
                        !text.includes("店铺")
                    ) {
                        result.title = text;
                        break;
                    }
                }
            }

            if (!result.title) {
                const meta = document.querySelector('meta[property="og:title"]');

                if (meta && meta.content) {
                    result.title = meta.content.trim();
                }
            }

            if (!result.title && document.title) {
                result.title = document.title;
            }

            // ------------------------------------------------------------
            // 2. 主图
            // ------------------------------------------------------------

            const mainRoots = Array.from(
                document.querySelectorAll(".image-carousel-track.vertical")
            );

            mainRoots.forEach(root => {
                root.querySelectorAll("img").forEach(img => {
                    if (isNoiseByClass(img)) return;

                    const url = getImgUrl(img);
                    addUnique(result.main_images, url);
                });
            });

            if (result.main_images.length === 0) {
                document.querySelectorAll(".preview-wrap .image-carousel-track img").forEach(img => {
                    if (isNoiseByClass(img)) return;

                    const url = getImgUrl(img);
                    addUnique(result.main_images, url);
                });
            }

            if (result.main_images.length === 0) {
                const selectors = [
                    "#spec-list img",
                    ".spec-list img",
                    ".spec-items img",
                    ".preview-list img"
                ];

                selectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(img => {
                        if (isNoiseByClass(img)) return;

                        const url = getImgUrl(img);
                        addUnique(result.main_images, url);
                    });
                });
            }

            result.main_images = result.main_images.slice(0, 12);

            // ------------------------------------------------------------
            // 3. SKU 图
            // ------------------------------------------------------------

            const skuMap = new Map();

            document.querySelectorAll(".specification-item-sku-image").forEach(img => {
                const url = getImgUrl(img);

                if (!url) return;
                if (!isImageUrl(url)) return;
                if (!isJdImage(url)) return;

                let skuName = "";

                const parent = img.closest(".specification-item-sku, li, a, div");

                if (parent && parent.innerText) {
                    skuName = parent.innerText.trim();
                }

                const key = url + "|" + skuName;

                if (!skuMap.has(key)) {
                    skuMap.set(key, {
                        url: url,
                        sku_name: skuName
                    });
                }
            });

            if (skuMap.size === 0) {
                const skuSelectors = [
                    "#choose-attrs img",
                    ".choose-attrs img",
                    ".choose-attr img",
                    ".choose-color img",
                    ".summary-attrs img",
                    "[class*='specification'] img",
                    "[class*='sku'] img"
                ];

                skuSelectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(img => {
                        const url = getImgUrl(img);

                        if (!url) return;
                        if (!isImageUrl(url)) return;
                        if (!isJdImage(url)) return;
                        if (isNoiseByClass(img)) return;

                        let skuName = "";

                        const parent = img.closest("li, a, div, span");

                        if (parent && parent.innerText) {
                            skuName = parent.innerText.trim();
                        }

                        const key = url + "|" + skuName;

                        if (!skuMap.has(key)) {
                            skuMap.set(key, {
                                url: url,
                                sku_name: skuName
                            });
                        }
                    });
                });
            }

            result.sku_images = Array.from(skuMap.values()).slice(0, 30);

            // ------------------------------------------------------------
            // 4. 详情图：基于页面视觉边界识别
            // ------------------------------------------------------------

            const getAbsRect = (el) => {
                const rect = el.getBoundingClientRect();

                return {
                    top: rect.top + window.scrollY,
                    bottom: rect.bottom + window.scrollY,
                    left: rect.left + window.scrollX,
                    right: rect.right + window.scrollX,
                    width: rect.width,
                    height: rect.height
                };
            };

            const getText = (el) => {
                if (!el) return "";
                return el.innerText ? el.innerText.trim() : "";
            };

            const findDetailStartY = () => {
                const candidates = [];

                document.querySelectorAll("*").forEach(el => {
                    const text = getText(el);
                    const id = el.id || "";
                    const cls = el.className ? String(el.className) : "";

                    if (!text && !id && !cls) return;

                    const shortText = text.length <= 30;

                    const hitByText =
                        shortText && (
                            text === "商品详情" ||
                            text === "商品介绍" ||
                            text === "图文详情" ||
                            text.includes("商品详情") ||
                            text.includes("商品介绍") ||
                            text.includes("图文详情")
                        );

                    const hitById =
                        id.includes("SPXQ") ||
                        id === "detail" ||
                        id === "J-detail" ||
                        id === "J-detail-content";

                    const hitByClass =
                        cls === "detail-content" ||
                        cls.includes("product-detail") ||
                        cls.includes("ssd-module-wrap");

                    if (hitByText || hitById || hitByClass) {
                        const rect = getAbsRect(el);

                        if (
                            rect.top > 500 &&
                            rect.width > 20 &&
                            rect.height > 5
                        ) {
                            candidates.push({
                                y: rect.top,
                                text,
                                id,
                                cls
                            });
                        }
                    }
                });

                candidates.sort((a, b) => a.y - b.y);

                const exact = candidates.find(item =>
                    item.text.includes("商品详情") ||
                    item.text.includes("商品介绍") ||
                    item.text.includes("图文详情") ||
                    item.id.includes("SPXQ")
                );

                if (exact) return exact.y;

                if (candidates.length > 0) {
                    return candidates[0].y;
                }

                return 0;
            };

            const findDetailEndY = (startY) => {
                const endKeywords = [
                    "正品行货",
                    "权利声明",
                    "价格说明",
                    "售后保障",
                    "包装清单",
                    "店铺推荐",
                    "猜你喜欢",
                    "为你推荐",
                    "商品评价",
                    "买家印象"
                ];

                const candidates = [];

                document.querySelectorAll("*").forEach(el => {
                    const text = getText(el);

                    if (!text) return;
                    if (text.length > 50) return;

                    if (endKeywords.some(keyword => text.includes(keyword))) {
                        const rect = getAbsRect(el);

                        if (
                            rect.top > startY + 100 &&
                            rect.width > 20 &&
                            rect.height > 5
                        ) {
                            candidates.push({
                                y: rect.top,
                                text
                            });
                        }
                    }
                });

                candidates.sort((a, b) => a.y - b.y);

                if (candidates.length > 0) {
                    return candidates[0].y;
                }

                return startY + 8000;
            };

            const startY = findDetailStartY();
            const endY = startY > 0 ? findDetailEndY(startY) : 0;

            const isInDetailRange = (el) => {
                if (!startY || !endY) return false;

                const rect = getAbsRect(el);
                const centerY = rect.top + rect.height / 2;

                if (centerY <= startY) return false;
                if (centerY >= endY) return false;

                // 排除右侧浮动栏、客服栏、导航栏
                if (rect.left < 0) return false;
                if (rect.left > window.innerWidth - 80) return false;

                // 详情图一般不会太小
                if (rect.width < 120 && rect.height < 120) return false;

                return true;
            };

            const isBadDetailUrl = (url) => {
                if (!url) return true;

                const lower = String(url).toLowerCase();

                const badWords = [
                    "logo",
                    "icon",
                    "sprite",
                    "avatar",
                    "qrcode",
                    "qr-code",
                    "shop",
                    "store",
                    "seller",
                    "recommend",
                    "comment",
                    "evaluate",
                    "service",
                    "promise",
                    "badge",
                    "medal",
                    "blank",
                    "loading",
                    "transparent",
                    "arrow",
                    "play",
                    "pause",
                    "video",
                    "customer",
                    "kefu",
                    "consult",
                    "dongdong",
                    "smile",
                    "face",
                    "star",
                    "rate",
                    "score",
                    "coupon",
                    "gift",
                    "imagetools",
                    "shaidan",
                    "default.image",
                    "popshop",
                    "elevator",
                    "lachine",
                    "calculator",
                    "certificate",
                    "qualification"
                ];

                return badWords.some(word => lower.includes(word));
            };

            const addDetailImage = (url, el) => {
                url = cleanUrl(url);

                if (!url) return;
                if (!isImageUrl(url)) return;
                if (!isJdImage(url)) return;
                if (isBadDetailUrl(url)) return;
                if (isNoiseByClass(el)) return;
                if (!isInDetailRange(el)) return;

                if (!result.detail_images.includes(url)) {
                    result.detail_images.push(url);
                }
            };

            // 从详情边界范围内抓 img
            document.querySelectorAll("img").forEach(img => {
                const url = getImgUrl(img);
                addDetailImage(url, img);
            });

            // 从详情边界范围内抓 background-image
            document.querySelectorAll("*").forEach(el => {
                const bg = getBgUrl(el);
                addDetailImage(bg, el);
            });

            result.detail_images = result.detail_images.slice(0, 100);

            return result;
        }
        """

    # ------------------------------------------------------------------
    # 标题处理
    # ------------------------------------------------------------------

    def _parse_title_from_html(self, soup: BeautifulSoup) -> str:
        selectors = [
            ".sku-title-name",
            ".sku-name",
            ".itemInfo-wrap .sku-name",
            ".product-intro .sku-name",
        ]

        for selector in selectors:
            tag = soup.select_one(selector)

            if tag:
                text = tag.get_text(" ", strip=True)

                if text and "最小单价" not in text and "计算器" not in text:
                    return text

        if soup.title:
            return soup.title.get_text(" ", strip=True)

        return ""

    def _clean_title(self, title: str) -> str:
        if not title:
            return "京东商品"

        title = title.strip()

        remove_parts = [
            "【行情 报价 价格 评测】",
            "【图片 价格 品牌 报价】",
            "京东",
            "JD.COM",
            "- 京东",
            "-京东",
            "_京东",
            "京东JD.COM",
        ]

        for part in remove_parts:
            title = title.replace(part, "")

        title = re.sub(r"\s+", " ", title).strip()

        if len(title) > 120:
            title = title[:120].strip()

        if "最小单价" in title or "计算器" in title:
            return "京东商品"

        return title or "京东商品"

    # ------------------------------------------------------------------
    # 主图
    # ------------------------------------------------------------------

    def _build_main_images(self, urls: list[str]) -> list[ImageItem]:
        normalized = []

        for url in urls:
            url = self._normalize_jd_image_url(url, image_type="main")

            if url:
                normalized.append(url)

        normalized = dedupe_urls(normalized)
        normalized = self._filter_main_images(normalized)
        normalized = normalized[:12]

        return [
            ImageItem(
                url=u,
                image_type="main",
                ext=get_url_ext(u),
                source="jd_main_precise",
            )
            for u in normalized
        ]

    def _filter_main_images(self, urls: list[str]) -> list[str]:
        result = []

        for url in urls:
            lower = url.lower()

            if not self._is_valid_image_url(lower):
                continue

            if self._is_noise_image(lower):
                continue

            if "360buyimg.com" not in lower:
                continue

            result.append(url)

        return dedupe_urls(result)

    # ------------------------------------------------------------------
    # SKU 图
    # ------------------------------------------------------------------

    def _build_sku_images(self, sku_items: list[dict]) -> list[ImageItem]:
        result = []
        seen = set()

        for item in sku_items:
            raw_url = item.get("url", "")
            sku_name = item.get("sku_name", "")

            url = self._normalize_jd_image_url(raw_url, image_type="sku")

            if not url:
                continue

            key = url + "|" + sku_name

            if key in seen:
                continue

            seen.add(key)

            lower = url.lower()

            if not self._is_valid_image_url(lower):
                continue

            if self._is_noise_image(lower):
                continue

            if "360buyimg.com" not in lower:
                continue

            result.append(
                ImageItem(
                    url=url,
                    image_type="sku",
                    ext=get_url_ext(url),
                    sku_name=sku_name,
                    source="jd_sku_precise",
                )
            )

        return result

    # ------------------------------------------------------------------
    # 详情图
    # ------------------------------------------------------------------

    def _build_detail_images(self, urls: list[str], source: str) -> list[ImageItem]:
        normalized = []

        for url in urls:
            url = self._normalize_jd_image_url(url, image_type="detail")

            if url:
                normalized.append(url)

        normalized = dedupe_urls(normalized)
        normalized = self._filter_detail_images(normalized)

        return [
            ImageItem(
                url=u,
                image_type="detail",
                ext=get_url_ext(u),
                source=source,
            )
            for u in normalized
        ]

    def _filter_detail_images(self, urls: list[str]) -> list[str]:
        """
        京东详情图严格过滤。

        详情图边界主要由 JS 根据页面坐标判断；
        Python 这里做二次 URL 过滤。
        """

        result = []

        for url in urls:
            if not url:
                continue

            lower = url.lower()

            if not self._is_valid_image_url(lower):
                continue

            if "360buyimg.com" not in lower:
                continue

            if self._is_noise_image(lower):
                continue

            if "shaidan" in lower:
                continue

            if "default.image" in lower:
                continue

            if "imagetools" in lower:
                continue

            if "popshop" in lower:
                continue

            if "lachine" in lower:
                continue

            if "elevator" in lower:
                continue

            allowed_signals = [
                "/imgzone/",
                "/sku/",
                "/cms/",
                "/jfs/",
                "/pcpubliccms/",
            ]

            if not any(signal in lower for signal in allowed_signals):
                continue

            result.append(url)

        return dedupe_urls(result)

    # ------------------------------------------------------------------
    # URL 与过滤
    # ------------------------------------------------------------------

    def _normalize_jd_image_url(self, url: str, image_type: str = "main") -> str:
        if not url:
            return ""

        url = str(url).strip()
        url = url.strip("'\"")
        url = url.replace("\\/", "/")
        url = html_lib.unescape(url)

        if url.startswith("//"):
            url = "https:" + url

        # 避免 .jpg.avif 保存后扩展名混乱
        url = url.replace(".jpg.avif", ".jpg")
        url = url.replace(".jpeg.avif", ".jpeg")
        url = url.replace(".png.avif", ".png")

        # pcpubliccms 缩略图转原路径
        url = re.sub(
            r"/pcpubliccms/s\d+x\d+_jfs/",
            "/pcpubliccms/jfs/",
            url,
            flags=re.I,
        )

        # /n5/s54x54_jfs/... -> /n1/jfs/...
        url = re.sub(
            r"/n\d+/s\d+x\d+_jfs/",
            "/n1/jfs/",
            url,
            flags=re.I,
        )

        if image_type in ["main", "sku"]:
            url = re.sub(r"/n\d+/", "/n1/", url, count=1)

        if url.startswith("/jfs/"):
            url = url.lstrip("/")
            return self._build_jd_img_url(url, image_type=image_type)

        if url.startswith("jfs/"):
            return self._build_jd_img_url(url, image_type=image_type)

        if url.startswith("/t1/"):
            url = "jfs" + url
            return self._build_jd_img_url(url, image_type=image_type)

        if url.startswith("t1/"):
            url = "jfs/" + url
            return self._build_jd_img_url(url, image_type=image_type)

        return normalize_image_url(url)

    def _build_jd_img_url(self, path: str, image_type: str = "main") -> str:
        path = path.lstrip("/")

        if image_type == "detail":
            return f"https://img30.360buyimg.com/imgzone/{path}"

        return f"https://img13.360buyimg.com/n1/{path}"

    def _is_valid_image_url(self, url: str) -> bool:
        if not url:
            return False

        lower = url.lower()

        return any(
            ext in lower
            for ext in [".jpg", ".jpeg", ".png", ".webp", ".avif"]
        )

    def _is_noise_image(self, url: str) -> bool:
        """
        判断是否明显是无关图片。

        注意：
        不使用 'ad'、'free' 这种短词过滤，
        避免误伤正常商品图 hash。
        """

        lower = url.lower()

        blacklist = [
            "logo",
            "icon",
            "sprite",
            "avatar",
            "qrcode",
            "qr-code",
            "shop",
            "store",
            "seller",
            "banner",
            "recommend",
            "comment",
            "evaluate",
            "service",
            "promise",
            "badge",
            "medal",
            "blank",
            "loading",
            "transparent",
            "arrow",
            "play",
            "pause",
            "video",
            "customer",
            "kefu",
            "consult",
            "dongdong",
            "smile",
            "face",
            "star",
            "rate",
            "score",
            "coupon",
            "gift",
            "imagetools",
            "shaidan",
            "default.image",
            "popshop",
            "elevator",
            "lachine",
            "calculator",
            "certificate",
            "qualification",
        ]

        return any(keyword in lower for keyword in blacklist)

    def _log(self, message: str):
        try:
            if hasattr(self.browser, "log_callback") and self.browser.log_callback:
                self.browser.log_callback(message)
        except Exception:
            pass
