"""Tavily Web Search 封装。

设计要点：
1. 仅在配置了 TAVILY_API_KEY 时启用；未配置时返回未启用提示，不影响主流程。
2. 用标准库 urllib + json 调用，避免引入额外依赖（tavily-python 等第三方包）。
3. 失败友好降级：网络异常 / 限流 / 无结果时返回结构化错误信息，不抛异常打断 agent。
4. 默认只取 top 5 结果，每条带标题/链接/内容片段，与知识库召回格式对齐，方便 LLM 引用。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import settings

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_MAX_RESULTS = 5
REQUEST_TIMEOUT = 15  # 秒


@dataclass
class TavilyResult:
    """单条搜索结果。"""

    title: str
    url: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "content": self.content}


@dataclass
class TavilyResponse:
    """一次搜索的整体响应。"""

    results: list[TavilyResult]
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def is_enabled() -> bool:
    """是否启用了 Tavily（配置了 API Key）。"""
    return bool(settings.tavily_api_key)


def search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> TavilyResponse:
    """调用 Tavily API 进行网络搜索。

    Args:
        query: 搜索关键词。
        max_results: 最多返回结果数（默认 5）。

    Returns:
        TavilyResponse：
        - 成功：results 非空，error 为空
        - 失败：results 为空，error 含原因描述
    """
    if not is_enabled():
        return TavilyResponse(results=[], error="Tavily 未配置 API Key，无法进行网络搜索。")

    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": int(max_results),
        "search_depth": "basic",
    }
    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # 限流 / 鉴权失败等
        err_msg = f"Tavily HTTP {e.code}"
        try:
            err_body = json.loads(e.read().decode("utf-8", errors="ignore"))
            if isinstance(err_body, dict) and err_body.get("detail"):
                err_msg += f": {err_body['detail']}"
        except Exception:
            pass
        return TavilyResponse(results=[], error=err_msg)
    except urllib.error.URLError as e:
        return TavilyResponse(results=[], error=f"网络异常: {e.reason}")
    except Exception as e:
        return TavilyResponse(results=[], error=f"未知错误: {e}")

    try:
        data_obj: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return TavilyResponse(results=[], error="响应解析失败")

    raw_results = data_obj.get("results") or []
    results: list[TavilyResult] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        results.append(
            TavilyResult(
                title=str(r.get("title") or "未命名"),
                url=str(r.get("url") or ""),
                content=str(r.get("content") or ""),
            )
        )
    if not results:
        return TavilyResponse(results=[], error="未搜索到相关结果")
    return TavilyResponse(results=results)


def format_results(resp: TavilyResponse) -> str:
    """把搜索结果格式化为给 LLM 看的文本（带 [来源N] 标号，便于引用）。"""
    if not resp.ok:
        return f"网络搜索失败：{resp.error}"
    if not resp.results:
        return "网络搜索未返回结果。"
    blocks = []
    for i, r in enumerate(resp.results, start=1):
        blocks.append(f"[来源{i}] (网络) {r.title}\nURL: {r.url}\n{r.content}")
    return "\n\n".join(blocks)
