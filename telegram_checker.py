import logging
import aiohttp
from database import db

logger = logging.getLogger(__name__)

DEFAULT_CHECKER_URL = "http://api.agbots.site:8080/check/"
MAX_NUMBERS = 300


async def check_numbers(numbers: list, auth: str = None, api_key: str = None, url: str = None) -> dict | None:
    """Check a list of phone numbers against the Agbots Telegram Checker API.

    API Spec:
    URL: http://api.agbots.site:8080/check/
    Method: GET
    JSON Payload:
      {
          "auth": "<username>",
          "api_key": "<api_key>",
          "phone_numbers": ["+98...", "+49..."]
      }

    Response:
      {
          "status": "200",
          "result_obj": {
              "+989915756448": true / false,
              ...
          }
      }

    Returns:
      A dict {clean_number: status} where status is True (registered/valid),
      False (not registered), or None if request failed.
    """
    if not numbers:
        return None

    # Load active checker credentials if not explicitly passed
    if not auth or not api_key:
        active = db.get_active_checker()
        if not active:
            logger.warning("No active checker configured in database.")
            return None
        auth = auth or active.get("username")
        api_key = api_key or active.get("api_key")
        url = url or active.get("url") or DEFAULT_CHECKER_URL

    target_url = (url or DEFAULT_CHECKER_URL).strip()

    # Normalize phone numbers for sending (ensure '+' prefix)
    formatted_nums = []
    clean_map = {}  # {formatted: clean_digits}
    for n in numbers[:MAX_NUMBERS]:
        raw = str(n).strip()
        if not raw:
            continue
        clean = raw.lstrip("+")
        with_plus = f"+{clean}"
        formatted_nums.append(with_plus)
        clean_map[with_plus] = clean
        clean_map[clean] = clean

    if not formatted_nums:
        return None

    payload = {
        "auth": str(auth).strip(),
        "api_key": str(api_key).strip(),
        "phone_numbers": formatted_nums
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                target_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                status_code = response.status
                text = await response.text()

                if status_code != 200:
                    logger.warning(f"Checker API HTTP {status_code}: {text}")
                    return None

                data = await response.json(content_type=None)

        if str(data.get("status")) != "200":
            logger.warning(f"Checker API error returned: {data}")
            return None

        result_obj = data.get("result_obj") or {}
        output = {}

        # Map results back to clean numbers (and with plus)
        for num_key, val in result_obj.items():
            k_clean = str(num_key).strip().lstrip("+")
            # Map val (True, False, 1, 0, or string)
            output[k_clean] = val
            output[f"+{k_clean}"] = val

        # Ensure all requested numbers have an entry
        for with_plus in formatted_nums:
            clean = clean_map[with_plus]
            if clean not in output and with_plus not in output:
                output[clean] = False
                output[with_plus] = False

        return output
    except Exception as exc:
        logger.error(f"Checker API request failed: {exc}")
        return None
