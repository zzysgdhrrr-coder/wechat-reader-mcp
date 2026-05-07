# wechat-reader-mcp

A minimal local MCP server that lets Claude Code read public WeChat Official
Account articles (mp.weixin.qq.com) and view their images.

## What it does

- `read_wechat_article(url)` — fetches an article via headless Chromium and
  returns it as markdown with image placeholders and a numbered image list.
- `view_wechat_image(url)` — downloads one image from `mmbiz.qpic.cn` and
  returns it as MCP `ImageContent` so Claude can actually look at it.

## Why local-first

Many existing WeChat scrapers route requests through third-party extraction
services, which means the URLs you read and the full article bodies leave
your machine. This server runs **100% locally**:

- Article extraction uses your own headless Chromium via Playwright.
- Image downloads go directly to `mmbiz.qpic.cn` with the `Referer` header
  omitted to bypass hotlink protection — no third-party image proxy.
- Nothing is persisted: no inbox, no cache, no cookies between calls.

## Install

```bash
git clone https://github.com/zzysgdhrrr-coder/wechat-reader-mcp.git
cd wechat-reader-mcp

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install "mcp[cli]>=1.0" "playwright>=1.40" "requests>=2.28" "idna>=3.6" "Pillow>=10.0"
.venv/bin/playwright install chromium
```

## Wire up to Claude Code

Add this to `~/.claude/settings.json` under `mcpServers` (replace
`/absolute/path/to/wechat-reader-mcp` with where you cloned the repo):

```json
{
  "mcpServers": {
    "wechat-reader": {
      "command": "/absolute/path/to/wechat-reader-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/wechat-reader-mcp/server.py"]
    }
  }
}
```

Restart Claude Code.

## Use

In Claude Code:

```text
读这篇文章 https://mp.weixin.qq.com/s/xxx，用三句话总结
读这篇文章 https://mp.weixin.qq.com/s/xxx，把第二张图里的财务数据列出来
```

Claude will call `read_wechat_article` first, then `view_wechat_image` on
specific images only when the question requires visual content.

## Security model

- **Domain whitelist**: `read_wechat_article` only accepts
  `https://mp.weixin.qq.com/s/...`; `view_wechat_image` only accepts
  `*.qpic.cn` URLs. Anything else is rejected.
- **SSRF protection**: hostnames are resolved and any private/loopback/
  link-local/cloud-metadata IP is blocked. IDN homograph attacks rejected.
- **Process isolation**: each article fetch starts a fresh Chromium context,
  no cookies persist between calls.
- **Size caps**: article body capped at 500 KB, single image at 5 MB.
- **No SVG**: SVG image content is rejected (causes Claude Code session
  crashes — see anthropics/claude-code#28279).
- **Token-aware resize**: images are downscaled to 2576px long edge before
  base64 encoding to bound LLM token cost.
- **Trust boundary marker**: article markdown is wrapped in
  `<wechat_article>...</wechat_article>` to remind the LLM the content is
  external data, not instructions (basic prompt-injection hygiene).

## Limits

- **Public articles only.** Paid / followers-only / 关注后可见 articles cannot
  be fetched — there is no login flow on purpose, to avoid storing your
  WeChat session cookie.
- **No comments / video.** Article body only.
- **Hotlink-protected placeholder image** is a known failure mode: if WeChat
  returns the "此图片来自微信公众平台" placeholder instead of the real image,
  the response will look unusually small (< 5 KB).
