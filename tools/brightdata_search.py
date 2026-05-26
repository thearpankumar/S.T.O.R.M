import asyncio
import logging
from typing import Any

import httpx

from config.settings import settings
from models.tools import SearchResult

logger = logging.getLogger(__name__)


async def brightdata_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search using Bright Data's SERP API.
    
    Returns a list of SearchResult-like dicts with:
    - title: result title
    - url: result URL
    - snippet: short text snippet
    - source: "brightdata"
    """
    if not settings.brightdata_api_key:
        logger.warning("Bright Data API key not configured")
        return []
    
    serp_api_url = "https://api.brightdata.com/serp"
    
    headers = {
        "Authorization": f"Bearer {settings.brightdata_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "query": query,
        "num_results": max_results,
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(serp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            results = []
            organic_results = data.get("organic_results", [])
            
            for item in organic_results[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", "")[:500],
                    "source": "brightdata"
                })
            
            return results
            
    except httpx.HTTPStatusError as e:
        logger.error(f"Bright Data API HTTP error: {e.response.status_code}")
        raise
    except httpx.RequestError as e:
        logger.error(f"Bright Data API request error: {e}")
        raise
    except Exception as e:
        logger.error(f"Bright Data search unexpected error: {e}")
        raise


async def brightdata_scrape(url: str) -> dict[str, Any]:
    """
    Scrape a URL using Bright Data's scraping API.
    """
    if not settings.brightdata_api_key:
        return {"url": url, "content": "", "success": False, "error": "API key not configured"}
    
    scrape_url = "https://api.brightdata.com/scrape"
    
    headers = {
        "Authorization": f"Bearer {settings.brightdata_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "url": url,
        "render": False,
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(scrape_url, json=payload, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            return {
                "url": url,
                "content": data.get("content", "")[:10000],
                "success": True,
                "error": None
            }
            
    except Exception as e:
        logger.error(f"Bright Data scrape error: {e}")
        return {"url": url, "content": "", "success": False, "error": str(e)}
