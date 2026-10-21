"""固定 Provider 的公共信息搜索；不开放任意 URL 抓取或内部网络访问。"""
import httpx

from src.core.config import get_settings
from src.core.exceptions import AppError, UpstreamError


async def search_public_information(query: str) -> dict:
    settings = get_settings()
    if not settings.EXTERNAL_SEARCH_ENABLED or not settings.TAVILY_API_KEY:
        raise AppError("外部搜索未启用或未配置 Tavily API Key")
    if not query.strip() or len(query) > 300:
        raise AppError("外部搜索关键词须为 1 至 300 字符")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post("https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {settings.TAVILY_API_KEY}"}, json={
                "query": query.strip(),
                "max_results": 3, "include_answer": False, "search_depth": "basic",
            })
            response.raise_for_status()
            payload = response.json()
        results = [{"title": str(item.get("title", ""))[:200],
                    "url": str(item.get("url", ""))[:2000],
                    "content": str(item.get("content", ""))[:1600],
                    "source_type": "public_web"}
                   for item in payload.get("results", [])[:3]
                   if isinstance(item, dict) and str(item.get("url", "")).startswith("https://")]
        return {"results": results, "authoritative_for_policy_price_inventory": False}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise UpstreamError("外部信息搜索失败，请稍后重试") from exc
