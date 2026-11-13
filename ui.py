import os
import io
import urllib.request
import logging
import re
import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, CopyTextButton

COUNTRY_FLAGS = {
    "GERMANY": "🇩🇪", "AFGHANISTAN": "🇦🇫", "ALBANIA": "🇦🇱", "ALGERIA": "🇩🇿",
    "BELARUS": "🇧🇾", "CENTRAL AFRICA": "🇨🇫", "GUATEMALA": "🇬🇹", "HAITI": "🇭🇹",
    "ITALY": "🇮🇹", "MALAYSIA": "🇲🇾", "MOROCCO": "🇲🇦", "MYANMAR": "🇲🇲",
    "NIGERIA": "🇳🇬", "SOUTH SUDAN": "🇸🇸", "SYRIA": "🇸🇾", "TANZANIA": "🇹🇿",
    "TUNISIA": "🇹🇳", "UKRAINE": "🇺🇦", "UZBEKISTAN": "🇺🇿", "YEMEN": "🇾🇪",
    "SRI LANKA": "🇱🇰", "INDIA": "🇮🇳", "BANGLADESH": "🇧🇩", "PAKISTAN": "🇵🇰",
    "INDONESIA": "🇮🇩", "RUSSIA": "🇷🇺", "VIETNAM": "🇻🇳", "PHILIPPINES": "🇵🇭",
    "UNITED STATES": "🇺🇸", "UNITED KINGDOM": "🇬🇧", "FRANCE": "🇫🇷", "SPAIN": "🇪🇸",
    "TOGO": "🇹🇬", "BENIN": "🇧🇯", "GHANA": "🇬🇭", "IVORY COAST": "🇨🇮",
    "CAMEROON": "🇨🇲", "KENYA": "🇰🇪", "UGANDA": "🇺🇬", "SENEGAL": "🇸🇳",
    "MALI": "🇲🇱", "BURKINA FASO": "🇧🇫", "NIGER": "🇳🇪", "GUINEA": "🇬🇳",
    "ZAMBIA": "🇿🇲", "ZIMBABWE": "🇿🇼", "SOUTH AFRICA": "🇿🇦", "EGYPT": "🇪🇬",
    "TURKEY": "🇹🇷", "BRAZIL": "🇧🇷", "COLOMBIA": "🇨🇴", "ARGENTINA": "🇦🇷",
    "MEXICO": "🇲🇽", "CANADA": "🇨🇦", "AUSTRALIA": "🇦🇺", "POLAND": "🇵🇱",
    "NETHERLANDS": "🇳🇱", "BELGIUM": "🇧🇪", "SWITZERLAND": "🇨🇭", "AUSTRIA": "🇦🇹",
    "SWEDEN": "🇸🇪", "NORWAY": "🇳🇴", "DENMARK": "🇩🇰", "FINLAND": "🇫🇮",
    "PORTUGAL": "🇵🇹", "GREECE": "🇬🇷", "CZECH REPUBLIC": "🇨🇿", "ROMANIA": "🇷🇴",
    "HUNGARY": "🇭🇺", "THAILAND": "🇹🇭", "MALDIVES": "🇲🇻", "NEPAL": "🇳🇵",
    "SAUDI ARABIA": "🇸🇦", "UAE": "🇦🇪", "IRAQ": "🇮🇶", "IRAN": "🇮🇷",
    "JORDAN": "🇯🇴", "KUWAIT": "🇰🇼", "OMAN": "🇴🇲", "QATAR": "🇶🇦",
    "JAPAN": "🇯🇵", "SOUTH KOREA": "🇰🇷", "CHINA": "🇨🇳", "HONG KONG": "🇭🇰",
    "TAIWAN": "🇹🇼", "SINGAPORE": "🇸🇬", "CAMBODIA": "🇰🇭", "LAOS": "🇱🇦"
}

PREFIX_FLAGS = {
    # North America
    "1": "🇺🇸", "1242": "🇧🇸", "1246": "🇧🇧", "1264": "🇦🇮", "1268": "🇦🇬",
    "1284": "🇻🇬", "1340": "🇻🇮", "1345": "🇰🇾", "1441": "🇧🇲", "1473": "🇬🇩",
    "1649": "🇹🇨", "1664": "🇲🇸", "1670": "🇲🇵", "1671": "🇬🇺", "1684": "🇦🇸",
    "1721": "🇸🇽", "1758": "🇱🇨", "1767": "🇩🇲", "1784": "🇻🇨", "1787": "🇵🇷",
    "1809": "🇩🇴", "1829": "🇩🇴", "1849": "🇩🇴", "1868": "🇹🇹", "1869": "🇰🇳",
    "1876": "🇯🇲",
    # Europe & Eurasia
    "7": "🇷🇺", "30": "🇬🇷", "31": "🇳🇱", "32": "🇧🇪", "33": "🇫🇷", "34": "🇪🇸",
    "36": "🇭🇺", "39": "🇮🇹", "40": "🇷🇴", "41": "🇨🇭", "43": "🇦🇹", "44": "🇬🇧",
    "45": "🇩🇰", "46": "🇸🇪", "47": "🇳🇴", "48": "🇵🇱", "49": "🇩🇪",
    "350": "🇬🇮", "351": "🇵🇹", "352": "🇱🇺", "353": "🇮🇪", "354": "🇮🇸",
    "355": "🇦🇱", "356": "🇲🇹", "357": "🇨🇾", "358": "🇫🇮", "359": "🇧🇬",
    "370": "🇱🇹", "371": "🇱🇻", "372": "🇪🇪", "373": "🇲🇩", "374": "🇦🇲",
    "375": "🇧🇾", "376": "🇦🇩", "377": "🇲🇨", "378": "🇸🇲", "380": "🇺🇦",
    "381": "🇷🇸", "382": "🇲🇪", "383": "🇽🇰", "385": "🇭🇷", "386": "🇸🇮",
    "387": "🇧🇦", "389": "🇲🇰", "420": "🇨🇿", "421": "🇸🇰", "423": "🇱🇮",
    # South & Central America
    "51": "🇵🇪", "52": "🇲🇽", "53": "🇨🇺", "54": "🇦🇷", "55": "🇧🇷", "56": "🇨🇱",
    "57": "🇨🇴", "58": "🇻🇪", "500": "🇫🇰", "501": "🇧🇿", "502": "🇬🇹",
    "503": "🇸🇻", "504": "🇭🇳", "505": "🇳🇮", "506": "🇨🇷", "507": "🇵🇦",
    "508": "🇵🇲", "509": "🇭🇹", "590": "🇬🇵", "591": "🇧🇴", "592": "🇬🇾",
    "593": "🇪🇨", "594": "🇬🇫", "595": "🇵🇾", "596": "🇲🇶", "597": "🇸🇷",
    "598": "🇺🇾", "599": "🇨🇼",
    # Asia & Oceania
    "60": "🇲🇾", "61": "🇦🇺", "62": "🇮🇩", "63": "🇵🇭", "64": "🇳🇿", "65": "🇸🇬",
    "66": "🇹🇭", "81": "🇯🇵", "82": "🇰🇷", "84": "🇻🇳", "86": "🇨🇳",
    "90": "🇹🇷", "91": "🇮🇳", "92": "🇵🇰", "93": "🇦🇫", "94": "🇱🇰", "95": "🇲🇲",
    "98": "🇮🇷", "670": "🇹🇱", "672": "🇳🇫", "673": "🇧🇳", "674": "🇳🇷",
    "675": "🇵🇬", "676": "🇹🇴", "677": "🇸🇧", "678": "🇻🇺", "679": "🇫🇯",
    "680": "🇵🇼", "681": "🇼🇫", "682": "🇨🇰", "683": "🇳🇺", "685": "🇼🇸",
    "686": "🇰🇮", "687": "🇳🇨", "688": "🇹🇻", "689": "🇵🇫", "690": "🇹🇰",
    "691": "🇫🇲", "692": "🇲🇭", "850": "🇰🇵", "852": "🇭🇰", "853": "🇲🇴",
    "855": "🇰🇭", "856": "🇱🇦", "880": "🇧🇩", "886": "🇹🇼", "960": "🇲🇻",
    "961": "🇱🇧", "962": "🇯🇴", "963": "🇸🇾", "964": "🇮🇶", "965": "🇰🇼",
    "966": "🇸🇦", "967": "🇾🇪", "968": "🇴🇲", "970": "🇵🇸", "971": "🇦🇪",
    "972": "🇮🇱", "973": "🇧🇭", "974": "🇶🇦", "975": "🇧🇹", "976": "🇲🇳",
    "977": "🇳🇵", "992": "🇹🇯", "993": "🇹🇲", "994": "🇦🇿", "995": "🇬🇪",
    "996": "🇰🇬", "998": "🇺🇿",
    # Africa
    "20": "🇪🇬", "27": "🇿🇦", "211": "🇸🇸", "212": "🇲🇦", "213": "🇩🇿",
    "216": "🇹🇳", "218": "🇱🇾", "220": "🇬🇲", "221": "🇸🇳", "222": "🇲🇷",
    "223": "🇲🇱", "224": "🇬🇳", "225": "🇨🇮", "226": "🇧🇫", "227": "🇳🇪",
    "228": "🇹🇬", "229": "🇧🇯", "230": "🇲🇺", "231": "🇱🇷", "232": "🇸🇱",
    "233": "🇬🇭", "234": "🇳🇬", "235": "🇹🇩", "236": "🇨🇫", "237": "🇨🇲",
    "238": "🇨🇻", "239": "🇸🇹", "240": "🇬🇶", "241": "🇬🇦", "242": "🇨🇬",
    "243": "🇨🇩", "244": "🇦🇴", "245": "🇬🇼", "246": "🇮🇴", "247": "🇦🇨",
    "248": "🇸🇨", "249": "🇸🇩", "250": "🇷🇼", "251": "🇪🇹", "252": "🇸🇴",
    "253": "🇩🇯", "254": "🇰🇪", "255": "🇹🇿", "256": "🇺🇬", "257": "🇧🇮",
    "258": "🇲🇿", "260": "🇿🇲", "261": "🇲🇬", "262": "🇷🇪", "263": "🇿🇼",
    "264": "🇳🇦", "265": "🇲🇼", "266": "🇱🇸", "267": "🇧🇼", "268": "🇸🇿",
    "269": "🇰🇲", "290": "🇸🇭", "291": "🇪🇷"
}

