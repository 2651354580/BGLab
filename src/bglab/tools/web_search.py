"""WebSearch 工具 — 对齐 WebSearchTool.ts。

调用外部搜索 API 返回结果，让模型能获取最新信息。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.parse

from bglab.tools.base import Tool, tool_error


def web_search(args: dict) -> str:
    """执行 web 搜索。使用 DuckDuckGo Instant Answer API (免费, 无需 key)。"""
    query = args.get("query", "")
    if not query.strip():
        return "Error: query is required"

    allowed_domains = args.get("allowed_domains", [])
    blocked_domains = args.get("blocked_domains", [])

    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "bglab/0.5"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        # Abstract
        abstract = data.get("AbstractText", "")
        if abstract:
            source = data.get("AbstractURL", "")
            results.append(f"**{data.get('Abstract', 'Summary')}**\n{abstract}\nSource: {source}")

        # Related topics
        for topic in data.get("RelatedTopics", [])[:5]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"- {topic['Text']}\n  {topic.get('FirstURL', '')}")

        if not results:
            return f"No results found for: {query}"

        # Domain filtering
        if allowed_domains:
            filtered = []
            for r in results:
                for d in allowed_domains:
                    if d in r:
                        filtered.append(r)
                        break
            results = filtered

        if blocked_domains:
            filtered = []
            for r in results:
                skip = False
                for d in blocked_domains:
                    if d in r:
                        skip = True
                        break
                if not skip:
                    filtered.append(r)
            results = filtered

        if not results:
            return f"No results for: {query} (after domain filtering)"

        return "\n\n".join(results)

    except json.JSONDecodeError:
        return tool_error("WEB_SEARCH_INVALID_RESPONSE")


WebSearchTool = Tool(
    name="WebSearch",
    searchHint="search the web for current information",
    description="Search the web for up-to-date information.",
    prompt="""Allows Claude to search the web and use the results to inform responses.

Usage notes:
- Domain filtering is supported to include or block specific websites
- Returns search result information formatted as search result blocks, including links as markdown hyperlinks
- Use this tool for accessing information beyond Claude's knowledge cutoff""",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to use",
                "minLength": 2,
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only include search results from these domains",
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Never include search results from these domains",
            },
        },
        "required": ["query"],
    },
    call=web_search,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
