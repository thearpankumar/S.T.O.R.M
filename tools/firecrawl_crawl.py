import asyncio
import logging
from typing import Any

import httpx

from config.settings import settings
from models.tools import FetchResult

logger = logging.getLogger(__name__)

FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"


async def firecrawl_fetch(url: str) -> dict[str, Any]:
    """
    Fetch and extract content from a URL using Firecrawl API.
    
    Returns a FetchResult-like dict with:
    - url: original URL
    - title: page title
    - content: extracted markdown content
    - success: bool
    - error: error message if failed
    """
    if not settings.firecrawl_api_key:
        logger.warning("Firecrawl API key not configured, falling back to web_fetch")
        from tools.web_fetch import web_fetch
        return await web_fetch(url)
    
    headers = {
        "Authorization": f"Bearer {settings.firecrawl_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(FIRECRAWL_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("success"):
                result_data = data.get("data", {})
                return {
                    "url": url,
                    "title": result_data.get("metadata", {}).get("title", ""),
                    "content": result_data.get("markdown", ""),
                    "success": True,
                    "error": None
                }
            
            return {
                "url": url,
                "title": "",
                "content": "",
                "success": False,
                "error": data.get("error", "Unknown Firecrawl error")
            }
            
    except httpx.HTTPStatusError as e:
        logger.error(f"Firecrawl API HTTP error: {e.response.status_code}")
        return {"url": url, "title": "", "content": "", "success": False, "error": f"HTTP {e.response.status_code}"}
    except httpx.RequestError as e:
        logger.error(f"Firecrawl API request error: {e}")
        return {"url": url, "title": "", "content": "", "success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Firecrawl unexpected error: {e}")
        return {"url": url, "title": "", "content": "", "success": False, "error": str(e)}


async def firecrawl_crawl(start_url: str, max_pages: int = 10) -> list[dict[str, Any]]:
    """
    Crawl multiple pages starting from a URL using Firecrawl.
    
    Note: This uses the Firecrawl crawl endpoint for deep crawling.
    """
    if not settings.firecrawl_api_key:
        logger.warning("Firecrawl API key not configured")
        return []
    
    crawl_url = "https://api.firecrawl.dev/v1/crawl"
    
    headers = {
        "Authorization": f"Bearer {settings.firecrawl_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "url": start_url,
        "limit": max_pages,
        "scrapeOptions": {
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(crawl_url, json=payload, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            results = []
            if data.get("success"):
                for item in data.get("data", []):
                    results.append({
                        "url": item.get("url", ""),
                        "title": item.get("metadata", {}).get("title", ""),
                        "content": item.get("markdown", ""),
                        "success": True,
                        "error": None
                    })
            
            return results
            
    except Exception as e:
        logger.error(f"Firecrawl crawl error: {e}")
        return []
