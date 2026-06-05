import boto3
import json
import asyncio
import threading
from pydantic import BaseModel
from typing import TypeVar
import logging

from botocore.exceptions import ClientError, ConnectTimeoutError
import concurrent.futures

from config.settings import settings

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_bedrock_client = None
_client_lock = threading.Lock()
_llm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="BedrockLLM")


def get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        with _client_lock:
            if _bedrock_client is None:
                from botocore.config import Config
                # max_pool_connections must be >= the number of simultaneous LLM
                # calls we fire (settings.llm_concurrency). The default of 10
                # caused "Connection pool is full" drops when 19 features ran in
                # parallel. We add +4 as headroom for retries and tool calls.
                pool_size = max(settings.llm_concurrency + 4, 16)
                config = Config(
                    read_timeout=300,
                    connect_timeout=30,
                    retries={'max_attempts': 5},
                    max_pool_connections=pool_size,
                )
                logger.info(
                    f"Creating Bedrock client "
                    f"(pool={pool_size}, concurrency={settings.llm_concurrency})"
                )
                _bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name=settings.aws_region,
                    config=config,
                )
    return _bedrock_client



def is_throttling_error(error: ClientError) -> bool:
    error_code = error.response.get("Error", {}).get("Code", "")
    return error_code in ["ThrottlingException", "ServiceUnavailableException", "RequestLimitExceeded"]


async def structured_call(
    prompt: str,
    response_model: type[T],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    system_prompt: str | None = None,
    max_retries: int = 3,
) -> T:
    client = get_bedrock_client()
    schema = response_model.model_json_schema()
    
    messages = []
    if system_prompt:
        messages.append({
            "role": "system",
            "content": [{"text": system_prompt}]
        })
    
    messages.append({
        "role": "user", 
        "content": [{"text": prompt}]
    })
    
    def _sync_call() -> str:
        request_params = {
            "modelId": settings.bedrock_model_id,
            "messages": messages,
            "inferenceConfig": {
                "temperature": temperature,
                "maxTokens": max_tokens
            },
            "outputConfig": {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(schema),
                            "name": response_model.__name__,
                            "description": f"Structured output for {response_model.__name__}"
                        }
                    }
                }
            }
        }
        
        if system_prompt:
            request_params["system"] = [{"text": system_prompt}]
        
        response = client.converse(**request_params)
        return response["output"]["message"]["content"][0]["text"]
    
    last_error = None
    for attempt in range(max_retries):
        try:
            json_str = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(_llm_executor, _sync_call),
                timeout=120.0
            )
            
            logger.debug(f"Bedrock response for {response_model.__name__}: {json_str[:200]}...")
            
            return response_model.model_validate_json(json_str)
        except asyncio.TimeoutError:
            logger.warning(f"LLM call timeout, attempt {attempt + 1}/{max_retries}")
            last_error = TimeoutError("LLM call timed out")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
        except ConnectTimeoutError:
            logger.warning(f"Connection timeout, attempt {attempt + 1}/{max_retries}")
            last_error = ConnectTimeoutError()
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
        except ClientError as e:
            last_error = e
            if is_throttling_error(e):
                wait_time = 2 ** attempt
                logger.warning(f"Throttled, waiting {wait_time}s before retry")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Bedrock client error: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error in structured_call: {e}")
            last_error = e
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    
    raise RuntimeError(f"Failed to get response after {max_retries} attempts: {last_error}")


async def simple_call(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    system_prompt: str | None = None,
    max_retries: int = 3,
) -> str:
    client = get_bedrock_client()
    
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    
    def _sync_call() -> str:
        request_params = {
            "modelId": settings.bedrock_model_id,
            "messages": messages,
            "inferenceConfig": {
                "temperature": temperature,
                "maxTokens": max_tokens
            }
        }
        
        if system_prompt:
            request_params["system"] = [{"text": system_prompt}]
        
        response = client.converse(**request_params)
        return response["output"]["message"]["content"][0]["text"]
    
    for attempt in range(max_retries):
        try:
            return await asyncio.get_running_loop().run_in_executor(_llm_executor, _sync_call)
        except ClientError as e:
            if is_throttling_error(e):
                await asyncio.sleep(2 ** attempt)
            elif attempt < max_retries - 1:
                await asyncio.sleep(1)
            else:
                raise
        except ConnectTimeoutError:
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            else:
                raise
    
    raise RuntimeError(f"Failed after {max_retries} attempts")
