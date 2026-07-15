import re
import html as html_lib
import httpx

from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class JDParser(BaseParser):
    """
    京东商品解析器。

    当前策略：
    1. 主图：从京东页面 DOM 中提取。
    2. SKU 图：从规格区域中的 SKU 图片提取。
    3. 详情图：
       - 优先从 Playwright 捕获到的 pc_item_getWareGraphic 接口响应中提取；
       - 传统详情接口作为兜底；
       - 自动过滤错误页素材、资质证照、推荐商品、评论图片、UI 图标等；
       - 自动清理 JSON/JSONP 转义 URL；
       - 自动去重。
    """

    def __init__(
        self,
        log_callback=None,
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
       self.browser = BrowserClient(
            headless=headless,
            login_wait_seconds=login_wait_seconds,
            log_callback=log_callback,
        )


    def parse(self, url: str) -> ProductData:
        """
        解析京东商品。
        """

        platform, product_id = PlatformDetector.detect(url)

        html, rendered_data, network_texts = self.browser.open_page_and_eval(
            url,
            js_script=self._build_jd_collect_js(),
            collect_network=True,
        )

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        title = rendered_data.get("title") or self._parse_title_from_html(soup)
        title = self._clean_title(title)

        main_urls = rendered_data.get("main_images") or []
        sku_items = rendered_data.get("sku_images") or []
        dom_detail_urls = rendered_data.get("detail_images") or []

        api_detail_urls = self._fetch_jd_detail_images_from_api(product_id, url)
        network_detail_urls = self._extract_detail_images_from_network_texts(network_texts)

        detail_urls = dedupe_urls(dom_detail_urls + api_detail_urls + network_detail_urls)

        main_images = self._build_main_images(main_urls)
        sku_images = self._build_sku_images(sku_items)
        detail_images = self._build_detail_images(
            detail_urls,
            source="jd_detail_graphic",
        )

        if not detail_images:
            self._log("京东页面未识别到详情图。可能该商品无图文详情，或当前接口未返回有效图片。")

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
        京东页面 DOM 提取 JS。

        负责：
        1. 标题；
        2. 主图；
        3. SKU 图；
        4. DOM 中已出现的详情图兜底。
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

                if (!url) return "";

                url = url.replace(/\\\//g, "/");

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

                const lower = String(url).toLowerCase();

                return (
                    lower.includes("360buyimg.com") ||
                    lower.includes("jdimg.com")
                );
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
                    "data-img",
                    "src"
                ];

                for (const attr of attrs) {
                    const val = img.getAttribute(attr);

                    if (val) {
                        return cleanUrl(val);
                    }
                }

                const srcset = img.getAttribute("srcset");

                if (srcset) {
                    const first = srcset.split(",")[0].trim().split(" ")[0];

                    if (first) {
                        return cleanUrl(first);
                    }
                }

                return "";
            };

            const getBgUrls = (el) => {
                if (!el) return [];

                const urls = [];
                const styleAttr = el.getAttribute("style") || "";
                const computedStyle = window.getComputedStyle(el);
                const bg = computedStyle && computedStyle.backgroundImage ? computedStyle.backgroundImage : "";

                [styleAttr, bg].forEach(text => {
                    if (!text) return;

                    const reg = /url\(["']?(.*?)["']?\)/g;
                    let m;

                    while ((m = reg.exec(text)) !== null) {
                        if (m[1]) {
                            urls.push(cleanUrl(m[1]));
                        }
                    }
                });

                return urls;
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
                    "qualification",
                    "license",
                    "permit",
                    "error-new"
                ];

                return badWords.some(w => text.includes(w));
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
                    "certification",
                    "qualification",
                    "license",
                    "licence",
                    "permit",
                    "businesslicense",
                    "business-license",
                    "aptitude",
                    "recordal",
                    "record",
                    "beian",
                    "icp",
                    "yyzz",
                    "wenwangwen",
                    "error-new",
                    "try_03",
                    "try1_07",
                    "yinying_06",
                    "error_06"
                ];

                return badWords.some(word => lower.includes(word));
            };

            // 标题
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

            // 主图
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

            // SKU 图
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

            // DOM 详情图兜底：只从明显详情模块中提取
            const detailSelectors = [
                ".ssd-module-wrap",
                ".ssd-module",
                "[class*='ssd-module']",
                "[id*='ssd']",
                "[class*='detail-content']",
                "[class*='product-detail']"
            ];

            const addDetailImage = (url, el) => {
                url = cleanUrl(url);

                if (!url) return;
                if (!isImageUrl(url)) return;
                if (!isJdImage(url)) return;
                if (isBadDetailUrl(url)) return;
                if (el && isNoiseByClass(el)) return;

                if (!result.detail_images.includes(url)) {
                    result.detail_images.push(url);
                }
            };

            detailSelectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(root => {
                    if (isNoiseByClass(root)) return;

                    root.querySelectorAll("img").forEach(img => {
                        const url = getImgUrl(img);
                        addDetailImage(url, img);
                    });

                    root.querySelectorAll("*").forEach(el => {
                        const bgs = getBgUrls(el);

                        bgs.forEach(bg => {
                            addDetailImage(bg, el);
                        });

                        const attrs = [
                            "src",
                            "data-src",
                            "data-lazyload",
                            "data-original",
                            "data-url",
                            "data-origin",
                            "data-lazy-img",
                            "data-img"
                        ];

                        attrs.forEach(attr => {
                            const val = el.getAttribute(attr);

                            if (val) {
                                addDetailImage(val, el);
                            }
                        });
                    });
                });
            });

            result.detail_images = result.detail_images.slice(0, 120);

            return result;
        }
        """

    # ------------------------------------------------------------------
    # 网络响应详情图提取
    # ------------------------------------------------------------------

    def _extract_detail_images_from_network_texts(self, network_texts: list[dict]) -> list[str]:
        """
        从 Playwright 捕获到的网络响应中提取京东详情图。

        只分析当前商品图文详情接口 pc_item_getWareGraphic。
        """

        if not network_texts:
            return []

        graphic_items = []

        for item in network_texts:
            response_url = item.get("url", "")
            text = item.get("text", "")

            if not response_url or not text:
                continue

            lower_url = response_url.lower()

            if (
                "functionid=pc_item_getwaregraphic" in lower_url
                or "pc_item_getwaregraphic" in lower_url
            ):
                graphic_items.append(item)

        if not graphic_items:
            return []

        all_urls = []

        for item in graphic_items:
            text = item.get("text", "")

            try:
                urls = self._extract_jd_images_from_text(text)
                urls = self._filter_detail_like_urls(urls)
                urls = [
                    u for u in urls
                    if not self._is_bad_network_candidate(u)
                ]

                all_urls.extend(dedupe_urls(urls))

            except Exception:
                continue

        return dedupe_urls(all_urls)

    def _filter_detail_like_urls(self, urls: list[str]) -> list[str]:
        """
        过滤网络响应中提取出来的 URL，只保留可能是当前商品详情图的图片。
        """

        result = []

        for url in urls:
            if not url:
                continue

            url = self._normalize_jd_image_url(url, image_type="detail")

            if not url:
                continue

            if not url.startswith("http://") and not url.startswith("https://"):
                continue

            lower = url.lower()

            if self._is_bad_network_candidate(lower):
                continue

            if self._is_noise_image(lower):
                continue

            if "360buyimg.com" not in lower and "jdimg.com" not in lower:
                continue

            if not self._is_valid_image_url(lower):
                continue

            bad_path_signals = [
                "storage.360buyimg.com",
                "static.360buyimg.com",
                "/devfe/",
                "/error-new/",
                "/static/",
                "/logo/",
                "/icon/",
                "/sprite/",
                "/avatar/",
                "/comment/",
                "/shaidan/",
                "/popshop/",
                "relsearch",
                "diviner",
                "mixer",
            ]

            if any(signal in lower for signal in bad_path_signals):
                continue

            good_path_signals = [
                "/imgzone/",
                "/sku/",
                "/cms/",
                "/jfs/",
                "/pcpubliccms/",
                "/image/",
                "/n1/",
                "/n0/",
                "/ssd/",
                "/desc/",
                "/detail/",
            ]

            if not any(signal in lower for signal in good_path_signals):
                continue

            result.append(url)

        return dedupe_urls(result)

    def _is_bad_network_candidate(self, url: str) -> bool:
        """
        过滤网络响应中提取到的明显无关图片。
        """

        if not url:
            return True

        lower = url.lower()

        bad_keywords = [
            "storage.360buyimg.com",
            "static.360buyimg.com/devfe",
            "error-new",
            "/error",
            "try_03",
            "try1_07",
            "yinying_06",
            "error_06",
            "blank",
            "loading",
            "transparent",
            "logo",
            "icon",
            "sprite",
            "qrcode",
            "qr-code",
            "favicon",
            "passport",
            "login",
            "captcha",
            "comment",
            "evaluate",
            "recommend",
            "relsearch",
            "diviner",
            "mixer",
            "shaidan",
            "popshop",
            "shop",
            "store",
            "seller",
            "service",
            "promise",
            "badge",
            "medal",
            "customer",
            "kefu",
            "dongdong",
            "certificate",
            "certification",
            "qualification",
            "license",
            "licence",
            "permit",
            "businesslicense",
            "business-license",
            "aptitude",
            "recordal",
            "record",
            "beian",
            "icp",
            "yyzz",
            "wenwangwen",
        ]

        return any(k in lower for k in bad_keywords)

    # ------------------------------------------------------------------
    # 京东传统详情接口
    # ------------------------------------------------------------------

    def _fetch_jd_detail_images_from_api(self, product_id: str, product_url: str) -> list[str]:
        """
        使用 Python 请求京东传统详情接口，作为兜底方案。
        """

        if not product_id:
            return []

        api_urls = [
            f"https://cd.jd.com/description/channel?skuId={product_id}&mainSkuId={product_id}&charset=utf-8&cdn=2",
            f"https://cd.jd.com/description/channel?skuId={product_id}&mainSkuId={product_id}&charset=utf-8",
            f"https://cd.jd.com/description/channel?skuId={product_id}&charset=utf-8&cdn=2",
            f"https://cd.jd.com/description/channel?skuId={product_id}&charset=utf-8",
            f"https://dx.3.cn/desc/{product_id}?cdn=2",
        ]

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": product_url,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

        all_urls = []

        for api_url in api_urls:
            try:
                with httpx.Client(
                    headers=headers,
                    timeout=15,
                    follow_redirects=True,
                    verify=False,
                ) as client:
                    response = client.get(api_url)

                if response.status_code != 200:
                    continue

                text = response.text or ""

                if len(text) < 50:
                    continue

                urls = self._extract_jd_images_from_text(text)
                urls = self._filter_detail_like_urls(urls)
                urls = [
                    u for u in urls
                    if not self._is_bad_network_candidate(u)
                ]

                if urls:
                    all_urls.extend(urls)
                    break

            except Exception:
                continue

        return dedupe_urls(all_urls)

    def _extract_jd_images_from_text(self, text: str) -> list[str]:
        """
        从京东接口 / 网络响应文本中提取图片 URL。
        """

        if not text:
            return []

        text = str(text)

        text = text.replace("\\u003c", "<")
        text = text.replace("\\u003C", "<")
        text = text.replace("\\u003e", ">")
        text = text.replace("\\u003E", ">")
        text = text.replace("\\u002f", "/")
        text = text.replace("\\u002F", "/")
        text = text.replace("\\/", "/")

        text = html_lib.unescape(text)

        urls = []

        def add_url_if_valid(raw_url: str, context: str = ""):
            if not raw_url:
                return

            if self._is_bad_detail_context(context):
                return

            url = self._normalize_jd_image_url(raw_url, image_type="detail")

            if not url:
                return

            if not url.startswith("http://") and not url.startswith("https://"):
                return

            lower = url.lower()

            if "360buyimg.com" not in lower and "jdimg.com" not in lower:
                return

            if not self._is_valid_image_url(lower):
                return

            if self._is_noise_image(lower):
                return

            if self._is_bad_network_candidate(lower):
                return

            urls.append(url)

        patterns = [
            r'(?:https?:)?//[^\'"<>\\\s]+?(?:360buyimg\.com|jdimg\.com)/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                raw_url = match.group(0)

                start = max(0, match.start() - 300)
                end = min(len(text), match.end() + 300)
                context = text[start:end]

                add_url_if_valid(raw_url, context=context)

        relative_patterns = [
            r'(?<![a-zA-Z0-9_/])jfs/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])/jfs/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])t1/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])/t1/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
        ]

        for pattern in relative_patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                raw_url = match.group(0)

                start = max(0, match.start() - 300)
                end = min(len(text), match.end() + 300)
                context = text[start:end]

                add_url_if_valid(raw_url, context=context)

        try:
            soup = BeautifulSoup(text, "lxml")
        except Exception:
            soup = BeautifulSoup(text, "html.parser")

        img_attrs = [
            "src",
            "data-src",
            "data-lazyload",
            "data-original",
            "data-url",
            "data-origin",
            "data-lazy-img",
            "data-img",
        ]

        for img in soup.find_all("img"):
            parent = img.parent
            context = ""

            if parent:
                context = parent.get_text(" ", strip=True)

                grand = parent.parent
                if grand:
                    context += " " + grand.get_text(" ", strip=True)

            if self._is_bad_detail_context(context):
                continue

            for attr in img_attrs:
                value = img.get(attr)

                if value:
                    add_url_if_valid(value, context=context)

        for tag in soup.find_all(True):
            context = tag.get_text(" ", strip=True)

            parent = tag.parent
            if parent:
                context += " " + parent.get_text(" ", strip=True)

            if self._is_bad_detail_context(context):
                continue

            for attr, value in tag.attrs.items():
                if not isinstance(value, str):
                    continue

                if (
                    "360buyimg.com" not in value
                    and "jdimg.com" not in value
                    and "jfs/" not in value
                    and "/jfs/" not in value
                    and "t1/" not in value
                    and "/t1/" not in value
                ):
                    continue

                found_urls = self._extract_urls_from_possible_attr(value)

                for found_url in found_urls:
                    add_url_if_valid(found_url, context=context)

        for tag in soup.find_all(True):
            context = tag.get_text(" ", strip=True)

            parent = tag.parent
            if parent:
                context += " " + parent.get_text(" ", strip=True)

            if self._is_bad_detail_context(context):
                continue

            style = tag.get("style") or ""

            if not style:
                continue

            for match in re.findall(r"url\([\"']?(.*?)[\"']?\)", style, flags=re.I):
                add_url_if_valid(match, context=context)

        return dedupe_urls(urls)

    def _extract_urls_from_possible_attr(self, value: str) -> list[str]:
        """
        从属性值中提取可能的京东图片链接。
        """

        if not value:
            return []

        value = str(value)
        value = html_lib.unescape(value)
        value = value.replace("\\/", "/")
        value = value.replace('\\"', '"')
        value = value.replace("\\'", "'")

        urls = []

        patterns = [
            r'(?:https?:)?//[^\'"<>\\\s]+?(?:360buyimg\.com|jdimg\.com)/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])jfs/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])/jfs/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])t1/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
            r'(?<![a-zA-Z0-9_/])/t1/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)(?:![^\'"<>\\\s]*)?',
        ]

        for pattern in patterns:
            for match in re.findall(pattern, value, flags=re.I):
                urls.append(match)

        return urls

    def _is_bad_detail_context(self, text: str) -> bool:
        """
        判断图片附近文本是否属于资质、证照、备案、许可证等非商品详情内容。
        """

        if not text:
            return False

        text = str(text).lower()

        bad_keywords = [
            "网络文化经营许可证",
            "增值电信业务经营许可证",
            "营业执照",
            "食品经营许可证",
            "医疗器械经营许可证",
            "出版物经营许可证",
            "互联网药品信息服务资格证书",
            "开户许可证",
            "许可证",
            "经营许可证",
            "资质证照",
            "证照信息",
            "商家资质",
            "品牌授权",
            "授权书",
            "授权证书",
            "备案",
            "icp",
            "京公网安备",
            "营业执照信息",
            "企业资质",
            "资质信息",
            "证书编号",
            "经营者",
            "统一社会信用代码",
            "法定代表人",
            "登记机关",
            "核准日期",
            "有效期",
            "许可范围",
            "license",
            "licence",
            "permit",
            "certificate",
            "certification",
            "qualification",
            "businesslicense",
            "business-license",
            "record",
            "recordal",
            "beian",
            "aptitude",
        ]

        return any(keyword.lower() in text for keyword in bad_keywords)

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

            if "360buyimg.com" not in lower and "jdimg.com" not in lower:
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

            if "360buyimg.com" not in lower and "jdimg.com" not in lower:
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

            if not url:
                continue

            if not url.startswith("http://") and not url.startswith("https://"):
                continue

            normalized.append(url)

        normalized = dedupe_urls(normalized)
        normalized = self._filter_detail_images(normalized)
        normalized = dedupe_urls(normalized)

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
        result = []

        for url in urls:
            if not url:
                continue

            url = self._normalize_jd_image_url(url, image_type="detail")

            if not url:
                continue

            if not url.startswith("http://") and not url.startswith("https://"):
                continue

            lower = url.lower()

            if not self._is_valid_image_url(lower):
                continue

            if "360buyimg.com" not in lower and "jdimg.com" not in lower:
                continue

            if self._is_noise_image(lower):
                continue

            if self._is_bad_network_candidate(lower):
                continue

            allowed_signals = [
                "/imgzone/",
                "/sku/",
                "/cms/",
                "/jfs/",
                "/pcpubliccms/",
                "/image/",
                "/n1/",
                "/n0/",
                "/ssd/",
                "/desc/",
                "/detail/",
            ]

            if not any(signal in lower for signal in allowed_signals):
                continue

            result.append(url)

        return dedupe_urls(result)

    # ------------------------------------------------------------------
    # URL 与过滤
    # ------------------------------------------------------------------

    def _normalize_jd_image_url(self, url: str, image_type: str = "main") -> str:
        """
        规范化京东图片 URL。
        """

        if not url:
            return ""

        url = str(url).strip()
        url = html_lib.unescape(url)

        url = url.replace("\\/", "/")
        url = url.replace('\\"', '"')
        url = url.replace("\\'", "'")

        url = url.strip()
        url = url.strip("\\")
        url = url.strip()
        url = url.strip("'\"")
        url = url.strip()
        url = url.strip("\\")
        url = url.strip("'\"\\ ")

        if not (
            url.startswith("http://")
            or url.startswith("https://")
            or url.startswith("//")
            or url.startswith("/jfs/")
            or url.startswith("jfs/")
            or url.startswith("/t1/")
            or url.startswith("t1/")
        ):
            m = re.search(
                r'(?:https?:)?//[^\'"<>\\\s]+?(?:360buyimg\.com|jdimg\.com)/[^\'"<>\\\s]+?\.(?:jpg|jpeg|png|webp|avif)',
                url,
                flags=re.I,
            )

            if m:
                url = m.group(0).strip("'\"\\ ")

        if url.startswith("//"):
            url = "https:" + url

        if url.startswith("http://"):
            url = "https://" + url[7:]

        url = url.strip("'\"\\ ")

        url = url.replace(".jpg.avif", ".jpg")
        url = url.replace(".jpeg.avif", ".jpeg")
        url = url.replace(".png.avif", ".png")

        url = re.sub(
            r"!(q\d+|cc_\d+x\d+|s\d+x\d+|cr_\d+x\d+_\d+_\d+).*?$",
            "",
            url,
            flags=re.I,
        )

        url = re.sub(
            r"/pcpubliccms/s\d+x\d+_jfs/",
            "/pcpubliccms/jfs/",
            url,
            flags=re.I,
        )

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

        if not url.startswith("http://") and not url.startswith("https://"):
            return ""

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
            "error-new",
            "try_03",
            "try1_07",
            "yinying_06",
            "error_06",
            "certificate",
            "certification",
            "qualification",
            "license",
            "licence",
            "permit",
            "businesslicense",
            "business-license",
            "aptitude",
            "recordal",
            "record",
            "beian",
            "icp",
            "yyzz",
            "wenwangwen",
        ]

        return any(keyword in lower for keyword in blacklist)

    def _log(self, message: str):
        try:
            if hasattr(self.browser, "log_callback") and self.browser.log_callback:
                self.browser.log_callback(message)
        except Exception:
            pass