PREFIX_COUNTRIES = {
    # North America
    "1": "UNITED STATES",
    # Europe & Eurasia
    "7": "RUSSIA", "30": "GREECE", "31": "NETHERLANDS", "32": "BELGIUM", "33": "FRANCE", "34": "SPAIN",
    "36": "HUNGARY", "39": "ITALY", "40": "ROMANIA", "41": "SWITZERLAND", "43": "AUSTRIA", "44": "UNITED KINGDOM",
    "45": "DENMARK", "46": "SWEDEN", "47": "NORWAY", "48": "POLAND", "49": "GERMANY",
    "351": "PORTUGAL", "358": "FINLAND", "375": "BELARUS", "380": "UKRAINE", "420": "CZECH REPUBLIC",
    # South & Central America
    "51": "PERU", "52": "MEXICO", "53": "CUBA", "54": "ARGENTINA", "55": "BRAZIL", "56": "CHILE",
    "57": "COLOMBIA", "58": "VENEZUELA", "502": "GUATEMALA", "509": "HAITI",
    # Asia & Oceania
    "60": "MALAYSIA", "61": "AUSTRALIA", "62": "INDONESIA", "63": "PHILIPPINES", "65": "SINGAPORE",
    "66": "THAILAND", "81": "JAPAN", "82": "SOUTH KOREA", "84": "VIETNAM", "86": "CHINA",
    "852": "HONG KONG", "855": "CAMBODIA", "856": "LAOS", "880": "BANGLADESH", "886": "TAIWAN",
    "90": "TURKEY", "91": "INDIA", "92": "PAKISTAN", "93": "AFGHANISTAN", "94": "SRI LANKA",
    "95": "MYANMAR", "961": "LEBANON", "962": "JORDAN", "963": "SYRIA", "964": "IRAQ",
    "965": "KUWAIT", "966": "SAUDI ARABIA", "967": "YEMEN", "968": "OMAN", "971": "UAE",
    "974": "QATAR", "977": "NEPAL", "998": "UZBEKISTAN",
    # Africa
    "20": "EGYPT", "27": "SOUTH AFRICA", "211": "SOUTH SUDAN", "212": "MOROCCO", "213": "ALGERIA",
    "216": "TUNISIA", "218": "LIBYA", "220": "GAMBIA", "221": "SENEGAL", "222": "MAURITANIA",
    "223": "MALI", "224": "GUINEA", "225": "IVORY COAST", "226": "BURKINA FASO", "227": "NIGER",
    "228": "TOGO", "229": "BENIN", "230": "MAURITIUS", "231": "LIBERIA", "232": "SIERRA LEONE",
    "233": "GHANA", "234": "NIGERIA", "235": "CHAD", "236": "CENTRAL AFRICA", "237": "CAMEROON",
    "243": "DR CONGO", "244": "ANGOLA", "254": "KENYA", "255": "TANZANIA", "256": "UGANDA",
    "260": "ZAMBIA", "263": "ZIMBABWE"
}

def get_country_details(country_name=None, phone_number=None):
    """Resolve (flag, country_name) from country name or phone number prefix."""
    clean = str(phone_number or "").strip().lstrip("+")
    name = (country_name or "").strip().upper()

    if not name and clean:
        for p_len in [4, 3, 2, 1]:
            p = clean[:p_len]
            if p in PREFIX_COUNTRIES:
                name = PREFIX_COUNTRIES[p]
                break

    flag = "🌍"
    if name and name in COUNTRY_FLAGS:
        flag = COUNTRY_FLAGS[name]
    elif clean:
        for p_len in [4, 3, 2, 1]:
            p = clean[:p_len]
            if p in PREFIX_FLAGS:
                flag = PREFIX_FLAGS[p]
                break

    return flag, (name or "UNKNOWN")

def get_country_flag(country_name=None, phone_number=None):
    """Resolve country flag from country name or phone number prefix."""
    flag, _ = get_country_details(country_name=country_name, phone_number=phone_number)
    return flag

def detect_sms_language(sms_text=""):
    """Detect language of incoming SMS message."""
    t = str(sms_text or "").lower()
    if any(w in t for w in ["votre code", "confirmation", "ne le partagez", "bienvenue", "bonjour"]):
        return "FRENCH"
    if any(w in t for w in ["your code", "verification code", "do not share", "welcome", "is your"]):
        return "ENGLISH"
    if any(w in t for w in ["tu codigo", "su codigo", "no compartas"]):
        return "SPANISH"
    if any(w in t for w in ["seu codigo", "nao compartilhe"]):
        return "PORTUGUESE"
    if any(w in t for w in ["ваш код", "код подтверждения"]):
        return "RUSSIAN"
    if any(w in t for w in ["رمز"]):
        return "ARABIC"
    return None

