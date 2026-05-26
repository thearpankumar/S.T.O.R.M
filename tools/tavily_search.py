import asyncio
import logging
from typing import Any

import httpx

from config.settings import settings
from models.tools import SearchResult

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"


async def tavily_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search using Tavily API.
    
    Returns a list of SearchResult-like dicts with:
    - title: result title
    - url: result URL
    - snippet: short text snippet
    - source: "tavily"
    """
    if not settings.tavily_api_key:
        logger.warning("Tavily API key not configured")
        return []
    
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_raw_content": False,
        "include_answer": True,
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TAVILY_API_URL, json=payload)
            response.raise_for_status()
            
            data = response.json()
            
            results = []
            for item in data.get("results", []):
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                    "snippet": item.get("content", "")[:500],
                    "source": "tavily"
                })
            
            return results
            
    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily API HTTP error: {e.response.status_code}")
        raise
    except httpx.RequestError as e:
        logger.error(f"Tavily API request error: {e}")
        raise
    except Exception as e:
        logger.error(f"Tavily search unexpected error: {e}")
        raise


async def tavily_extract(url: str) -> dict[str, Any]:
    """
    Extract content from a URL using Tavily's extraction API.
    """
    if not settings.tavily_api_key:
        return {"url": url, "content": "", "success": False, "error": "API key not configured"}
    
    extract_url = "https://api.tavily.com/extract"
    
    payload = {
        "api_key": settings.tavily_api_key,
        "urls": [url],
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(extract_url, json=payload)
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            
            if results:
                return {
                    "url": url,
                    "content": results[0].get("raw_content", "")[:10000],
                    "success": True,
                    "error": None
                }
            
            return {"url": url, "content": "", "success": False, "error": "No content extracted"}
            
    except Exception as e:
        logger.error(f"Tavily extract error: {e}")
        return {"url": url, "content": "", "success": False, "error": str(e)}
