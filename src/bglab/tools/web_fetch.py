"""WebFetch 工具 — 对齐 WebFetchTool.ts。

获取 URL 内容并返回 markdown。
"""

from __future__ import annotations

import urllib.request
import urllib.error

from bglab.tools.base import Tool, tool_error


def web_fetch(args: dict) -> str:
    """获取 URL 内容。"""
    url = args.get("url", "")
    if not url:
        return "Error: url is required"
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"

    prompt = args.get("prompt", "")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bglab/0.5"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        # 简单 HTML→text
        import re
        # Remove scripts and styles
        body = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', body, flags=re.IGNORECASE)
        body = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', body, flags=re.IGNORECASE)
        # Remove HTML tags
        body = re.sub(r'<[^>]+>', ' ', body)
        # Collapse whitespace
        body = re.sub(r'\s+', ' ', body).strip()
        # Truncate
        if len(body) > 8000:
            body = body[:8000] + "...(truncated)"

        if prompt:
            return f"Content from {url}:\n\n{body}\n\n---\nInstructions: {prompt}"
        return f"Content from {url}:\n\n{body}"

    except urllib.error.HTTPError:
        return tool_error("WEB_FETCH_HTTP_FAILURE")


WebFetchTool = Tool(
    name="WebFetch",
    searchHint="fetch webpage content from URL",
    description="Fetch content from a URL and process it.",
    prompt="""Fetches content from a specified URL and returns it as text.

Usage notes:
- The URL must be a fully-formed valid URL
- HTTP URLs will be automatically upgraded to HTTPS
- The prompt parameter should describe what information you want to extract from the page
- This tool is read-only and does not modify any files
- Results may be summarized if the content is very large""",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch content from",
                "format": "uri",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to describe what information to extract",
            },
        },
        "required": ["url", "prompt"],
    },
    call=web_fetch,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
