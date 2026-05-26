import asyncio
import logging
from typing import Any

import httpx
from trafilatura import fetch_url, extract

from models.tools import FetchResult

logger = logging.getLogger(__name__)


async def web_fetch(url: str, timeout: int = 30) -> dict[str, Any]:
    """
    Fetch and extract content from a URL using httpx + trafilatura.
    
    Returns a FetchResult-like dict with:
    - url: original URL
    - title: page title
    - content: extracted text content
    - success: bool
    - error: error message if failed
    """
    try:
        loop = asyncio.get_running_loop()
        
        def _sync_fetch() -> str | None:
            return fetch_url(url)
        
        html_content = await loop.run_in_executor(None, _sync_fetch)
        
        if not html_content:
            return {
                "url": url,
                "title": "",
                "content": "",
                "success": False,
                "error": "Failed to fetch URL content"
            }
        
        text_content = extract(html_content, include_comments=False, include_tables=True)
        
        title = ""
        if "<title>" in html_content.lower():
            import re
            title_match = re.search(r"<title[^>]*>([^<]+)</title>", html_content, re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()
        
        return {
            "url": url,
            "title": title,
            "content": text_content or "",
            "success": True,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"web_fetch error for {url}: {e}")
        return {
            "url": url,
            "title": "",
            "content": "",
            "success": False,
            "error": str(e)
        }


async def web_fetch_batch(urls: list[str]) -> list[dict[str, Any]]:
    """
    Fetch multiple URLs in parallel.
    """
    tasks = [web_fetch(url) for url in urls]
    return await asyncio.gather(*tasks)
