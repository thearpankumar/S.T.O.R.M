import asyncio
from collections import Counter
from typing import TypeVar, Callable
from pydantic import BaseModel
import logging

from llm.bedrock import structured_call
from config.settings import settings

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


async def n_way_consensus(
    prompt: str,
    response_model: type[T],
    extract_list_fn: Callable[[T], list[str]],
    threshold: int = 2,
    num_calls: int | None = None,
) -> list[str]:
    calls = num_calls or settings.consensus_calls
    
    if calls == 1:
        logger.info(f"Single call for {response_model.__name__}")
        result = await structured_call(prompt, response_model)
        return extract_list_fn(result)
    
    async def single_call(call_id: int) -> T:
        logger.info(f"Consensus call {call_id + 1}/{calls} for {response_model.__name__}")
        await asyncio.sleep(0)
        return await structured_call(prompt, response_model)

    results = await asyncio.gather(*[single_call(i) for i in range(calls)])
    await asyncio.sleep(0)
    
    all_items = []
    for r in results:
        items = extract_list_fn(r)
        all_items.extend(items)
    
    counter = Counter(item.lower().strip() for item in all_items)
    consensus_items = []
    
    seen_normalized = set()
    for item in all_items:
        normalized = item.lower().strip()
        if counter[normalized] >= threshold and normalized not in seen_normalized:
            consensus_items.append(item.strip())
            seen_normalized.add(normalized)
    
    logger.info(f"Consensus: {len(all_items)} total items, {len(consensus_items)} passed threshold")
    return consensus_items


async def three_way_consensus(
    prompt: str,
    response_model: type[T],
    extract_list_fn: Callable[[T], list[str]],
    threshold: int = 2,
) -> list[str]:
    return await n_way_consensus(prompt, response_model, extract_list_fn, threshold, num_calls=3)


async def three_way_consensus_objects(
    prompt: str,
    response_model: type[T],
    merge_fn: Callable[[list[T]], T],
) -> T:
    calls = settings.consensus_calls
    
    async def single_call(call_id: int) -> T:
        logger.info(f"Consensus call {call_id + 1}/{calls} for {response_model.__name__}")
        return await structured_call(prompt, response_model)

    results = await asyncio.gather(*[single_call(i) for i in range(calls)])
    return merge_fn(results)
