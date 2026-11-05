import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

CHECKER_URL = "http://checker.irbots.com:2021/check"
MAX_NUMBERS = 300


async def check_numbers(numbers):
    """Check a list of phone numbers against the Telegram checker API.

    Returns:
        A dict {number: status} where status is True (registered),
        False (not registered) or "error". Keys are normalized (no leading '+').
        Returns None when the API key is not configured or the check fails.
    """
    api_key = os.getenv("CHECKER_API_KEY")
    if not api_key or not numbers:
        return None

    normalized = [str(n).strip().lstrip("+") for n in numbers if str(n).strip()]
    if not normalized:
        return None

    payload = ",".join(normalized[:MAX_NUMBERS])
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                CHECKER_URL,
                params={"key": api_key, "numbers": payload},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    logger.warning(f"Telegram checker API error: HTTP {response.status}")
                    return None
                res = await response.json(content_type=None)

        if res.get("status") != "ok" or not isinstance(res.get("data"), dict):
            logger.warning(f"Telegram checker API bad response: {res.get('msg')}")
            return None

        raw = res["data"]
        for num in normalized:
            match = None
            for key, value in raw.items():
                if str(key).lstrip("+") == num:
                    match = value
                    break
            result[num] = match if match is not None else False
        return result
    except Exception as e:
        logger.error(f"Telegram checker request failed: {e}")
        return None