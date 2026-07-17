import re
import html as html_lib
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.models import ProductData, ImageItem
from core.detector import PlatformDetector
from core.browser import BrowserClient
from utils.url_utils import normalize_image_url, dedupe_urls, get_url_ext


class Alibaba1688Parser(BaseParser):
    """
    1688 商品解析器。

    当前策略：
    1. 标题：使用已验证较准确的标题解析逻辑；
    2. 主图：优先从 Playwright 渲染后的 DOM 中提取左侧缩略图；
    3. 主图不采集 background-image，避免播放/旋转/放大/参数等 UI 图标混入；
    4. SKU 图：优先从 Playwright 渲染后的 DOM 中提取规格区域图片；
    5. SKU 区域允许 background-image，因为 SKU 小图可能是背景图；
    6. 详情图：优先 descUrl/detailUrl 接口；
    7. 详情图兜底只使用严格详情字段，避免把主图误识别为详情图；
    8. 修复详情接口请求头中文导致的 ascii 编码错误。
    """

    def __init__(
        self,
        log_callback=None,
        headless: bool = False,
        login_wait_seconds: int = 180,
    ):
        self.log_callback = log_callback
        self.browser = BrowserClient(
            user_data_dir="browser_data/1688",
            headless=headless,
            login_wait_seconds=login_wait_seconds,
            log_callback=log_callback,
        )


    def parse(self, url: str) -> ProductData:
        platform, product_id = PlatformDetector.detect(url)

        rendered_data = {}

        self._log("正在打开 1688 商品页面...")

        if hasattr(self.browser, "open_page_with_extracted_data"):
            result = self.browser.open_page_with_extracted_data(
                url,
                self._build_1688_extract_script(),
            )
            html = result.get("html", "") or ""
            rendered_data = result.get("data", {}) or {}
        else:
            html = self.browser.open_page(url)

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        title = self._parse_title(soup, html)
        if not title:
            title = f"1688商品_{product_id or 'unknown'}"

        self._log("正在解析 1688 主图...")
        rendered_main_urls = rendered_data.get("mainImages", []) or []
        main_urls = self._normalize_urls(rendered_main_urls)
        self._log(f"1688渲染DOM主图候选：{len(main_urls)} 张")

        if not main_urls:
            main_urls = self._parse_main_image_urls(html, soup)

        self._log("正在解析 1688 SKU 图...")
        rendered_sku_urls = rendered_data.get("skuImages", []) or []
        sku_urls = self._normalize_urls(rendered_sku_urls)
        self._log(f"1688渲染DOM SKU候选：{len(sku_urls)} 张")

        if not sku_urls:
            sku_urls = self._parse_sku_image_urls(html, soup)

        self._log("正在解析 1688 详情图...")
        raw_detail_urls = self._parse_detail_image_urls(html, url)

        # 如果渲染后的 HTML 没识别到详情图，则用原始 open_page() 重试一次。
        # 注意：重试仍然使用严格详情图规则，不再宽泛扫整页图片。
        if not raw_detail_urls:
            self._log("1688详情图为空，尝试使用原始页面方式重新提取详情图...")

            try:
                fallback_html = self.browser.open_page(url)
                raw_detail_urls = self._parse_detail_image_urls(fallback_html, url)
            except Exception as e:
                self._log(f"1688详情图重试提取失败：{e}")

        main_urls = self._dedupe_keep_order(main_urls)
        sku_urls = self._dedupe_keep_order(sku_urls)
        detail_urls = self._dedupe_keep_order(raw_detail_urls)

        main_set = set(self._image_dedupe_key(u) for u in main_urls)
        sku_set = set(self._image_dedupe_key(u) for u in sku_urls)

        # 详情图排除主图和 SKU 图，避免主图/主图不同尺寸混入详情图。
        detail_urls = [
            u for u in detail_urls
            if self._image_dedupe_key(u) not in main_set
            and self._image_dedupe_key(u) not in sku_set
        ]

        # 不从主图里排除 SKU 图。
        # 1688 很多 SKU 图就是主图区域中的一张图。

        main_urls = main_urls[:12]
        detail_urls = detail_urls[:120]
        sku_urls = sku_urls[:40]

        main_images = self._build_images(main_urls, "main", "1688_main")
        detail_images = self._build_images(detail_urls, "detail", "1688_detail")
        sku_images = self._build_images(sku_urls, "sku", "1688_sku")

        self._log(f"1688商品标题：{title}")
        self._log(f"1688主图识别：{len(main_images)} 张")
        self._log(f"1688详情图识别：{len(detail_images)} 张")
        self._log(f"1688SKU图识别：{len(sku_images)} 张")

        return ProductData(
            platform=platform,
            product_id=product_id,
            title=title,
            url=url,
            main_images=main_images,
            detail_images=detail_images,
            sku_images=sku_images,
        )

    # ------------------------------------------------------------------
    # Playwright DOM 提取脚本
    # ------------------------------------------------------------------

    def _build_1688_extract_script(self) -> str:
        """
        在 Playwright 渲染后的页面中执行 JS，直接提取：
        1. 左侧主图区域图片；
        2. SKU 规格区域图片。

        注意：
            主图区域不采集 background-image；
            SKU 区域允许采集 background-image。
        """

        return r"""
        () => {
            const result = {
                mainImages: [],
                skuImages: []
            };

            function normalizeUrl(url) {
                if (!url) return '';

                url = String(url).trim();

                if (!url) return '';

                if (url.startsWith('//')) {
                    url = 'https:' + url;
                }

                if (!/^https?:\/\//i.test(url)) return '';

                url = url.replace(/&amp;/g, '&');

                return url;
            }

            function isImageUrl(url) {
                return /\.(jpg|jpeg|png|webp)(\?|_|$)/i.test(url || '');
            }

            function imageKey(url) {
                let u = String(url || '').toLowerCase().trim();

                u = u.split('?')[0];
                u = u.replace(/^https?:\/\//, '');

                u = u.replace(/(\.(jpg|jpeg|png|webp))_\d+x\d+(q\d+)?\.(jpg|jpeg|png|webp)$/i, '$1');
                u = u.replace(/(\.(jpg|jpeg|png|webp))_\d+x\d+(q\d+)?$/i, '$1');
                u = u.replace(/(\.(jpg|jpeg|png|webp))_\d+x\d+.*$/i, '$1');
                u = u.replace(/(\.(jpg|jpeg|png|webp))_\.(webp|jpg|jpeg|png)$/i, '$1');

                return u;
            }

            function isBadUrl(url) {
                const u = String(url || '').toLowerCase();

                const badWords = [
                    'icon',
                    'logo',
                    'avatar',
                    'qrcode',
                    'qr_code',
                    'loading',
                    'placeholder',
                    'default',
                    'sprite',
                    'button',
                    'btn',
                    'play',
                    'video',
                    'rotate',
                    'rotation',
                    'zoom',
                    'expand',
                    'param',
                    'parameter',
                    'collect',
                    'favorite',
                    'share',
                    'service',
                    'guarantee',
                    'credit',
                    'member',
                    'shop',
                    'seller',
                    'wangwang',
                    'aliww',
                    'favicon',
                    'transparent',
                    'blank',
                    'empty',
                    'arrow',
                    'close',
                    'search',
                    'cart',
                    'login',
                    'coupon',
                    'discount',
                    'activity',
                    'promotion',
                    'insurance',
                    'promise',
                    'protect',
                    'certificate',
                    'license',
                    'company',
                    'store',
                    'factory',
                    'supplier',
                    'return',
                    'refund',
                    '7day',
                    '48h',
                    '48hour'
                ];

                if (badWords.some(w => u.includes(w))) return true;

                const smallPatterns = [
                    '12x12',
                    '16x16',
                    '20x20',
                    '24x24',
                    '30x30',
                    '32x32',
                    '36x36',
                    '40x40',
                    '48x48',
                    '50x50',
                    '60x60',
                    '64x64',
                    '70x70',
                    '72x72',
                    '80x80',
                    '88x88',
                    '90x90',
                    '100x100',
                    '110x110',
                    '120x120'
                ];

                if (smallPatterns.some(p => u.includes(p))) return true;

                return false;
            }

            function addUrl(list, url) {
                url = normalizeUrl(url);

                if (!url) return;
                if (!isImageUrl(url)) return;
                if (isBadUrl(url)) return;

                if (!/alicdn\.com|1688\.com/i.test(url)) return;

                const key = imageKey(url);
                const exists = list.some(item => imageKey(item) === key);

                if (!exists) {
                    list.push(url);
                }
            }

            function collectUrlsFromElement(el, list, options = {}) {
                if (!el) return;

                const allowBackground = !!options.allowBackground;
                const minRenderedSize = options.minRenderedSize || 40;

                const attrs = [
                    'src',
                    'data-src',
                    'data-original',
                    'data-lazy-src',
                    'data-img',
                    'data-url',
                    'data-lazyload'
                ];

                // img 标签
                el.querySelectorAll('img').forEach(img => {
                    const rect = img.getBoundingClientRect();

                    if (rect.width && rect.height) {
                        if (rect.width < minRenderedSize || rect.height < minRenderedSize) {
                            return;
                        }
                    }

                    const imgClass = (img.className || '').toString().toLowerCase();
                    const imgAlt = (img.getAttribute('alt') || '').toLowerCase();
                    const imgTitle = (img.getAttribute('title') || '').toLowerCase();
                    const imgText = imgClass + ' ' + imgAlt + ' ' + imgTitle;

                    const badImgWords = [
                        'icon',
                        'logo',
                        'button',
                        'btn',
                        'play',
                        'video-icon',
                        'close',
                        'arrow',
                        'expand',
                        'zoom',
                        'rotate',
                        'param',
                        'parameter',
                        '播放',
                        '视频',
                        '旋转',
                        '放大',
                        '参数'
                    ];

                    if (badImgWords.some(w => imgText.includes(w))) {
                        return;
                    }

                    attrs.forEach(attr => {
                        addUrl(list, img.getAttribute(attr));
                    });

                    const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset');
                    if (srcset) {
                        srcset.split(',').forEach(part => {
                            const url = part.trim().split(/\s+/)[0];
                            addUrl(list, url);
                        });
                    }
                });

                // source 标签
                el.querySelectorAll('source').forEach(source => {
                    const srcset = source.getAttribute('srcset') || source.getAttribute('data-srcset');
                    if (srcset) {
                        srcset.split(',').forEach(part => {
                            const url = part.trim().split(/\s+/)[0];
                            addUrl(list, url);
                        });
                    }
                });

                // 主图区域默认不采集 background-image。
                // SKU 区域通过 allowBackground=true 开启。
                if (!allowBackground) {
                    return;
                }

                const all = el.querySelectorAll('*');

                all.forEach(node => {
                    const rect = node.getBoundingClientRect();

                    if (rect.width && rect.height) {
                        if (rect.width < minRenderedSize || rect.height < minRenderedSize) {
                            return;
                        }
                    }

                    const cls = (node.className || '').toString().toLowerCase();
                    const text = (node.innerText || '').toLowerCase();
                    const title = (node.getAttribute('title') || '').toLowerCase();
                    const aria = (node.getAttribute('aria-label') || '').toLowerCase();

                    const nodeText = cls + ' ' + text + ' ' + title + ' ' + aria;

                    const badNodeWords = [
                        'icon',
                        'logo',
                        'button',
                        'btn',
                        'play',
                        'video',
                        'close',
                        'arrow',
                        'expand',
                        'zoom',
                        'rotate',
                        'param',
                        'parameter',
                        '宝贝',
                        '参数',
                        '播放',
                        '视频',
                        '旋转',
                        '放大'
                    ];

                    if (badNodeWords.some(w => nodeText.includes(w))) {
                        return;
                    }

                    const inlineStyle = node.getAttribute('style') || '';
                    const inlineMatches = inlineStyle.match(/url\(["']?([^"')]+)["']?\)/ig);

                    if (inlineMatches) {
                        inlineMatches.forEach(m => {
                            const mm = m.match(/url\(["']?([^"')]+)["']?\)/i);
                            if (mm && mm[1]) {
                                addUrl(list, mm[1]);
                            }
                        });
                    }

                    try {
                        const style = window.getComputedStyle(node);
                        const bg = style && style.backgroundImage ? style.backgroundImage : '';

                        if (bg && bg !== 'none') {
                            const matches = bg.match(/url\(["']?([^"')]+)["']?\)/ig);
                            if (matches) {
                                matches.forEach(m => {
                                    const mm = m.match(/url\(["']?([^"')]+)["']?\)/i);
                                    if (mm && mm[1]) {
                                        addUrl(list, mm[1]);
                                    }
                                });
                            }
                        }
                    } catch (e) {}
                });
            }

            function collectNearLeftGallery() {
                const selectors = [
                    '[class*="detail-gallery"]',
                    '[class*="gallery"]',
                    '[class*="album"]',
                    '[class*="main-image"]',
                    '[class*="mainImage"]',
                    '[class*="image-list"]',
                    '[class*="imageList"]',
                    '[class*="preview"]',
                    '[class*="magnifier"]',
                    '[class*="vertical-img"]',
                    '[class*="verticalImg"]',
                    '[class*="thumb"]',
                    '[class*="thumbnail"]'
                ];

                const candidates = [];

                selectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(el => {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || '').toLowerCase();
                        const cls = (el.className || '').toString().toLowerCase();
                        const html = (el.outerHTML || '').slice(0, 1000).toLowerCase();
                        const area = text + ' ' + cls + ' ' + html;

                        if (rect.width < 30 || rect.height < 30) return;

                        const badArea = [
                            'description',
                            'detail-content',
                            'rich-text',
                            'shop',
                            'seller',
                            'company',
                            'service',
                            'guarantee'
                        ];

                        const goodArea = [
                            'gallery',
                            'album',
                            'main-image',
                            'mainimage',
                            'preview',
                            'magnifier',
                            'thumb',
                            'thumbnail',
                            'image-list',
                            'imagelist',
                            'vertical'
                        ];

                        if (badArea.some(w => area.includes(w)) && !goodArea.some(w => area.includes(w))) {
                            return;
                        }

                        candidates.push({
                            el,
                            top: rect.top,
                            left: rect.left,
                            width: rect.width,
                            height: rect.height
                        });
                    });
                });

                candidates.sort((a, b) => {
                    if (Math.abs(a.top - b.top) > 50) return a.top - b.top;
                    return a.left - b.left;
                });

                candidates.slice(0, 25).forEach(item => {
                    collectUrlsFromElement(item.el, result.mainImages, {
                        allowBackground: false,
                        minRenderedSize: 45
                    });
                });
            }

            function collectSkuArea() {
                const selectors = [
                    '[class*="sku"]',
                    '[class*="Sku"]',
                    '[class*="sale-prop"]',
                    '[class*="saleProp"]',
                    '[class*="prop-item"]',
                    '[class*="propItem"]',
                    '[class*="spec"]',
                    '[class*="model"]',
                    '[class*="attribute"]',
                    '[class*="offer-attr"]'
                ];

                const candidates = [];

                selectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(el => {
                        const text = (el.innerText || '').toLowerCase();
                        const html = (el.outerHTML || '').toLowerCase();
                        const combined = text + ' ' + html;

                        const looksLikeSku =
                            combined.includes('sku') ||
                            combined.includes('规格') ||
                            combined.includes('型号') ||
                            combined.includes('颜色') ||
                            combined.includes('尺寸') ||
                            combined.includes('款式') ||
                            combined.includes('类型') ||
                            combined.includes('model') ||
                            combined.includes('spec') ||
                            combined.includes('prop') ||
                            /\b[a-z]\d{1,3}\b/i.test(combined);

                        if (!looksLikeSku) return;

                        candidates.push(el);
                    });
                });

                candidates.slice(0, 60).forEach(el => {
                    collectUrlsFromElement(el, result.skuImages, {
                        allowBackground: true,
                        minRenderedSize: 35
                    });
                });
            }

            collectNearLeftGallery();
            collectSkuArea();

            result.mainImages = result.mainImages.slice(0, 12);
            result.skuImages = result.skuImages.slice(0, 40);

            return result;
        }
        """

    # ------------------------------------------------------------------
    # 标题解析
    # ------------------------------------------------------------------

    def _parse_title(self, soup: BeautifulSoup, html: str) -> str:
        meta_selectors = [
            ("meta", {"property": "og:title"}),
            ("meta", {"name": "og:title"}),
            ("meta", {"name": "title"}),
            ("meta", {"itemprop": "name"}),
        ]

        for name, attrs in meta_selectors:
            tag = soup.find(name, attrs=attrs)
            if tag:
                title = tag.get("content", "").strip()
                title = self._clean_title(title)
                if self._is_valid_title(title):
                    return title

        selectors = [
            "h1",
            ".offer-title",
            ".mod-detail-title",
            ".detail-title",
            ".title-text",
            "[class*=offer-title]",
            "[class*=detail-title]",
            "[class*=title-text]",
        ]

        for selector in selectors:
            try:
                tag = soup.select_one(selector)
            except Exception:
                tag = None

            if tag:
                title = tag.get_text(" ", strip=True)
                title = self._clean_title(title)
                if self._is_valid_title(title):
                    return title

        text = self._decode_text(html)

        patterns = [
            r'"subject"\s*:\s*"([^"]{3,300})"',
            r'"offerTitle"\s*:\s*"([^"]{3,300})"',
            r'"offer_title"\s*:\s*"([^"]{3,300})"',
            r'"productTitle"\s*:\s*"([^"]{3,300})"',
            r'"product_title"\s*:\s*"([^"]{3,300})"',
            r'"title"\s*:\s*"([^"]{3,300})"',
            r"'subject'\s*:\s*'([^']{3,300})'",
            r"'offerTitle'\s*:\s*'([^']{3,300})'",
            r"'title'\s*:\s*'([^']{3,300})'",
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                start = max(0, m.start() - 120)
                prefix = text[start:m.start()].lower()

                if any(k in prefix for k in [
                    "company",
                    "companyname",
                    "shop",
                    "shopname",
                    "seller",
                    "sellername",
                    "supplier",
                    "suppliername",
                    "store",
                    "storename",
                    "corp",
                ]):
                    continue

                title = self._clean_title(m.group(1))
                if self._is_valid_title(title):
                    return title

        if soup.title:
            title = soup.title.get_text(" ", strip=True)
            title = self._clean_title(title)
            if self._is_valid_title(title):
                return title

        return ""

    def _clean_title(self, title: str) -> str:
        if not title:
            return ""

        title = self._decode_text(title)
        title = re.sub(r"\s+", " ", title).strip()

        remove_parts = [
            "- 阿里巴巴",
            "_阿里巴巴",
            "- 1688.com",
            "_1688.com",
            "- 1688",
            "_1688",
            "阿里巴巴",
        ]

        for part in remove_parts:
            title = title.replace(part, "")

        title = title.strip(" -_|,，。")

        return title

    def _is_valid_title(self, title: str) -> bool:
        if not title:
            return False

        if len(title) < 3:
            return False

        bad_words = [
            "有限公司",
            "公司首页",
            "诚信通",
            "旺铺",
            "供应商",
            "企业介绍",
            "公司介绍",
            "联系方式",
            "店铺首页",
            "实力商家",
            "工厂档案",
            "经营模式",
        ]

        if any(w in title for w in bad_words) and len(title) <= 60:
            return False

        bad_exact = [
            "1688",
            "阿里巴巴",
            "商品详情",
            "产品详情",
            "店铺首页",
        ]

        if title in bad_exact:
            return False

        return True

    # ------------------------------------------------------------------
    # 主图兜底解析
    # ------------------------------------------------------------------

    def _parse_main_image_urls(self, html: str, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []

        urls.extend(self._parse_main_images_from_dom(soup))

        if len(urls) < 6:
            text = self._decode_text(html)

            main_keys = [
                "offerImgList",
                "offerImageList",
                "mainImageList",
                "mainImages",
                "main_images",
                "albumImages",
                "albumImageList",
                "productImageList",
                "productImages",
            ]

            for key in main_keys:
                for block in self._extract_json_like_blocks_by_key(text, key, max_len=5000):
                    block_urls = self._extract_image_urls_from_text(block)

                    for u in block_urls:
                        if not self._is_likely_product_image(u):
                            continue

                        if self._is_service_or_ui_context(block, u):
                            continue

                        urls.append(u)

        urls = self._normalize_urls(urls)
        urls = [u for u in urls if self._is_likely_product_image(u)]
        urls = self._dedupe_keep_order(urls)

        return urls[:12]

    def _parse_main_images_from_dom(self, soup: BeautifulSoup) -> list[str]:
        """
        静态 HTML 兜底提取主图。

        注意：
            主图区域不提取 background-image，
            避免播放、旋转、放大、参数等 UI 图标混入。
        """
        urls: list[str] = []

        selectors = [
            "[class*=detail-gallery]",
            "[class*=gallery]",
            "[class*=album]",
            "[class*=main-image]",
            "[class*=mainImage]",
            "[class*=image-list]",
            "[class*=imageList]",
            "[class*=preview]",
            "[class*=magnifier]",
            "[class*=vertical-img]",
            "[class*=verticalImg]",
            "[class*=thumb]",
            "[class*=thumbnail]",
        ]

        for selector in selectors:
            try:
                nodes = soup.select(selector)
            except Exception:
                nodes = []

            for node in nodes[:20]:
                node_text = str(node)
                lower_node_text = node_text.lower()

                if any(k in lower_node_text for k in [
                    "description",
                    "detail-content",
                    "rich-text",
                    "service",
                    "guarantee",
                    "shop",
                    "seller",
                    "company",
                ]):
                    if not any(k in lower_node_text for k in [
                        "gallery",
                        "album",
                        "main-image",
                        "mainimage",
                        "preview",
                        "magnifier",
                        "thumb",
                        "thumbnail",
                        "image-list",
                        "imagelist",
                    ]):
                        continue

                for img in node.find_all("img"):
                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-img",
                        "data-url",
                        "data-lazyload",
                    ]:
                        value = img.get(attr)
                        if value:
                            urls.append(value)

                    srcset = img.get("srcset") or img.get("data-srcset")
                    if srcset:
                        for part in srcset.split(","):
                            value = part.strip().split(" ")[0]
                            if value:
                                urls.append(value)

                # 主图区域不采集 background-image。
                # 这些背景图大多是播放按钮、旋转按钮、放大图标、参数按钮等 UI。
                # urls.extend(self._extract_background_image_urls(node_text))

        return urls

    # ------------------------------------------------------------------
    # SKU 兜底解析
    # ------------------------------------------------------------------

    def _parse_sku_image_urls(self, html: str, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []

        urls.extend(self._parse_sku_images_from_dom(soup))

        text = self._decode_text(html)

        sku_keys = [
            "skuProps",
            "sku_props",
            "skuModel",
            "sku_model",
            "skuImages",
            "sku_images",
            "propertyPics",
            "property_pics",
            "skuPictures",
            "sku_pictures",
            "skuImageMap",
            "sku_image_map",
        ]

        for key in sku_keys:
            for block in self._extract_json_like_blocks_by_key(text, key, max_len=12000):
                block_urls = self._extract_image_urls_from_text(block)

                for u in block_urls:
                    if not self._is_likely_product_image(u):
                        continue

                    if self._is_service_or_ui_context(block, u):
                        continue

                    if self._is_detail_marketing_context(block, u):
                        continue

                    urls.append(u)

        urls = self._normalize_urls(urls)
        urls = [u for u in urls if self._is_likely_product_image(u)]
        urls = self._dedupe_keep_order(urls)

        return urls[:40]

    def _parse_sku_images_from_dom(self, soup: BeautifulSoup) -> list[str]:
        """
        SKU 区域兜底提取。

        注意：
            SKU 区域允许 background-image，
            因为 SKU 小图有时就是背景图。
        """
        urls: list[str] = []

        selectors = [
            "[class*=sku]",
            "[class*=Sku]",
            "[class*=sale-prop]",
            "[class*=saleProp]",
            "[class*=prop-item]",
            "[class*=propItem]",
            "[class*=spec]",
            "[class*=model]",
            "[class*=offer-attr]",
            "[class*=attribute]",
        ]

        for selector in selectors:
            try:
                nodes = soup.select(selector)
            except Exception:
                nodes = []

            for node in nodes[:60]:
                node_text = str(node)
                plain_text = node.get_text(" ", strip=True)

                if not self._looks_like_sku_node(plain_text, node_text):
                    continue

                urls.extend(self._extract_background_image_urls(node_text))

                for img in node.find_all("img"):
                    for attr in [
                        "src",
                        "data-src",
                        "data-original",
                        "data-lazy-src",
                        "data-img",
                        "data-url",
                        "data-lazyload",
                    ]:
                        value = img.get(attr)
                        if value:
                            urls.append(value)

                    srcset = img.get("srcset") or img.get("data-srcset")
                    if srcset:
                        for part in srcset.split(","):
                            value = part.strip().split(" ")[0]
                            if value:
                                urls.append(value)

        return urls

    def _looks_like_sku_node(self, plain_text: str, html_text: str) -> bool:
        text = f"{plain_text} {html_text}".lower()

        good_words = [
            "sku",
            "规格",
            "型号",
            "颜色",
            "尺寸",
            "款式",
            "类型",
            "model",
            "spec",
            "prop",
            "attribute",
            "sale-prop",
            "saleprop",
        ]

        if any(w in text for w in good_words):
            return True

        if re.search(r"\b[a-z]\d{1,3}\b", text, re.I):
            return True

        return False

    # ------------------------------------------------------------------
    # 详情图解析
    # ------------------------------------------------------------------

    def _parse_detail_image_urls(self, html: str, page_url: str) -> list[str]:
        """
        解析详情图。

        策略：
        1. 优先请求 descUrl/detailUrl；
        2. descUrl 返回内容中直接提取详情图片；
        3. 只有 descUrl 完全失败时，才使用严格详情字段兜底；
        4. 不使用 description/content/offerDetail/productDetail 等宽字段扫整页。
        """
        text = self._decode_text(html)

        urls: list[str] = []

        # ------------------------------------------------------------
        # 1. 优先 descUrl / detailUrl 接口
        # ------------------------------------------------------------
        desc_urls = self._extract_desc_urls(text, page_url)

        if not desc_urls:
            self._log("1688未找到有效详情接口 descUrl。")

        for desc_url in desc_urls:
            self._log(f"尝试请求 1688 详情接口：{desc_url}")

            try:
                detail_html = self._request_text(desc_url)
            except Exception as e:
                self._log(f"1688详情接口请求失败：{e}")
                continue

            if not detail_html:
                self._log("1688详情接口返回为空。")
                continue

            self._log(f"1688详情接口返回长度：{len(detail_html)}")

            detail_urls = self._extract_detail_images_from_desc_html(detail_html)

            self._log(f"1688详情接口提取图片：{len(detail_urls)} 张")

            urls.extend(detail_urls)

        # ------------------------------------------------------------
        # 2. 如果 descUrl 没拿到图片，再用严格字段兜底
        # ------------------------------------------------------------
        if not urls:
            strict_detail_keys = [
                "detailContent",
                "detailImages",
                "detail_images",
                "richText",
                "rich_text",
            ]

            for key in strict_detail_keys:
                for block in self._extract_json_like_blocks_by_key(text, key, max_len=40000):
                    block_urls = self._extract_image_urls_from_text(block)

                    for u in block_urls:
                        if not self._is_likely_product_image(u):
                            continue

                        # 详情图只过滤明显 UI/服务图，不过滤营销图/参数图
                        if self._is_service_or_ui_context(block, u):
                            continue

                        urls.append(u)

        urls = self._normalize_urls(urls)
        urls = [
            u for u in urls
            if self._is_likely_product_image(u)
        ]
        urls = self._dedupe_keep_order(urls)

        return urls[:120]


    def _extract_desc_urls(self, text: str, page_url: str) -> list[str]:
        """
        提取 1688 详情接口 URL。

        只保留真正可能返回商品详情 HTML/图片的接口。
        过滤：
            - 金融授信页
            - air.1688.com
            - credit-buy
            - 非详情接口
        """
        urls = []

        patterns = [
            r'"descUrl"\s*:\s*"([^"]+)"',
            r"'descUrl'\s*:\s*'([^']+)'",
            r'"detailUrl"\s*:\s*"([^"]+)"',
            r"'detailUrl'\s*:\s*'([^']+)'",
            r'((?:https?:)?//itemcdn\.tmall\.com/1688offer/[^"\']+)',
            r'((?:https?:)?//[^"\']+/offer/desc/[^"\']+)',
            r'((?:https?:)?//[^"\']+desc[^"\']+offer[^"\']+)',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                raw = m.group(1)
                if not raw:
                    continue

                raw = self._decode_text(raw)
                raw = raw.strip().strip('"').strip("'")

                if not raw:
                    continue

                if raw.startswith("//"):
                    raw = "https:" + raw
                elif raw.startswith("/"):
                    raw = urljoin(page_url, raw)

                if not raw.startswith("http"):
                    continue

                lower = raw.lower()

                # --------------------------------------------------------
                # 过滤明显不是商品详情图接口的 URL
                # --------------------------------------------------------
                bad_words = [
                    "air.1688.com",
                    "credit-buy",
                    "scene_quota",
                    "finance",
                    "login",
                    "member",
                    "passport",
                    "cart",
                    "trade",
                    "order",
                    "pay",
                ]

                if any(w in lower for w in bad_words):
                    continue

                # --------------------------------------------------------
                # 只保留真正像详情接口的 URL
                # --------------------------------------------------------
                good_words = [
                    "itemcdn.tmall.com/1688offer",
                    "/offer/desc/",
                    "desc",
                ]

                if not any(w in lower for w in good_words):
                    continue

                if raw not in urls:
                    urls.append(raw)

        return urls[:5]


    def _extract_detail_images_from_desc_html(self, detail_html: str) -> list[str]:
        """
        从 1688 descUrl 接口返回内容中提取详情图。

        兼容格式：
            1. 直接 HTML
            2. JSON/JSONP: {"content": "..."}
            3. JS 变量：var offer_details = ...
            4. 转义 HTML 字符串
            5. 全文本中直接包含图片 URL
        """
        text = self._decode_text(detail_html)

        content_candidates = []

        # ------------------------------------------------------------
        # 1. 尝试提取 content/desc/detailContent 字段
        # ------------------------------------------------------------
        patterns = [
            r'"content"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'content'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
            r'"desc"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r"'desc'\s*:\s*'(.+?)'\s*(?:,\s*'|\})",
            r'"offerDetail"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r'"detailContent"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
            r'"detail"\s*:\s*"(.+?)"\s*(?:,\s*"|\})',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.S):
                value = m.group(1)
                if value:
                    content_candidates.append(value)

        # ------------------------------------------------------------
        # 2. 重要：无论是否提取到 content 字段，都加入完整返回体兜底
        #
        # 有些 1688 desc 接口 content 字段很长，简单正则可能截断。
        # 如果只解析截断后的 content，就会出现“接口请求成功但详情图为 0”。
        # ------------------------------------------------------------
        content_candidates.append(text)

        urls = []

        for content in content_candidates:
            content = self._decode_text(content)

            # 常见 JS/JSON 转义修复
            content = content.replace('\\"', '"')
            content = content.replace("\\'", "'")
            content = content.replace("\\n", "\n")
            content = content.replace("\\r", "\r")
            content = content.replace("\\t", "\t")

            try:
                soup = BeautifulSoup(content, "lxml")
            except Exception:
                soup = BeautifulSoup(content, "html.parser")

            # --------------------------------------------------------
            # 3. 从 img 标签提取
            # --------------------------------------------------------
            for img in soup.find_all("img"):
                for attr in [
                    "src",
                    "data-src",
                    "data-original",
                    "data-lazy-src",
                    "data-img",
                    "data-url",
                    "data-lazyload",
                    "srcset",
                    "data-srcset",
                ]:
                    value = img.get(attr)
                    if not value:
                        continue

                    if attr in ["srcset", "data-srcset"]:
                        for part in value.split(","):
                            u = part.strip().split(" ")[0]
                            if u:
                                urls.append(u)
                    else:
                        urls.append(value)

            # --------------------------------------------------------
            # 4. 从 background-image 提取
            # --------------------------------------------------------
            urls.extend(self._extract_background_image_urls(content))

            # --------------------------------------------------------
            # 5. 从全文正则提取图片 URL
            # --------------------------------------------------------
            urls.extend(self._extract_image_urls_from_text(content))

        urls = self._normalize_urls(urls)

        # 详情图这里不能太严格过滤营销图/参数图，
        # 只做基本商品图 URL 判断。
        urls = [
            u for u in urls
            if self._is_likely_product_image(u)
        ]

        return self._dedupe_keep_order(urls)


    # ------------------------------------------------------------------
    # 文本 / URL 工具
    # ------------------------------------------------------------------

    def _extract_json_like_blocks_by_key(
        self,
        text: str,
        key: str,
        max_len: int = 10000,
    ) -> list[str]:
        blocks = []

        key_patterns = [
            f'"{key}"',
            f"'{key}'",
            key,
        ]

        for key_pattern in key_patterns:
            start = 0

            while True:
                idx = text.find(key_pattern, start)
                if idx < 0:
                    break

                left = max(0, idx - 800)
                right = min(len(text), idx + max_len)
                block = text[left:right]

                if block and block not in blocks:
                    blocks.append(block)

                start = idx + len(key_pattern)

                if len(blocks) >= 30:
                    break

        return blocks

    def _extract_image_urls_from_text(self, text: str) -> list[str]:
        if not text:
            return []

        text = self._decode_text(text)

        urls = []

        patterns = [
            r'(?:https?:)?//[^"\'\s<>\\]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s<>\\]*)?',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.jpg_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.jpeg_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.png_[^"\'\s<>\\]+',
            r'(?:https?:)?//[^"\'\s<>\\]+?\.webp_[^"\'\s<>\\]+',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                urls.append(m.group(0))

        return urls

    def _extract_background_image_urls(self, text: str) -> list[str]:
        if not text:
            return []

        text = self._decode_text(text)

        urls = []

        patterns = [
            r'background-image\s*:\s*url\(["\']?([^"\')]+)["\']?\)',
            r'background\s*:\s*url\(["\']?([^"\')]+)["\']?\)',
            r'url\(["\']?((?:https?:)?//[^"\')]+?\.(?:jpg|jpeg|png|webp)[^"\')]*)["\']?\)',
        ]

        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                urls.append(m.group(1))

        return urls

    def _normalize_urls(self, urls: list[str]) -> list[str]:
        result = []

        for url in urls:
            url = self._normalize_image_url(url)
            if url:
                result.append(url)

        return self._dedupe_keep_order(result)

    def _normalize_image_url(self, url: str) -> str:
        if not url:
            return ""

        url = self._decode_text(url)
        url = url.strip().strip('"').strip("'").strip()

        if not url:
            return ""

        if url.startswith("//"):
            url = "https:" + url

        if not url.startswith("http"):
            return ""

        url = url.rstrip("\\")
        url = url.rstrip(",")
        url = url.rstrip(";")
        url = url.rstrip(")")
        url = url.rstrip("]")
        url = url.rstrip("}")

        # xxx.jpg_300x300.jpg -> xxx.jpg
        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?\.(?:jpg|jpeg|png|webp)$',
            r'\1',
            url,
            flags=re.I,
        )

        # xxx.jpg_300x300 -> xxx.jpg
        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?$',
            r'\1',
            url,
            flags=re.I,
        )

        # xxx.jpg_300x300q90_... -> xxx.jpg
        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\d+x\d+.*$',
            r'\1',
            url,
            flags=re.I,
        )

        # xxx.jpg_.webp -> xxx.jpg
        url = re.sub(
            r'(\.(?:jpg|jpeg|png|webp))_\.(?:webp|jpg|jpeg|png)$',
            r'\1',
            url,
            flags=re.I,
        )

        url = re.sub(r'([?&])x-oss-process=image/resize[^&]*', r'\1', url)

        try:
            url = normalize_image_url(url)
        except Exception:
            pass

        return url

    def _is_likely_product_image(self, url: str) -> bool:
        if not url:
            return False

        u = url.lower()

        if not u.startswith("http"):
            return False

        if not re.search(r'\.(jpg|jpeg|png|webp)(?:$|\?|_)', u, re.I):
            return False

        bad_keywords = [
            "icon",
            "logo",
            "avatar",
            "qrcode",
            "qr_code",
            "qr-",
            "loading",
            "placeholder",
            "default",
            "sprite",
            "button",
            "btn",
            "play",
            "video",
            "rotate",
            "rotation",
            "zoom",
            "expand",
            # 注意：
            # 不要在全局 URL 过滤中加入 param / parameter，
            # 否则可能误杀详情图里的参数图。
            "collect",
            "favorite",
            "share",
            "service",
            "security",
            "guarantee",
            "credit",
            "member",
            "shop",
            "seller",
            "wangwang",
            "aliww",
            "favicon",
            "transparent",
            "blank",
            "empty",
            "grey",
            "gray",
            "arrow",
            "close",
            "search",
            "cart",
            "login",
            "rank",
            "medal",
            "badge",
            "coupon",
            "discount",
            "activity",
            "promotion",
            "insurance",
            "promise",
            "protect",
            "safe",
            "seal",
            "cert",
            "certificate",
            "license",
            "company",
            "store",
            "factory",
            "supplier",
            "contact",
            "phone",
            "tel",
            "map",
            "location",
            "auth",
            "authen",
            "weixin",
            "wechat",
            "alipay",
            "taobao",
            "tmall",
            "return",
            "refund",
            "7day",
            "seven",
            "48h",
            "48hour",
            "assurance",
            "buyer",
            "trade",
            "chengxin",
            "chengxintong",
        ]

        if any(k in u for k in bad_keywords):
            return False

        small_patterns = [
            "12x12",
            "16x16",
            "20x20",
            "24x24",
            "30x30",
            "32x32",
            "36x36",
            "40x40",
            "48x48",
            "50x50",
            "60x60",
            "64x64",
            "70x70",
            "72x72",
            "80x80",
            "88x88",
            "90x90",
            "100x100",
            "110x110",
            "120x120",
        ]

        if any(p in u for p in small_patterns):
            return False

        bad_path_keywords = [
            "/tps/",
            "/tfs/",
            "/favicon",
        ]

        if any(k in u for k in bad_path_keywords):
            return False

        good_keywords = [
            "cbu01.alicdn.com/img/ibank",
            "cbu01.alicdn.com/img",
            "cbu01.alicdn.com/",
            "img.alicdn.com/imgextra",
            "img.alicdn.com/bao/uploaded",
            "alicdn.com",
        ]

        if not any(k in u for k in good_keywords):
            return False

        return True

    def _is_service_or_ui_context(self, text: str, url: str) -> bool:
        if not text or not url:
            return False

        lower_text = text.lower()
        lower_url = url.lower()

        pos = lower_text.find(lower_url)

        if pos < 0:
            ctx = lower_url
        else:
            start = max(0, pos - 300)
            end = min(len(lower_text), pos + len(lower_url) + 300)
            ctx = lower_text[start:end]

        bad_context_words = [
            "icon",
            "logo",
            "sprite",
            "button",
            "btn",
            "play",
            "video",
            "rotate",
            "rotation",
            "zoom",
            "expand",
            "param",
            "parameter",
            "avatar",
            "qrcode",
            "qr",
            "shop",
            "seller",
            "company",
            "store",
            "supplier",
            "service",
            "guarantee",
            "promise",
            "protect",
            "security",
            "credit",
            "auth",
            "cert",
            "certificate",
            "license",
            "return",
            "refund",
            "7day",
            "seven",
            "48h",
            "48hour",
            "insurance",
            "trade",
            "buyer",
            "coupon",
            "discount",
            "activity",
            "promotion",
            "wangwang",
            "aliww",
            "favorite",
            "collect",
            "share",
            "cart",
        ]

        if any(w in ctx for w in bad_context_words):
            return True

        bad_cn_words = [
            "七天",
            "7天",
            "退货",
            "退款",
            "包退",
            "包换",
            "48小时",
            "保障",
            "买家保障",
            "交易保障",
            "服务",
            "诚信通",
            "实力商家",
            "认证",
            "证书",
            "店铺",
            "公司",
            "供应商",
            "收藏",
            "分享",
            "播放",
            "视频",
            "旋转",
            "放大",
            "参数",
            "宝贝",
        ]

        if any(w in ctx for w in bad_cn_words):
            return True

        return False

    def _is_detail_marketing_context(self, text: str, url: str) -> bool:
        if not text or not url:
            return False

        lower_text = text.lower()
        lower_url = url.lower()

        pos = lower_text.find(lower_url)

        if pos < 0:
            ctx = lower_text[:1000]
        else:
            start = max(0, pos - 300)
            end = min(len(lower_text), pos + len(lower_url) + 300)
            ctx = lower_text[start:end]

        bad_words = [
            "detail",
            "description",
            "desc",
            "richtext",
            "rich-text",
            "content",
            "module",
            "营销",
            "详情",
            "卖点",
            "参数",
            "banner",
            "poster",
            "海报",
            "宣传",
        ]

        if any(w in ctx for w in bad_words):
            return True

        return False

    def _decode_text(self, text: str) -> str:
        if not text:
            return ""

        try:
            text = html_lib.unescape(text)
        except Exception:
            pass

        text = text.replace("\\/", "/")
        text = text.replace("\\u002F", "/")
        text = text.replace("\\u002f", "/")
        text = text.replace("&amp;", "&")

        def replace_unicode(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)

        text = re.sub(r"\\u([0-9a-fA-F]{4})", replace_unicode, text)

        return text

    def _dedupe_keep_order(self, urls: list[str]) -> list[str]:
        result = []
        seen = set()

        for url in urls:
            if not url:
                continue

            key = self._image_dedupe_key(url)

            if key in seen:
                continue

            seen.add(key)
            result.append(url)

        try:
            result = dedupe_urls(result)
        except Exception:
            pass

        return result

    def _image_dedupe_key(self, url: str) -> str:
        """
        生成图片去重 key。

        重点处理 1688 / 阿里 CDN 图片多尺寸问题，例如：
            xxx.jpg
            xxx.jpg_100x100.jpg
            xxx.jpg_300x300.jpg
            xxx.jpg_460x460q90.jpg
            xxx.jpg_.webp
            xxx.jpg_720x720q90.jpg_.webp

        这些都应该视为同一张图。
        """
        if not url:
            return ""

        u = url.lower().strip()

        # 去查询参数
        u = u.split("?")[0]

        # 去协议
        u = u.replace("https://", "").replace("http://", "")

        # 去尾部非法字符
        u = u.rstrip("\\")
        u = u.rstrip(",")
        u = u.rstrip(";")
        u = u.rstrip(")")
        u = u.rstrip("]")
        u = u.rstrip("}")

        # 处理 xxx.jpg_.webp / xxx.png_.webp
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\.(?:webp|jpg|jpeg|png)$",
            r"\1",
            u,
            flags=re.I,
        )

        # 处理 xxx.jpg_720x720q90.jpg_.webp
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?\.(?:jpg|jpeg|png|webp)_\.(?:webp|jpg|jpeg|png)$",
            r"\1",
            u,
            flags=re.I,
        )

        # 处理 xxx.jpg_720x720q90.jpg
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?\.(?:jpg|jpeg|png|webp)$",
            r"\1",
            u,
            flags=re.I,
        )

        # 处理 xxx.jpg_720x720q90
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))_\d+x\d+(?:q\d+)?$",
            r"\1",
            u,
            flags=re.I,
        )

        # 兜底：只要第一个图片扩展名后面还有缩略参数，直接截断
        match = re.match(r"^(.*?\.(?:jpg|jpeg|png|webp))(?:_.*)?$", u, re.I)
        if match:
            u = match.group(1)

        return u

    def _request_text(self, url: str) -> str:
        """
        请求文本内容。

        重要：
            HTTP 请求头中不能包含中文字符。
            之前 User-Agent 中如果写了 Chrome/[IP 地址] 会导致：
            'ascii' codec can't encode characters
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://detail.1688.com/",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text

    def _build_images(
        self,
        urls: list[str],
        image_type: str,
        source: str,
    ) -> list[ImageItem]:
        images = []

        for index, url in enumerate(urls, start=1):
            ext = ""

            try:
                ext = get_url_ext(url)
            except Exception:
                ext = ""

            if not ext:
                ext = ".jpg"

            name = f"{index:03d}{ext}"

            images.append(
                ImageItem(
                    url=url,
                    image_type=image_type,
                    name=name,
                    ext=ext,
                    sku_name=None,
                    source=source,
                )
            )

        return images

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