def get_main_menu_keyboard():
    """Bottom reply keyboard shown in User Mode with color styles."""
    keyboard = [
        [
            KeyboardButton("📱 GET NUMBER", style="primary"),
            KeyboardButton("🪵 OTP HISTORY", style="danger")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_reply_keyboard(checker_enabled=False):
    """Bottom reply keyboard shown in Admin Mode with color styles."""
    checker_toggle_text = "🟢 Checker: ON" if checker_enabled else "🔴 Checker: OFF"
    checker_style = "success" if checker_enabled else "danger"
    keyboard = [
        [
            KeyboardButton("➕ Upload Numbers", style="primary"),
            KeyboardButton("📢 Broadcast Message", style="primary")
        ],
        [
            KeyboardButton("🌐 Manage Panels", style="primary"),
            KeyboardButton("📊 Reports", style="primary")
        ],
        [
            KeyboardButton("⚙️ Checker System", style="primary"),
            KeyboardButton(checker_toggle_text, style=checker_style)
        ],
        [
            KeyboardButton("➕ Add Service", style="success"),
            KeyboardButton("🗑 Remove Service", style="danger")
        ],
        [
            KeyboardButton("🗑 Remove Numbers", style="danger"),
            KeyboardButton("🗑 Reset DB", style="danger")
        ],
        [
            KeyboardButton("👤 User Mode", style="danger")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def format_welcome_message():
    """Welcome message from Image 1"""
    return (
        "╔══════════════════════╗\n"
        "│   👑 NUMBER BOT\n"
        "╚══════════════════════╝\n\n"
        "🌚 Welcome to Number & OTP Service\n"
        "──────────────────────\n"
        "✅ Choose an option below\n"
        "to continue using the bot.\n"
        "──────────────────────\n"
        "💎 Premium OTP Service"
    )

def get_service_selection_keyboard(services=None):
    """Inline keyboard for selecting services with styled buttons.
    Supports dynamic services list: list of dicts [{'label': ..., 'name': ...}] or fallback defaults.
    """
    if not services:
        services = [
            {"label": "💬 IMO", "name": "IMO"},
            {"label": "🫆 TELEGRAM", "name": "TELEGRAM"},
            {"label": "🧊 VK", "name": "VK"},
            {"label": "🟢 WHATSAPP", "name": "WHATSAPP"}
        ]
    keyboard = []
    row = []
    for svc in services:
        if isinstance(svc, dict):
            label = svc.get("label", svc.get("name", ""))
            val = svc.get("name", "").lower()
        elif isinstance(svc, (list, tuple)):
            label, val = svc[0], str(svc[1]).lower()
        else:
            label, val = str(svc), str(svc).lower()
        style = "success" if "whatsapp" in val else "primary"
        row.append(InlineKeyboardButton(label, callback_data=f"service_{val}", style=style))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

def get_country_selection_keyboard(service, active_countries=None):
    """Inline keyboard for selecting countries"""
    if active_countries is None:
        countries = []
    else:
        countries = active_countries
    
    keyboard = []
    # Group into pairs of 2 columns
    for i in range(0, len(countries), 2):
        row = []
        row.append(InlineKeyboardButton(countries[i][0], callback_data=f"country_{countries[i][1]}", style="primary"))
        if i + 1 < len(countries):
            row.append(InlineKeyboardButton(countries[i+1][0], callback_data=f"country_{countries[i+1][1]}", style="primary"))
        keyboard.append(row)
        
    # Change service button at the bottom
    keyboard.append([InlineKeyboardButton("⬅️ Change Service", callback_data="change_service", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def _status_icon(status):
    """Map a checker status to its exact visual icon:
    🟢 Fresh / Unoccupied (PHONE_NUMBER_UNOCCUPIED, False, 'unoccupied', 'fresh')
    🔒 Used / Occupied (PHONE_NUMBER_OCCUPIED, True, 'occupied') -> 🔒 lock icon
    🔴 Ban / Banned / Invalid (PHONE_NUMBER_BANNED, 'banned', 'invalid') -> 🔴 red dot
    ❄️ Frozen / Suspended ('frozen', 'freeze', 'cold') -> ❄️ snowflake icon
    🟡 Unknown / Error / Timeout
    """
    if status is None:
        return "🟡"

    s = str(status).strip().upper()
    
    # 1. Fresh / Unregistered on Telegram (Available)
    if s in ("PHONE_NUMBER_UNOCCUPIED", "UNOCCUPIED", "FRESH", "FALSE", "0", "NO"):
        return "🟢"
        
    # 2. Used / Occupied (Already has Telegram account)
    if s in ("PHONE_NUMBER_OCCUPIED", "OCCUPIED", "TRUE", "1", "YES", "USED"):
        return "🔒"
        
    # 3. Frozen
    if "FROZEN" in s or "FREEZE" in s or "SUSPEND" in s:
        return "❄️"

    # 4. Banned / Invalid
    if "BAN" in s or "INVALID" in s or "BLOCK" in s:
        return "🔴"

    return "🟡"

def build_available_numbers_rich_blocks(service_name, numbers=None, mode="RANDOM", prefix=None):
    """Build rich message blocks for available numbers matching screenshot."""
    mode_str = str(mode or "RANDOM").upper()
    if prefix and mode_str == "PREFIX":
        mode_text = f"🎯 MODE: SET PREFIX ({prefix})"
    else:
        mode_text = "🎲 MODE: RANDOM"

    header_text = f"📱 AVAILABLE NUMBERS ({service_name.upper()}) 📱\n\n{mode_text}"

    table_cells = [
        [
            {
                "text": header_text,
                "is_header": True,
                "align": "center"
            }
        ]
    ]

    clean_nums = []
    if numbers:
        for num in numbers:
            num_str = str(num).strip()
            formatted_num = num_str if num_str.startswith("+") else f"+{num_str}"
            clean_nums.append(formatted_num)
            table_cells.append([
                {
                    "text": {
                        "type": "button",
                        "button": {
                            "text": formatted_num,
                            "copy_text": {
                                "text": formatted_num
                            }
                        }
                    },
                    "align": "left"
                }
            ])
    else:
        table_cells.append([
            {
                "text": f"⚠️ No numbers available for {service_name.upper()}",
                "align": "center"
            }
        ])

    otp_grp = os.getenv("OTP_GROUP_URL", "https://t.me/+ZTN2ldN9repmNWNl")
    copy_text_val = "\n".join(clean_nums) if clean_nums else ""

    button_cells = [
        [
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "COPY ALL",
                        "copy_text": {"text": copy_text_val}
                    }
                },
                "align": "center",
                "colspan": 2
            }
        ],
        [
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "NEXT NUMBER",
                        "callback_data": "next_numbers"
                    }
                },
                "align": "center"
            },
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "RANDOM NUMBER",
                        "callback_data": "random_number"
                    }
                },
                "align": "center"
            }
        ],
        [
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "CHANGE SERVICE",
                        "callback_data": "change_service"
                    }
                },
                "align": "center"
            },
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "SET PREFIX",
                        "callback_data": "prefix_filter"
                    }
                },
                "align": "center"
            }
        ],
        [
            {
                "text": {
                    "type": "button",
                    "button": {
                        "text": "OTP GROUP",
                        "url": otp_grp
                    }
                },
                "align": "center",
                "colspan": 2
            }
        ]
    ]

    return [
        {
            "type": "table",
            "is_bordered": True,
            "cells": table_cells
        },
        {
            "type": "table",
            "is_bordered": False,
            "cells": button_cells
        }
    ]

async def send_rich_available_numbers_message(bot_token, chat_id, service_name, numbers=None, mode="RANDOM", prefix=None, session=None) -> int | None:
    """Send available numbers as Rich Message table with buttons. Returns message_id or None."""
    blocks = build_available_numbers_rich_blocks(service_name, numbers=numbers, mode=mode, prefix=prefix)
    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "blocks": blocks
        }
    }
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
        
    try:
        async with session.post(f"https://api.telegram.org/bot{bot_token}/sendRichMessage", json=payload, timeout=10) as resp:
            data = await resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            logger.error(f"sendRichMessage failed: {data}")
            return None
    except Exception as e:
        logger.error(f"Error in send_rich_available_numbers_message: {e}")
        return None
    finally:
        if close_session:
            await session.close()

