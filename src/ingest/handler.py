import json
import logging
import os
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def main(event: dict[str, Any], _context: object) -> dict[str, Any]:
    logger.info(json.dumps({"event": event, "raw_bucket": os.environ["RAW_BUCKET"]}))
    return {"status": "ok"}
