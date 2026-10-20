"""Lambda entry point for POST /query behind the API Gateway HTTP API (payload format 2.0).

Request body: {"question": "..."}. The JWT authorizer has already validated the caller;
the token's subject is logged so every answer can be traced to a user.
"""

import base64
import functools
import json
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from query.answer import answer
from query.config import DEFAULT_QUERY_CONFIG

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent_runtime import AgentsforBedrockRuntimeClient
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

# Bedrock throttles (Amazon Rerank allows 2 requests per minute) are answered with 429.
THROTTLING = frozenset({"ThrottlingException", "TooManyRequestsException"})
RETRY_AFTER_SECONDS = 30
# Fail fast: API Gateway gives up after 30 seconds, so long client-side retries are pointless.
_CLIENT_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 2}, connect_timeout=5, read_timeout=20
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@functools.cache
def _agent_runtime() -> "AgentsforBedrockRuntimeClient":
    return boto3.client("bedrock-agent-runtime", config=_CLIENT_CONFIG)


@functools.cache
def _bedrock_runtime() -> "BedrockRuntimeClient":
    return boto3.client("bedrock-runtime", config=_CLIENT_CONFIG)


def _response(
    status: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(body),
    }


class BadRequest(Exception):
    """The request body is not {"question": "..."}; answered with HTTP 400."""


def _question(event: Mapping[str, Any]) -> str:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BadRequest("body must be JSON") from error
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str):
        raise BadRequest('body must be {"question": "..."}')
    return question


def _caller(event: Mapping[str, Any]) -> str:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return str(claims.get("sub", "unknown"))


def main(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    caller = _caller(event)
    try:
        question = _question(event)
        result = answer(
            question,
            os.environ["KNOWLEDGE_BASE_ID"],
            DEFAULT_QUERY_CONFIG,
            _agent_runtime(),
            _bedrock_runtime(),
        )
    except (BadRequest, ValueError) as error:
        logger.info("rejected request from %s: %s", caller, error)
        return _response(400, {"error": str(error)})
    except ClientError as error:
        if error.response.get("Error", {}).get("Code", "") not in THROTTLING:
            logger.exception("query failed for %s", caller)
            return _response(500, {"error": "internal error"})
        logger.warning("throttled for %s", caller)
        return _response(
            429,
            {"error": "busy, retry shortly"},
            {"retry-after": str(RETRY_AFTER_SECONDS)},
        )
    except Exception:
        logger.exception("query failed for %s", caller)
        return _response(500, {"error": "internal error"})
    logger.info(
        "answered for %s: abstained=%s citations=%d usage=%s",
        caller,
        result.abstained,
        len(result.citations),
        result.usage,
    )
    return _response(200, result.to_dict())