async def edit_rich_available_numbers_message(bot_token, chat_id, message_id, service_name, numbers=None, mode="RANDOM", prefix=None, session=None) -> bool:
    """Edit an existing message to Rich Message table with buttons. Returns True on success."""
    blocks = build_available_numbers_rich_blocks(service_name, numbers=numbers, mode=mode, prefix=prefix)
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {
            "blocks": blocks
        }
    }
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
        
    try:
        async with session.post(f"https://api.telegram.org/bot{bot_token}/editMessageText", json=payload, timeout=10) as resp:
            data = await resp.json()
            if data.get("ok"):
                return True
            logger.error(f"editMessageText (rich) failed: {data}")
            return False
    except Exception as e:
        logger.error(f"Error in edit_rich_available_numbers_message: {e}")
        return False
    finally:
        if close_session:
            await session.close()

def format_available_numbers_message(service_name, mode="RANDOM", prefix=None):
    """Header message for available numbers matching screenshot format."""
    msg = f"📱 AVAILABLE NUMBERS ({service_name.upper()}) 📱\n\n"
    if prefix and str(mode).upper() == "PREFIX":
        msg += f"🎯 MODE: SET PREFIX ({prefix})"
    else:
        msg += "🎲 MODE: RANDOM"
    return msg

def get_available_numbers_keyboard(numbers=None, country=None, statuses=None):
    """Fallback inline keyboard under the available numbers list matching Image:
    - Number buttons are primary (cyan/blue)
    - If statuses are present, a 🟢 (fresh) or 🔴 (occupied) dot emoji is displayed at the end
    - Number format has '+' (e.g. +213553686912)
    - Copy All button is primary
    - Next Numbers & Random Number are success (green)
    - Change Service & Set Prefix are danger (red/orange)
    - OTP Group is primary
    """
    keyboard = []
    
    # 1. Number buttons with copy feature
    if numbers:
        formatted_nums = []
        for num in numbers:
            num_str = str(num).strip()
            formatted_num = num_str if num_str.startswith("+") else f"+{num_str}"
            formatted_nums.append(formatted_num)
            clean_num = num_str.lstrip("+")
            
            flag = get_country_flag(country, num_str)
            
            if statuses and clean_num in statuses:
                stat = statuses.get(clean_num)
                status_icon = f" {_status_icon(stat)}"
            else:
                status_icon = ""
            
            btn_text = f"{flag} {formatted_num}{status_icon}"
            keyboard.append([
                InlineKeyboardButton(
                    btn_text,
                    copy_text=CopyTextButton(text=formatted_num),
                    style="primary"
                )
            ])
            
        if formatted_nums:
            keyboard.append([
                InlineKeyboardButton(
                    "📋 Copy All",
                    copy_text=CopyTextButton(text="\n".join(formatted_nums)),
                    style="primary"
                )
            ])
            
    # 2. Control navigation buttons
    keyboard.append([
        InlineKeyboardButton("🔄 Next Numbers", callback_data="next_numbers", style="success"),
        InlineKeyboardButton("🎲 Random Number", callback_data="random_number", style="success")
    ])
    keyboard.append([
        InlineKeyboardButton("🛠️ Change Service", callback_data="change_service", style="danger"),
        InlineKeyboardButton("🎯 Set Prefix", callback_data="prefix_filter", style="danger")
    ])
    
    otp_grp = os.getenv("OTP_GROUP_URL", "https://t.me/+ZTN2ldN9repmNWNl")
    keyboard.append([
        InlineKeyboardButton("👥 OTP Group ", url=otp_grp, style="primary")
    ])
    return InlineKeyboardMarkup(keyboard)

def format_broadcast_message(platform, country, icon="📱", flag=None, count=None):
    """Formats the broadcast message for new stock matching the screenshot."""
    if not flag:
        flag = get_country_flag(country)
    count_str = f"📊 Added: {count} numbers\n" if count else ""
    return (
        "📦 New Numbers Added!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 Country: {flag} {country.title()}\n"
        f"📱 Service: {platform.title()}\n"
        f"{count_str}"
        "⚡ Grab your numbers now!"
    )

