import asyncio
import logging
from typing import Literal
from enum import Enum

from config.settings import settings

logger = logging.getLogger(__name__)


class ToolName(str, Enum):
    TAVILY = "tavily"
    BRIGHTDATA = "brightdata"
    FIRECRAWL = "firecrawl"
    WEB_FETCH = "web_fetch"


class QuotaManager:
    def __init__(self):
        self._quotas: dict[str, int] = {}
        self._exhausted: set[str] = set()
        self._lock = asyncio.Lock()
        
    async def initialize(self) -> None:
        from db.store import db
        
        rows = await db.fetchall("SELECT tool_name, quota_remaining, exhausted FROM tool_quota")
        for row in rows:
            self._quotas[row["tool_name"]] = row["quota_remaining"]
            if row["exhausted"]:
                self._exhausted.add(row["tool_name"])
        
        if ToolName.TAVILY.value not in self._quotas:
            self._quotas[ToolName.TAVILY.value] = 1000
        
        if ToolName.BRIGHTDATA.value not in self._quotas:
            self._quotas[ToolName.BRIGHTDATA.value] = 500
            
        if ToolName.FIRECRAWL.value not in self._quotas:
            self._quotas[ToolName.FIRECRAWL.value] = 100
    
    async def decrement(self, tool_name: str) -> None:
        async with self._lock:
            if tool_name in self._quotas:
                self._quotas[tool_name] -= 1
                if self._quotas[tool_name] <= 0:
                    self._exhausted.add(tool_name)
                    await self._persist_exhaustion(tool_name)
                    
    async def _persist_exhaustion(self, tool_name: str) -> None:
        from db.store import db
        
        await db.execute(
            """INSERT INTO tool_quota (tool_name, quota_remaining, exhausted)
               VALUES (?, 0, 1)
               ON CONFLICT(tool_name) DO UPDATE SET quota_remaining = 0, exhausted = 1""",
            (tool_name,)
        )
        await db.commit()
        logger.warning(f"Tool {tool_name} quota exhausted")
    
    def is_available(self, tool_name: str) -> bool:
        return tool_name not in self._exhausted
    
    def get_available_tools(self) -> list[str]:
        return [t for t in self._quotas.keys() if t not in self._exhausted]
    
    def get_remaining(self, tool_name: str) -> int:
        return self._quotas.get(tool_name, 0)


quota_manager = QuotaManager()


async def search(query: str) -> tuple[list[dict], str]:
    """
    Priority order:
    1. Tavily (if quota available)
    2. BrightData (fallback)
    """
    from tools.tavily_search import tavily_search
    from tools.brightdata_search import brightdata_search
    
    if quota_manager.is_available(ToolName.TAVILY.value):
        try:
            results = await tavily_search(query)
            await quota_manager.decrement(ToolName.TAVILY.value)
            return results, ToolName.TAVILY.value
        except Exception as e:
            logger.warning(f"Tavily search failed: {e}")
    
    if quota_manager.is_available(ToolName.BRIGHTDATA.value):
        try:
            results = await brightdata_search(query)
            await quota_manager.decrement(ToolName.BRIGHTDATA.value)
            return results, ToolName.BRIGHTDATA.value
        except Exception as e:
            logger.warning(f"BrightData search failed: {e}")
    
    logger.error("All search tools exhausted or unavailable")
    return [], ""


async def fetch(url: str) -> dict:
    """
    Priority order:
    1. Firecrawl (if quota available)
    2. Direct web_fetch (fallback)
    """
    from tools.web_fetch import web_fetch
    from tools.firecrawl_crawl import firecrawl_fetch
    
    if quota_manager.is_available(ToolName.FIRECRAWL.value):
        try:
            result = await firecrawl_fetch(url)
            await quota_manager.decrement(ToolName.FIRECRAWL.value)
            return result
        except Exception as e:
            logger.warning(f"Firecrawl fetch failed: {e}")
    
    return await web_fetch(url)
