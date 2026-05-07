"""
WeChat Reader MCP — read public WeChat Official Account articles, view their images.

Two tools:
- read_wechat_article(url): article -> markdown with image placeholders
- view_wechat_image(url):   image URL -> MCP ImageContent

Strict domain whitelist: mp.weixin.qq.com (article) and *.qpic.cn (image).
No third-party services. No persistent state. Image downloads omit Referer to
bypass mmbiz.qpic.cn hotlink protection.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import socket
from urllib.parse import urlparse

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
from PIL import Image

try:
    import idna
    _IDNA_AVAILABLE = True
except ImportError:
    _IDNA_AVAILABLE = False


# ---------- Constants ----------

ARTICLE_URL_PREFIX = "https://mp.weixin.qq.com/s/"
IMAGE_HOST_SUFFIX = ".qpic.cn"

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

ARTICLE_TIMEOUT_MS = 30_000
IMAGE_DOWNLOAD_TIMEOUT_S = 15
ARTICLE_MAX_BYTES = 500 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024

# Output budget for a single image, in base64 chars.
# Claude Code MAX_MCP_OUTPUT_TOKENS defaults to 25_000, and image content is
# accounted toward that budget by base64 length (≈ 4 chars / token), so we
# target well under it to leave room for the wrapper text. See
# anthropics/claude-code#9152.
TARGET_IMAGE_B64_CHARS = 80_000  # ~ 20K tokens
IMAGE_LONG_EDGE_MAX = 1568        # initial cap before quality stepdown

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
]

_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata.googleusercontent.com",
    "169.254.169.254",
}


# ---------- URL validation (SSRF + whitelist) ----------

def _validate_url_safe(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked: only http/https allowed, got '{parsed.scheme}'")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Blocked: no hostname in URL")

    hostname_lower = hostname.lower()
    for blocked in _BLOCKED_HOSTNAMES:
        if hostname_lower == blocked or hostname_lower.endswith(f".{blocked}"):
            raise ValueError(f"Blocked: hostname '{hostname}' is reserved")

    if _IDNA_AVAILABLE:
        try:
            idna.encode(hostname).decode("ascii")
        except idna.core.InvalidCodepoint:
            raise ValueError(f"Blocked: invalid IDN characters in '{hostname}'")
        except Exception:
            pass

    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"Blocked: cannot resolve '{hostname}'")

    for _, _, _, _, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(f"Blocked: '{hostname}' resolves to private address {ip}")


def _check_article_url(url: str) -> None:
    if not url.startswith(ARTICLE_URL_PREFIX):
        raise ValueError(
            f"URL must start with {ARTICLE_URL_PREFIX} (got: {url[:80]})"
        )
    _validate_url_safe(url)


def _check_image_url(url: str) -> None:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.hostname.endswith(IMAGE_HOST_SUFFIX):
        raise ValueError(
            f"Image URL must be on *{IMAGE_HOST_SUFFIX} (got: {url[:80]})"
        )
    _validate_url_safe(url)


# ---------- Article fetching (Playwright) ----------

# JS that walks #js_content and produces markdown-ish text + image inventory.
_EXTRACT_JS = r"""() => {
    const titleEl = document.querySelector('#activity-name');
    const authorEl = document.querySelector('#js_name')
        || document.querySelector('.rich_media_meta_text')
        || document.querySelector('.profile_nickname');
    const timeEl = document.querySelector('#publish_time')
        || document.querySelector('em#publish_time');
    const container = document.querySelector('#js_content')
        || document.querySelector('.rich_media_content');

    const meta = {
        title: titleEl ? titleEl.innerText.trim() : '',
        author: authorEl ? authorEl.innerText.trim() : '',
        publish_time: timeEl ? (timeEl.innerText || timeEl.getAttribute('data-time') || '').trim() : '',
    };

    if (!container) {
        return { ...meta, body: null, images: [] };
    }

    const images = [];
    const parts = [];
    let imgIdx = 0;

    const walk = (node) => {
        if (node.nodeType === 3) {
            const t = node.textContent;
            if (t.trim()) parts.push(t.replace(/\s+/g, ' '));
            return;
        }
        if (node.nodeType !== 1) return;
        const tag = node.tagName;
        if (tag === 'IMG') {
            const src = node.getAttribute('data-src') || node.getAttribute('src');
            if (src) {
                imgIdx++;
                const alt = (node.getAttribute('alt') || '').trim();
                images.push({ index: imgIdx, url: src, alt: alt });
                parts.push('\n[图片' + imgIdx + ': ' + src + ']\n');
            }
            return;
        }
        if (tag === 'BR') { parts.push('\n'); return; }
        if (tag === 'SCRIPT' || tag === 'STYLE') return;

        if (['H1','H2','H3','H4','H5','H6'].includes(tag)) {
            parts.push('\n\n## ');
            for (const c of node.childNodes) walk(c);
            parts.push('\n');
            return;
        }
        if (tag === 'STRONG' || tag === 'B') {
            parts.push('**');
            for (const c of node.childNodes) walk(c);
            parts.push('**');
            return;
        }
        if (tag === 'EM' || tag === 'I') {
            parts.push('_');
            for (const c of node.childNodes) walk(c);
            parts.push('_');
            return;
        }
        if (['P','DIV','SECTION','BLOCKQUOTE','LI','TR'].includes(tag)) {
            for (const c of node.childNodes) walk(c);
            parts.push('\n\n');
            return;
        }
        for (const c of node.childNodes) walk(c);
    };
    walk(container);

    let body = parts.join('').replace(/\n{3,}/g, '\n\n').trim();
    return { ...meta, body, images };
}"""


async def _fetch_article(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. From the project venv run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=CHROME_UA)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=ARTICLE_TIMEOUT_MS)
            except Exception as e:
                raise RuntimeError(f"Failed to load page: {e}")

            try:
                await page.wait_for_selector("#js_content", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            return await page.evaluate(_EXTRACT_JS)
        finally:
            await browser.close()


def _format_article_markdown(data: dict, source_url: str) -> str:
    title = data.get("title") or "(无标题)"
    author = data.get("author") or "(未知作者)"
    publish_time = data.get("publish_time") or "(未知时间)"
    body = data.get("body") or ""
    images = data.get("images") or []

    if not body:
        return (
            "<wechat_article>\n"
            f"# {title}\n"
            f"**作者**：{author} ｜ **发布时间**：{publish_time} ｜ **原文**：{source_url}\n\n"
            "---\n\n"
            "⚠️ 抓取失败：未能提取正文内容。可能原因：\n"
            "- 文章为付费/关注后可见\n"
            "- 链接已失效或被删除\n"
            "- 微信反爬触发\n"
            "请在微信客户端中打开此链接核实。\n"
            "</wechat_article>"
        )

    truncated = ""
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > ARTICLE_MAX_BYTES:
        body = body_bytes[:ARTICLE_MAX_BYTES].decode("utf-8", errors="ignore")
        truncated = "\n\n_...（正文超过 500KB 已截断）_"

    image_section = ""
    if images:
        items = "\n".join(f"{img['index']}. {img['url']}" for img in images)
        image_section = (
            f"\n\n---\n\n## 图片清单（共 {len(images)} 张）\n{items}\n"
            "\n_要查看某张图片内容，调用 view_wechat_image(url) 工具。_"
        )

    return (
        "<wechat_article>\n"
        f"# {title}\n"
        f"**作者**：{author} ｜ **发布时间**：{publish_time} ｜ **原文**：{source_url}\n\n"
        "---\n\n"
        f"{body}{truncated}"
        f"{image_section}\n"
        "</wechat_article>"
    )


# ---------- Image fetching ----------

def _detect_mime(magic: bytes) -> str | None:
    if len(magic) < 12:
        return None
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if magic.startswith(b"GIF87a") or magic.startswith(b"GIF89a"):
        return "image/gif"
    if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
        return "image/webp"
    return None


def _download_image(url: str) -> tuple[bytes, str]:
    headers = {
        "User-Agent": CHROME_UA,
        # Intentionally NO Referer — mmbiz.qpic.cn returns the placeholder
        # "此图片来自微信公众平台" image when Referer is set to non-WeChat origin.
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=IMAGE_DOWNLOAD_TIMEOUT_S, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Image download failed: {e}")

    cl = resp.headers.get("Content-Length")
    if cl:
        try:
            if int(cl) > IMAGE_MAX_BYTES:
                resp.close()
                raise ValueError(f"Image too large: {cl} bytes (max {IMAGE_MAX_BYTES})")
        except (ValueError, TypeError):
            pass

    raw = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        raw.extend(chunk)
        if len(raw) > IMAGE_MAX_BYTES:
            resp.close()
            raise ValueError(f"Image exceeds {IMAGE_MAX_BYTES} bytes")
    raw_bytes = bytes(raw)

    if not raw_bytes:
        raise RuntimeError("Empty image response")

    mime = _detect_mime(raw_bytes[:16])
    if mime is None:
        raise ValueError("Unrecognized image format (allowed: jpeg/png/gif/webp)")
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError(f"Unsupported image type: {mime}")
    return raw_bytes, mime


def _b64_chars(raw_len: int) -> int:
    """Approximate base64 length for raw_len bytes."""
    return ((raw_len + 2) // 3) * 4


def _to_rgb(img: Image.Image) -> Image.Image:
    """Drop alpha by compositing on white (JPEG can't carry alpha)."""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _shrink_to_budget(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Resize + recompress so base64 fits in TARGET_IMAGE_B64_CHARS.

    GIFs are passed through unmodified to preserve animation, even if oversized.
    Other formats are downsampled to IMAGE_LONG_EDGE_MAX, re-encoded as JPEG with
    descending quality, and as a last resort dimensions are halved.
    """
    if mime == "image/gif":
        return raw, mime  # don't lose animation frames

    try:
        img = Image.open(io.BytesIO(raw))
    except Exception:
        return raw, mime  # opaque to Pillow; return as-is

    w, h = img.size
    long_edge = max(w, h)
    if long_edge > IMAGE_LONG_EDGE_MAX:
        scale = IMAGE_LONG_EDGE_MAX / long_edge
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    img = _to_rgb(img)

    # Quality stepdown — JPEG, optimize, progressive.
    for quality in (85, 70, 55, 40):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
        data = out.getvalue()
        if _b64_chars(len(data)) <= TARGET_IMAGE_B64_CHARS:
            return data, "image/jpeg"

    # Still too big — halve dimensions once more, then return whatever we get.
    nw, nh = max(1, img.size[0] // 2), max(1, img.size[1] // 2)
    img = img.resize((nw, nh), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=60, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


# ---------- MCP server ----------

mcp = FastMCP(
    "wechat-reader",
    instructions=(
        "Read public WeChat Official Account (mp.weixin.qq.com) articles.\n\n"
        "Workflow:\n"
        "1. read_wechat_article(url) returns markdown. Image positions appear "
        "inline as [图片N: <url>]; a full image inventory is appended at the end.\n\n"
        "2. Decide whether to view images by task type, BEFORE drafting:\n"
        "   - Analytical (梳理/分析/对比/数据核验/总结) on a chart-bearing article → "
        "view all data images (tables, comparison charts, rankings, figure "
        "screenshots) up front, in parallel. Authors' textual summaries of their "
        "own charts often round or cherry-pick — verify the source when precision "
        "matters.\n"
        "   - Casual reading / unrelated Q&A → skip images.\n"
        "   - Specific question targeting one chart → view just that chart.\n\n"
        "3. Skip non-data images: 二维码, author headshots, decorative banners, "
        "公众号底图 (often the last 1–2 images).\n\n"
        "Cost note: each image ≈ 5–10K output tokens. Justified for analysis on "
        "chart-heavy articles; not for casual reads."
    ),
)


@mcp.tool()
async def read_wechat_article(url: str) -> str:
    """
    Fetch a public WeChat Official Account article and return its content as markdown.

    Args:
        url: Article URL. Must start with https://mp.weixin.qq.com/s/

    Returns:
        Markdown wrapped in <wechat_article> tags. Image positions appear as
        [图片N: <url>] placeholders; a numbered image list is appended at the end.
        Call view_wechat_image(url) to inspect a specific image's content.
    """
    _check_article_url(url)
    data = await _fetch_article(url)
    return _format_article_markdown(data, url)


@mcp.tool()
async def view_wechat_image(url: str) -> ImageContent:
    """
    Download a single WeChat article image and return it as ImageContent.

    Args:
        url: Image URL on *.qpic.cn (typically mmbiz.qpic.cn). Get URLs from the
             图片清单 section of read_wechat_article output.

    Returns:
        MCP ImageContent (base64 jpeg/png/webp/gif). Long edge auto-downscaled
        to 1568px to keep token cost bounded.
    """
    _check_image_url(url)
    raw, mime = await asyncio.to_thread(_download_image, url)
    raw, mime = await asyncio.to_thread(_shrink_to_budget, raw, mime)
    b64 = base64.b64encode(raw).decode("ascii")
    return ImageContent(type="image", data=b64, mimeType=mime)


if __name__ == "__main__":
    mcp.run(transport="stdio")