def get_broadcast_keyboard():
    """Inline keyboard under the broadcast message"""
    keyboard = [
        [InlineKeyboardButton("⚡ Get Numbers Now ↗", callback_data="get_number_broadcast", style="primary")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ── OTP MESSAGE FORMATTING ──────────────────────────────────────────────────

def extract_country_prefix(phone_number, country_name=None):
    """Extract standard carrier prefix based on country.
    Germany: 9 or 10 digits (e.g. 491551046)
    Algeria: 8 digits (e.g. 2135609)
    Guinea: 9 digits (e.g. 22462708X)
    Mali: 8 digits (e.g. 2237026X)
    Default: 8 digits
    """
    clean = str(phone_number or "").strip().lstrip("+")
    if not clean:
        return ""

    if clean.startswith("49"):       # Germany (e.g. 491551046 or 4915510)
        return clean[:10] if len(clean) >= 10 else clean
    elif clean.startswith("213"):    # Algeria (increased 1 digit -> 8 digits, e.g. 2135609)
        return clean[:8]
    elif clean.startswith("224"):    # Guinea (increased 1 digit -> 9 digits)
        return clean[:9]
    elif clean.startswith("223"):    # Mali (increased 1 digit -> 8 digits)
        return clean[:8]
    elif clean.startswith("880"):    # Bangladesh (increased 1 digit -> 9 digits)
        return clean[:9]
    elif clean.startswith("44"):     # UK (increased 1 digit -> 9 digits)
        return clean[:9]

    return clean[:8] if len(clean) >= 8 else clean

def format_otp_group_message(number, service, otp, prefix, owner_name="N/A", sms_content=""):
    """Format OTP message for group in table format matching requested style:
    🇩🇪 GERMANY
    💬 WHATSAPP
    ┌──────────────┬──────────────────┐
    │ 📁 PREFIX    │ 491551194        │
    ├──────────────┼──────────────────┤
    │ 📲 NUMBER    │ +•••••••1636     │
    ├──────────────┼──────────────────┤
    │ 🗨 OTP       │ 403-170          │
    └──────────────┴──────────────────┘
    """
    flag, country_name = get_country_details(phone_number=number)

    clean_num = str(number).strip().lstrip("+")
    if len(clean_num) >= 4:
        masked_num = f"+•••••••{clean_num[-4:]}"
    else:
        masked_num = f"+{clean_num}"

    # Extract exact country prefix: Germany -> 491551194, Algeria -> 2135414
    prefix_str = str(prefix).strip() if prefix else ""
    if not prefix_str or prefix_str == "None" or (clean_num.startswith("49") and len(prefix_str.rstrip("X")) < 9):
        prefix_str = extract_country_prefix(clean_num, country_name=country_name)
    else:
        prefix_str = prefix_str.rstrip("X")

    # Add XXXX suffix for most countries; skip Germany (+49), Algeria (+213), UK (+44)
    _no_xxxx = ("49", "213", "44", "33", "39", "34", "49", "351")
    if not any(clean_num.startswith(p) for p in _no_xxxx):
        if not prefix_str.endswith("XXXX"):
            prefix_str = f"{prefix_str}XXXX"

    otp_clean = str(otp).strip()
    if len(otp_clean) == 6 and otp_clean.isdigit():
        formatted_otp = f"{otp_clean[:3]}-{otp_clean[3:]}"
    else:
        formatted_otp = otp_clean

    srv_raw = str(service or "WHATSAPP").strip().upper()
    if not srv_raw or srv_raw in ["SMS", "NONE"]:
        srv_name = "WHATSAPP"
    else:
        srv_name = srv_raw

    # Detect language if possible
    lang = detect_sms_language(sms_content)
    if not lang and country_name in [
        "MALI", "SENEGAL", "IVORY COAST", "BURKINA FASO", "NIGER", "TOGO", "BENIN",
        "CAMEROON", "ALGERIA", "MOROCCO", "FRANCE", "ITALY", "BELGIUM", "GUINEA BISSAU",
        "CHAD", "CENTRAL AFRICA", "GABON", "CONGO", "DR CONGO", "MAURITANIA", "DJIBOUTI"
    ]:
        lang = "FRENCH"
    elif not lang and country_name in [
        "GUINEA", "NIGERIA", "GHANA", "KENYA", "UGANDA", "ZAMBIA", "TANZANIA",
        "SOUTH AFRICA", "ZIMBABWE", "SIERRA LEONE", "LIBERIA", "ETHIOPIA", "SOMALIA"
    ]:
        lang = "ENGLISH"

    srv_line = f"{srv_name} · {lang}" if lang else srv_name

    user_line = f"\n👤 <b>User:</b> {owner_name}" if (owner_name and owner_name != "N/A") else ""

    # Clean structured table without <pre> so values are independently copyable
    # PREFIX, NUMBER, OTP use <code> so single tap copies ONLY that specific value
    # Pure white background (no <pre> gray block)
    return (
        f"<b>{flag}  {country_name}</b>\n"
        f"<b>💬  {srv_line}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂  <b>PREFIX</b>   │  <code>{prefix_str}</code>\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"📲  <b>NUMBER</b>   │  <code>{masked_num}</code>\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🗨  <b>OTP</b>      │  <code>{formatted_otp}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━"
        f"{user_line}"
    )

def format_otp_private_message(number, service=None, otp=None, prefix=""):
    """Format OTP message for private (owner DM) — inside box with full number & prefix."""
    flag = get_country_flag(phone_number=number)
    formatted_num = number if number.startswith("+") else f"+{number}"
    pref = (prefix or extract_country_prefix(number)).rstrip("X")
    return (
        f"╔══════════════════════╗\n"
        f" │ 📞 {flag} <code>{formatted_num}</code>\n"
        f" │ 🎯 Prefix:  <code>{pref}</code>\n"
        f"╚══════════════════════╝"
    )

def _get_flag_img(flag_emoji, target_h=44):
    try:
        code = ''.join([chr(ord(c) - 127397).lower() for c in flag_emoji if 127462 <= ord(c) <= 127487])
        if not code or len(code) != 2:
            return None
        cache_dir = os.path.join(os.path.dirname(__file__), 'cache_flags')
        os.makedirs(cache_dir, exist_ok=True)
        flag_path = os.path.join(cache_dir, f'{code}.png')
        if not os.path.exists(flag_path):
            url = f'https://flagcdn.com/w80/{code}.png'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as res:
                with open(flag_path, 'wb') as f:
                    f.write(res.read())
        im = Image.open(flag_path).convert('RGBA')
        aspect = im.width / im.height
        new_w = int(target_h * aspect)
        im = im.resize((new_w, target_h), Image.Resampling.LANCZOS)
        mask = Image.new('L', (new_w, target_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, new_w, target_h], radius=6, fill=255)
        out = Image.new('RGBA', (new_w, target_h), (0, 0, 0, 0))
        out.paste(im, (0, 0), mask)
        return out
    except Exception:
        return None

def _create_service_icon(service_name, size=40):
    s = str(service_name or "WHATSAPP").upper()
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if 'TELEGRAM' in s:
        d.ellipse([0, 0, size, size], fill=(42, 171, 238))
        d.polygon([(size*0.22, size*0.5), (size*0.78, size*0.22), (size*0.62, size*0.78), (size*0.45, size*0.6)], fill=(255, 255, 255))
    elif 'IMO' in s:
        d.rounded_rectangle([0, 0, size, size], radius=size//4, fill=(30, 144, 255))
        try:
            f = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', int(size*0.4))
            d.text((size*0.15, size*0.25), 'imo', font=f, fill=(255, 255, 255))
        except Exception:
            pass
    elif 'VK' in s:
        d.rounded_rectangle([0, 0, size, size], radius=size//4, fill=(74, 118, 168))
        try:
            f = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', int(size*0.45))
            d.text((size*0.2, size*0.2), 'VK', font=f, fill=(255, 255, 255))
        except Exception:
            pass
    else: # WhatsApp default
        d.rounded_rectangle([0, 0, size, size], radius=size//4, fill=(37, 211, 102))
        try:
            font = ImageFont.truetype('C:/Windows/Fonts/seguiemj.ttf', int(size * 0.65))
            d.text((int(size * 0.15), int(size * 0.05)), '📞', font=font, fill=(255, 255, 255))
        except Exception:
            pass
    return img

def _create_icon_folder(size=36):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 8, size-4, size-4], radius=4, fill=(245, 158, 11))
    d.rounded_rectangle([4, 4, size//2, 10], radius=2, fill=(217, 119, 6))
    d.ellipse([size-12, 2, size-2, 12], fill=(239, 68, 68))
    return img

def _create_icon_phone(size=36):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 2, size-8, size-2], radius=6, fill=(229, 231, 235), outline=(156, 163, 175), width=2)
    d.rectangle([10, 6, size-12, size-8], fill=(254, 202, 202))
    d.ellipse([size-14, size-16, size-4, size-6], fill=(239, 68, 68))
    return img

def _create_icon_chat(size=36):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, size-4, size-10], radius=6, fill=(244, 114, 182))
    d.polygon([(8, size-10), (14, size-10), (8, size-3)], fill=(244, 114, 182))
    try:
        f = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', size//2)
        d.text((size//2 - 4, size//4 - 2), '?', font=f, fill=(255, 255, 255))
    except Exception:
        pass
    return img

def generate_otp_card(phone_number, service=None, otp=None, prefix="", sms_content=None) -> bytes:
    """Generate high-resolution PNG image card matching the Snake Network OTP design."""
    flag_emoji, country_name = get_country_details(phone_number=phone_number)
    clean_num = str(phone_number).strip().lstrip("+")
    if len(clean_num) >= 4:
        masked_num = f"+•••••••{clean_num[-4:]}"
    else:
        masked_num = f"+{clean_num}"

    prefix_str = str(prefix).strip() if prefix else ""
    if not prefix_str or prefix_str == "None" or (clean_num.startswith("49") and len(prefix_str.rstrip("X")) < 9):
        prefix_str = extract_country_prefix(clean_num, country_name=country_name)
    else:
        prefix_str = prefix_str.rstrip("X")

    _no_xxxx = ("49", "213", "44", "33", "39", "34", "351")
    if not any(clean_num.startswith(p) for p in _no_xxxx):
        if not prefix_str.endswith("XXXX"):
            prefix_str = f"{prefix_str}XXXX"

    otp_clean = str(otp).strip()
    if len(otp_clean) == 6 and otp_clean.isdigit():
        formatted_otp = f"{otp_clean[:3]}-{otp_clean[3:]}"
    else:
        formatted_otp = otp_clean

    srv_raw = str(service or "WHATSAPP").strip().upper()
    if not srv_raw or srv_raw in ["SMS", "NONE"]:
        srv_name = "WHATSAPP"
    else:
        srv_name = srv_raw

    lang = detect_sms_language(sms_content)
    if not lang and country_name in [
        "MALI", "SENEGAL", "IVORY COAST", "BURKINA FASO", "NIGER", "TOGO", "BENIN",
        "CAMEROON", "ALGERIA", "MOROCCO", "FRANCE", "ITALY", "BELGIUM", "GUINEA BISSAU",
        "CHAD", "CENTRAL AFRICA", "GABON", "CONGO", "DR CONGO", "MAURITANIA", "DJIBOUTI"
    ]:
        lang = "FRENCH"
    elif not lang and country_name in [
        "GUINEA", "NIGERIA", "GHANA", "KENYA", "UGANDA", "ZAMBIA", "TANZANIA",
        "SOUTH AFRICA", "ZIMBABWE", "SIERRA LEONE", "LIBERIA", "ETHIOPIA", "SOMALIA"
    ]:
        lang = "ENGLISH"

    scale = 2
    w = 380 * scale
    h = 240 * scale
    radius = 16 * scale

    font_country = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 16 * scale)
    font_service = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 13 * scale)
    font_label   = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 13 * scale)
    font_val     = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 14 * scale)
    font_otp     = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 16 * scale)

    bg_color     = (255, 255, 255)
    border_color = (215, 220, 228)
    header_bg    = (246, 248, 251)
    grid_line    = (226, 230, 238)
    text_dark    = (17, 24, 39)
    otp_blue     = (2, 132, 199)

    img = Image.new('RGBA', (w, h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)

    card_box = [1 * scale, 1 * scale, w - 2 * scale, h - 2 * scale]
    draw.rounded_rectangle(card_box, radius=radius, fill=bg_color, outline=border_color, width=1 * scale)

    header_h = 76 * scale
    header_mask = Image.new('L', (w, h), 0)
    m_draw = ImageDraw.Draw(header_mask)
    m_draw.rounded_rectangle(card_box, radius=radius, fill=255)
    m_draw.rectangle([0, header_h, w, h], fill=0)

    header_layer = Image.new('RGBA', (w, h), header_bg)
    img.paste(header_layer, (0, 0), header_mask)

    draw.rounded_rectangle(card_box, radius=radius, fill=None, outline=border_color, width=1 * scale)
    draw.line([(1 * scale, header_h), (w - 2 * scale, header_h)], fill=grid_line, width=1 * scale)

    # 1. Header: Country Line
    country_str = country_name.upper()
    flag_im = _get_flag_img(flag_emoji, target_h=22 * scale)
    c_bbox = draw.textbbox((0, 0), country_str, font=font_country)
    c_tw = c_bbox[2] - c_bbox[0]
    c_th = c_bbox[3] - c_bbox[1]
    
    flag_w = flag_im.width if flag_im else 0
    gap = 8 * scale
    total_w1 = flag_w + (gap if flag_im else 0) + c_tw
    start_x1 = (w - total_w1) // 2
    y1 = 12 * scale

    if flag_im:
        img.paste(flag_im, (start_x1, y1), flag_im)
        draw.text((start_x1 + flag_w + gap, y1 + (22 * scale - c_th) // 2), country_str, fill=text_dark, font=font_country)
    else:
        draw.text(((w - c_tw) // 2, y1), country_str, fill=text_dark, font=font_country)

    # 2. Header: Service Line
    srv_str = f'{srv_name} · {lang.upper()}' if lang else srv_name
    srv_im = _create_service_icon(srv_name, size=20 * scale)
    s_bbox = draw.textbbox((0, 0), srv_str, font=font_service)
    s_tw = s_bbox[2] - s_bbox[0]
    s_th = s_bbox[3] - s_bbox[1]
    srv_w = srv_im.width
    total_w2 = srv_w + gap + s_tw
    start_x2 = (w - total_w2) // 2
    y2 = 42 * scale

    img.paste(srv_im, (start_x2, y2), srv_im)
    draw.text((start_x2 + srv_w + gap, y2 + (20 * scale - s_th) // 2), srv_str, fill=text_dark, font=font_service)

    # 3. Table Rows
    table_top = header_h
    table_bottom = h - 2 * scale
    row_h = (table_bottom - table_top) // 3
    col_split = int(w * 0.38)

    draw.line([(col_split, table_top), (col_split, table_bottom)], fill=grid_line, width=1 * scale)
    draw.line([(1 * scale, table_top + row_h), (w - 2 * scale, table_top + row_h)], fill=grid_line, width=1 * scale)
    draw.line([(1 * scale, table_top + 2 * row_h), (w - 2 * scale, table_top + 2 * row_h)], fill=grid_line, width=1 * scale)

    icon_sz = 20 * scale
    rows = [
        (_create_icon_folder(icon_sz), 'PREFIX', prefix_str, font_val, text_dark),
        (_create_icon_phone(icon_sz),  'NUMBER', masked_num,  font_val, text_dark),
        (_create_icon_chat(icon_sz),   'OTP',    formatted_otp, font_otp, otp_blue),
    ]

    for i, (icon_im, label, val, v_font, v_color) in enumerate(rows):
        y_mid = table_top + i * row_h + (row_h // 2)

        l_bbox = draw.textbbox((0, 0), label, font=font_label)
        l_tw = l_bbox[2] - l_bbox[0]
        l_th = l_bbox[3] - l_bbox[1]
        
        tot_lw = icon_sz + 8 * scale + l_tw
        lx_start = (col_split - tot_lw) // 2
        
        img.paste(icon_im, (lx_start, y_mid - icon_sz // 2), icon_im)
        draw.text((lx_start + icon_sz + 8 * scale, y_mid - l_th // 2 - 2 * scale), label, fill=text_dark, font=font_label)

        v_bbox = draw.textbbox((0, 0), val, font=v_font)
        v_tw = v_bbox[2] - v_bbox[0]
        v_th = v_bbox[3] - v_bbox[1]
        vx = col_split + (w - col_split - v_tw) // 2
        vy = y_mid - v_th // 2 - 2 * scale
        draw.text((vx, vy), val, fill=v_color, font=v_font)

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG', quality=95)
    buf.seek(0)
    return buf.getvalue()

def get_otp_keyboard(otp=None, prefix=None):
    """Inline keyboard for group:
    [ NUMBER ]  |  [ METHOD ]
    """
    bot_url = os.getenv("BOT_URL", "https://t.me/bossalltime_bot")
    method_url = os.getenv("METHOD_URL", bot_url)

    keyboard = [
        [
            InlineKeyboardButton(
                "NUMBER",
                url=bot_url,
                style="primary"
            ),
            InlineKeyboardButton(
                "METHOD",
                url=method_url,
                style="primary"
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_rich_otp_group_message(session, bot_token, chat_id, number, service=None, otp=None, prefix="", owner_name="N/A", sms_content=None) -> bool:
    """Send native Telegram Article Table via official Bot API 10.3 sendRichMessage."""
    try:
        flag_emoji, country_name = get_country_details(phone_number=number)
        clean_num = str(number).strip().lstrip("+")
        if len(clean_num) >= 4:
            masked_num = f"+•••••••{clean_num[-4:]}"
        else:
            masked_num = f"+{clean_num}"

        prefix_str = str(prefix).strip() if prefix else ""
        if not prefix_str or prefix_str == "None" or (clean_num.startswith("49") and len(prefix_str.rstrip("X")) < 9):
            prefix_str = extract_country_prefix(clean_num, country_name=country_name)
        else:
            prefix_str = prefix_str.rstrip("X")

        _no_xxxx = ("49", "213", "44", "33", "39", "34", "351")
        if not any(clean_num.startswith(p) for p in _no_xxxx):
            if not prefix_str.endswith("XXXX"):
                prefix_str = f"{prefix_str}XXXX"

        otp_clean = str(otp).strip()
        if len(otp_clean) == 6 and otp_clean.isdigit():
            formatted_otp = f"{otp_clean[:3]}-{otp_clean[3:]}"
        else:
            formatted_otp = otp_clean

        srv_raw = str(service or "WHATSAPP").strip().upper()
        if not srv_raw or srv_raw in ["SMS", "NONE"]:
            srv_name = "WHATSAPP"
        else:
            srv_name = srv_raw

        lang = detect_sms_language(sms_content)
        if not lang and country_name in [
            "MALI", "SENEGAL", "IVORY COAST", "BURKINA FASO", "NIGER", "TOGO", "BENIN",
            "CAMEROON", "ALGERIA", "MOROCCO", "FRANCE", "ITALY", "BELGIUM", "GUINEA BISSAU",
            "CHAD", "CENTRAL AFRICA", "GABON", "CONGO", "DR CONGO", "MAURITANIA", "DJIBOUTI"
        ]:
            lang = "FRENCH"
        elif not lang and country_name in [
            "GUINEA", "NIGERIA", "GHANA", "KENYA", "UGANDA", "ZAMBIA", "TANZANIA",
            "SOUTH AFRICA", "ZIMBABWE", "SIERRA LEONE", "LIBERIA", "ETHIOPIA", "SOMALIA"
        ]:
            lang = "ENGLISH"

        srv_line = f"{srv_name} · {lang}" if lang else srv_name
        header_text = f"{flag_emoji}  {country_name}\n💬  {srv_line}"

        is_group_chat = str(chat_id).startswith("-")

        bot_url = os.getenv("BOT_URL", "https://t.me/bossalltime_bot")
        method_url = os.getenv("METHOD_URL", bot_url)

        table_cells = [
            [
                {
                    "text": header_text,
                    "is_header": True,
                    "colspan": 2,
                    "align": "center"
                }
            ],
            [
                {"text": "📁  PREFIX", "align": "left"},
                {
                    "text": {"type": "code", "text": prefix_str},
                    "align": "center"
                }
            ],
            [
                {"text": "📲  NUMBER", "align": "left"},
                {"text": masked_num, "align": "center"}
            ],
            [
                {"text": "💬  OTP", "align": "left"},
                {
                    "text": {"type": "code", "text": formatted_otp},
                    "align": "center"
                }
            ],
            [
                {"text": "👤  USER", "align": "left"},
                {"text": str(owner_name or "N/A"), "align": "center"}
            ]
        ]

        blocks = [
            {
                "type": "table",
                "is_bordered": True,
                "is_compact": True,
                "cells": table_cells
            }
        ]

        # NUMBER and METHOD buttons inside rich message without dividing or bottom border (evenly spaced in 2 columns)
        if is_group_chat:
            blocks.append({
                "type": "table",
                "is_bordered": False,
                "is_compact": True,
                "cells": [
                    [
                        {
                            "text": {
                                "type": "button",
                                "button": {
                                    "text": "NUMBER",
                                    "url": bot_url,
                                    "style": "primary"
                                }
                            },
                            "align": "center"
                        },
                        {
                            "text": {
                                "type": "button",
                                "button": {
                                    "text": "METHOD",
                                    "url": method_url,
                                    "style": "primary"
                                }
                            },
                            "align": "center"
                        }
                    ]
                ]
            })

        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "blocks": blocks
            }
        }

        async with session.post(f"https://api.telegram.org/bot{bot_token}/sendRichMessage", json=payload, timeout=10) as resp:
            data = await resp.json()
            return bool(data.get("ok"))
    except Exception as e:
        logger.error(f"Error in send_rich_otp_group_message: {e}")
        return False

async def send_rich_otp_private_message(session, bot_token, chat_id, number, service=None, otp=None, prefix="", sms_content=None) -> bool:
    """Send native Telegram Article Table for private user chat with FULL number and copy button."""
    try:
        flag_emoji, country_name = get_country_details(phone_number=number)
        clean_num = str(number).strip().lstrip("+")
        full_num = f"+{clean_num}"

        prefix_str = str(prefix).strip() if prefix else ""
        if not prefix_str or prefix_str == "None" or (clean_num.startswith("49") and len(prefix_str.rstrip("X")) < 9):
            prefix_str = extract_country_prefix(clean_num, country_name=country_name)
        else:
            prefix_str = prefix_str.rstrip("X")

        _no_xxxx = ("49", "213", "44", "33", "39", "34", "351")
        if not any(clean_num.startswith(p) for p in _no_xxxx):
            if not prefix_str.endswith("XXXX"):
                prefix_str = f"{prefix_str}XXXX"

        otp_clean = str(otp).strip()
        if len(otp_clean) == 6 and otp_clean.isdigit():
            formatted_otp = f"{otp_clean[:3]}-{otp_clean[3:]}"
        else:
            formatted_otp = otp_clean

        srv_raw = str(service or "WHATSAPP").strip().upper()
        if not srv_raw or srv_raw in ["SMS", "NONE"]:
            srv_name = "WHATSAPP"
        else:
            srv_name = srv_raw

        lang = detect_sms_language(sms_content)
        if not lang and country_name in [
            "MALI", "SENEGAL", "IVORY COAST", "BURKINA FASO", "NIGER", "TOGO", "BENIN",
            "CAMEROON", "ALGERIA", "MOROCCO", "FRANCE", "ITALY", "BELGIUM", "GUINEA BISSAU",
            "CHAD", "CENTRAL AFRICA", "GABON", "CONGO", "DR CONGO", "MAURITANIA", "DJIBOUTI"
        ]:
            lang = "FRENCH"
        elif not lang and country_name in [
            "GUINEA", "NIGERIA", "GHANA", "KENYA", "UGANDA", "ZAMBIA", "TANZANIA",
            "SOUTH AFRICA", "ZIMBABWE", "SIERRA LEONE", "LIBERIA", "ETHIOPIA", "SOMALIA"
        ]:
            lang = "ENGLISH"

        srv_line = f"{srv_name} · {lang}" if lang else srv_name
        header_text = f"{flag_emoji}  {country_name}\n💬  {srv_line}"

        table_cells = [
            [
                {
                    "text": header_text,
                    "is_header": True,
                    "colspan": 2,
                    "align": "center"
                }
            ],
            [
                {"text": "📁  PREFIX", "align": "left"},
                {
                    "text": {"type": "code", "text": prefix_str},
                    "align": "center"
                }
            ],
            [
                {"text": "📲  NUMBER", "align": "left"},
                {
                    "text": {"type": "code", "text": full_num},
                    "align": "center"
                }
            ],
            [
                {"text": "💬  OTP", "align": "left"},
                {
                    "text": {"type": "code", "text": formatted_otp},
                    "align": "center"
                }
            ]
        ]

        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "blocks": [
                    {
                        "type": "table",
                        "is_bordered": True,
                        "is_compact": True,
                        "cells": table_cells
                    }
                ]
            }
        }

        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        try:
            async with session.post(f"https://api.telegram.org/bot{bot_token}/sendRichMessage", json=payload, timeout=10) as resp:
                data = await resp.json()
                return bool(data.get("ok"))
        finally:
            if close_session:
                await session.close()
    except Exception as e:
        logger.error(f"Error in send_rich_otp_private_message: {e}")
        return False

def get_private_otp_keyboard(otp):
    """Inline keyboard for private message: single red copy button."""
    keyboard = [
        [
            InlineKeyboardButton(
                f"🔑 {otp}",
                copy_text=CopyTextButton(text=str(otp)),
                style="danger"
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ── ADMIN KEYBOARDS ─────────────────────────────────────────────────────────

SERVICES = [
    ("💬 IMO",       "IMO"),
    ("🫆 TELEGRAM",  "TELEGRAM"),
    ("🧊 VK",        "VK"),
    ("🟢 WHATSAPP",  "WHATSAPP"),
]

COUNTRIES = [
    ("🇦🇫 AFGHANISTAN",    "AFGHANISTAN"),
    ("🇦🇱 ALBANIA",        "ALBANIA"),
    ("🇩🇿 ALGERIA",        "ALGERIA"),
    ("🇧🇾 BELARUS",        "BELARUS"),
    ("🇨🇫 CENTRAL AFRICA", "CENTRAL AFRICA"),
    ("🇩🇪 GERMANY",        "GERMANY"),
    ("🇬🇹 GUATEMALA",      "GUATEMALA"),
    ("🇭🇹 HAITI",          "HAITI"),
    ("🇮🇹 ITALY",          "ITALY"),
    ("🇲🇾 MALAYSIA",       "MALAYSIA"),
    ("🇲🇦 MOROCCO",        "MOROCCO"),
    ("🇲🇲 MYANMAR",        "MYANMAR"),
    ("🇳🇬 NIGERIA",        "NIGERIA"),
    ("🇸🇸 SOUTH SUDAN",    "SOUTH SUDAN"),
    ("🇸🇾 SYRIA",          "SYRIA"),
    ("🇹🇿 TANZANIA",       "TANZANIA"),
    ("🇹🇳 TUNISIA",        "TUNISIA"),
    ("🇺🇦 UKRAINE",        "UKRAINE"),
    ("🇺🇿 UZBEKISTAN",     "UZBEKISTAN"),
    ("🇾🇪 YEMEN",          "YEMEN"),
    ("🇱🇰 SRI LANKA",      "SRI LANKA"),
    ("🇮🇳 INDIA",          "INDIA"),
    ("🇧🇩 BANGLADESH",     "BANGLADESH"),
    ("🇵🇰 PAKISTAN",       "PAKISTAN"),
    ("🇮🇩 INDONESIA",      "INDONESIA"),
]

def get_admin_service_keyboard(services=None):
    """Admin inline keyboard for selecting which service to add numbers for.
    `services` is a list of {label, name} dicts (from DB). Falls back to hardcoded SERVICES.
    """
    keyboard = []
    if services:
        for svc in services:
            label = svc.get("label", svc.get("name", "?"))
            val = svc.get("name", "")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_svc_{val}", style="primary")])
    else:
        for label, val in SERVICES:
            keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_svc_{val}", style="primary")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_back", style="danger"),
                     InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_remove_service_keyboard(services=None):
    """Admin inline keyboard to select which service to remove."""
    keyboard = []
    if services:
        for svc in services:
            label = svc.get("label", svc.get("name", "?"))
            val = svc.get("name", "")
            keyboard.append([InlineKeyboardButton(
                f"🗑 {label}", callback_data=f"admin_rm_svc_{val}", style="danger"
            )])
    else:
        keyboard.append([InlineKeyboardButton("⚠️ No services to remove", callback_data="admin_cancel", style="danger")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="danger")])
    return InlineKeyboardMarkup(keyboard)


def get_admin_menu_keyboard():
    """Admin main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("➕ Upload Numbers", callback_data="admin_upload", style="primary")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton("📊 Reports", callback_data="admin_report", style="primary")],
        [InlineKeyboardButton("❌ Close", callback_data="admin_cancel", style="danger")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_report_keyboard():
    """Admin reports submenu keyboard."""
    keyboard = [
        [InlineKeyboardButton("👥 OTP Report (Today)", callback_data="report_summary", style="primary")],
        [InlineKeyboardButton("📥 Numbers with OTP - Today", callback_data="report_with_today", style="primary")],
        [InlineKeyboardButton("📭 Numbers without OTP - Today", callback_data="report_without_today", style="primary")],
        [InlineKeyboardButton("📥 Numbers with OTP - All time", callback_data="report_with_all", style="primary")],
        [InlineKeyboardButton("📭 Numbers without OTP - All time", callback_data="report_without_all", style="primary")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_report_back", style="danger")],
        [InlineKeyboardButton("❌ Close", callback_data="admin_cancel", style="danger")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_remove_stock_keyboard(stock_summary):
    """Admin inline keyboard listing all service+country combos with counts.
    stock_summary items: (service, country, total, available, assigned)
    Each button triggers admin_rm_{SERVICE}_{COUNTRY} callback."""
    keyboard = []
    for item in stock_summary:
        service, country, total = item[0], item[1], item[2]
        avail = item[3] if len(item) > 3 else 0
        assigned = item[4] if len(item) > 4 else 0
        flag = get_country_flag(country)
        label = f"🗑 {service} – {flag} {country} ({total} | ✅{avail} 📌{assigned})"
        callback = f"admin_rm_{service}_{country}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback, style="danger")])
    if not stock_summary:
        keyboard.append([InlineKeyboardButton("⚠️ No numbers in database", callback_data="admin_cancel", style="danger")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_remove_confirm_keyboard(service, country, count):
    """Confirmation keyboard before removing numbers."""
    keyboard = [
        [InlineKeyboardButton(f"✅ Yes, remove all {count} numbers", callback_data=f"admin_rm_confirm_{service}_{country}", style="danger")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="primary")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_reset_confirm_keyboard():
    """Confirmation keyboard before resetting entire database."""
    keyboard = [
        [InlineKeyboardButton("⚠️ YES, RESET ENTIRE DATABASE", callback_data="admin_reset_confirm", style="danger")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="primary")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_country_keyboard():
    """Admin inline keyboard for selecting the country for the chosen service."""
    keyboard = []
    for i in range(0, len(COUNTRIES), 2):
        row = [InlineKeyboardButton(COUNTRIES[i][0], callback_data=f"admin_cty_{COUNTRIES[i][1]}", style="primary")]
        if i + 1 < len(COUNTRIES):
            row.append(InlineKeyboardButton(COUNTRIES[i+1][0], callback_data=f"admin_cty_{COUNTRIES[i+1][1]}", style="primary"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_back", style="danger"),
                     InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_panels_keyboard(panels: list):
    """Inline keyboard for managing panels: list panels with status toggles, add panel, close."""
    keyboard = []
    # Button to Add New Panel
    keyboard.append([
        InlineKeyboardButton("➕ Add New Panel", callback_data="panel_add_start", style="success")
    ])
    
    # List each panel with toggle and delete
    for p in panels:
        p_id = p["id"]
        name = p["name"]
        status_icon = "🟢" if p["is_active"] else "🔴"
        action_text = "Stop" if p["is_active"] else "Start"
        action_style = "danger" if p["is_active"] else "success"
        
        keyboard.append([
            InlineKeyboardButton(f"{status_icon} {name} ({action_text})", callback_data=f"panel_toggle_{p_id}", style=action_style),
            InlineKeyboardButton("🗑 Delete", callback_data=f"panel_delete_{p_id}", style="danger")
        ])
        
    keyboard.append([
        InlineKeyboardButton("❌ Close", callback_data="admin_cancel", style="danger")
    ])
    return InlineKeyboardMarkup(keyboard)

def get_admin_checkers_keyboard(checkers: list):
    """Inline keyboard for managing checkers: list checkers with status toggles, add checker, close."""
    keyboard = []
    # Button to Add Checker
    keyboard.append([
        InlineKeyboardButton("➕ Add Checker", callback_data="checker_add_start", style="success")
    ])

    for c in checkers:
        c_id = c["id"]
        name = c["name"]
        username = c["username"]
        status_icon = "🟢" if c["is_active"] else "🔴"
        action_text = "Disable" if c["is_active"] else "Enable"
        action_style = "danger" if c["is_active"] else "success"

        keyboard.append([
            InlineKeyboardButton(f"{status_icon} {name} ({username})", callback_data=f"checker_toggle_{c_id}", style=action_style),
            InlineKeyboardButton("🗑 Delete", callback_data=f"checker_delete_{c_id}", style="danger")
        ])

    keyboard.append([
        InlineKeyboardButton("❌ Close", callback_data="admin_cancel", style="danger")
    ])
    return InlineKeyboardMarkup(keyboard)

