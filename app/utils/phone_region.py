from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PhoneRegion:
    code: str
    country: str
    flag: str


PHONE_REGIONS = {
    "1": PhoneRegion("1", "US/CA", "🇺🇸"),
    "7": PhoneRegion("7", "RU/KZ", "🇷🇺"),
    "20": PhoneRegion("20", "EG", "🇪🇬"),
    "27": PhoneRegion("27", "ZA", "🇿🇦"),
    "30": PhoneRegion("30", "GR", "🇬🇷"),
    "31": PhoneRegion("31", "NL", "🇳🇱"),
    "32": PhoneRegion("32", "BE", "🇧🇪"),
    "33": PhoneRegion("33", "FR", "🇫🇷"),
    "34": PhoneRegion("34", "ES", "🇪🇸"),
    "36": PhoneRegion("36", "HU", "🇭🇺"),
    "39": PhoneRegion("39", "IT", "🇮🇹"),
    "40": PhoneRegion("40", "RO", "🇷🇴"),
    "41": PhoneRegion("41", "CH", "🇨🇭"),
    "44": PhoneRegion("44", "UK", "🇬🇧"),
    "45": PhoneRegion("45", "DK", "🇩🇰"),
    "46": PhoneRegion("46", "SE", "🇸🇪"),
    "47": PhoneRegion("47", "NO", "🇳🇴"),
    "48": PhoneRegion("48", "PL", "🇵🇱"),
    "49": PhoneRegion("49", "DE", "🇩🇪"),
    "51": PhoneRegion("51", "PE", "🇵🇪"),
    "52": PhoneRegion("52", "MX", "🇲🇽"),
    "53": PhoneRegion("53", "CU", "🇨🇺"),
    "54": PhoneRegion("54", "AR", "🇦🇷"),
    "55": PhoneRegion("55", "BR", "🇧🇷"),
    "56": PhoneRegion("56", "CL", "🇨🇱"),
    "57": PhoneRegion("57", "CO", "🇨🇴"),
    "58": PhoneRegion("58", "VE", "🇻🇪"),
    "60": PhoneRegion("60", "MY", "🇲🇾"),
    "61": PhoneRegion("61", "AU", "🇦🇺"),
    "62": PhoneRegion("62", "ID", "🇮🇩"),
    "63": PhoneRegion("63", "PH", "🇵🇭"),
    "64": PhoneRegion("64", "NZ", "🇳🇿"),
    "65": PhoneRegion("65", "SG", "🇸🇬"),
    "66": PhoneRegion("66", "TH", "🇹🇭"),
    "81": PhoneRegion("81", "JP", "🇯🇵"),
    "82": PhoneRegion("82", "KR", "🇰🇷"),
    "84": PhoneRegion("84", "VN", "🇻🇳"),
    "86": PhoneRegion("86", "CN", "🇨🇳"),
    "90": PhoneRegion("90", "TR", "🇹🇷"),
    "91": PhoneRegion("91", "IN", "🇮🇳"),
    "92": PhoneRegion("92", "PK", "🇵🇰"),
    "93": PhoneRegion("93", "AF", "🇦🇫"),
    "94": PhoneRegion("94", "LK", "🇱🇰"),
    "95": PhoneRegion("95", "MM", "🇲🇲"),
    "98": PhoneRegion("98", "IR", "🇮🇷"),
    "212": PhoneRegion("212", "MA", "🇲🇦"),
    "213": PhoneRegion("213", "DZ", "🇩🇿"),
    "216": PhoneRegion("216", "TN", "🇹🇳"),
    "218": PhoneRegion("218", "LY", "🇱🇾"),
    "220": PhoneRegion("220", "GM", "🇬🇲"),
    "221": PhoneRegion("221", "SN", "🇸🇳"),
    "225": PhoneRegion("225", "CI", "🇨🇮"),
    "233": PhoneRegion("233", "GH", "🇬🇭"),
    "234": PhoneRegion("234", "NG", "🇳🇬"),
    "237": PhoneRegion("237", "CM", "🇨🇲"),
    "249": PhoneRegion("249", "SD", "🇸🇩"),
    "251": PhoneRegion("251", "ET", "🇪🇹"),
    "254": PhoneRegion("254", "KE", "🇰🇪"),
    "255": PhoneRegion("255", "TZ", "🇹🇿"),
    "256": PhoneRegion("256", "UG", "🇺🇬"),
    "260": PhoneRegion("260", "ZM", "🇿🇲"),
    "263": PhoneRegion("263", "ZW", "🇿🇼"),
    "351": PhoneRegion("351", "PT", "🇵🇹"),
    "352": PhoneRegion("352", "LU", "🇱🇺"),
    "353": PhoneRegion("353", "IE", "🇮🇪"),
    "354": PhoneRegion("354", "IS", "🇮🇸"),
    "355": PhoneRegion("355", "AL", "🇦🇱"),
    "356": PhoneRegion("356", "MT", "🇲🇹"),
    "357": PhoneRegion("357", "CY", "🇨🇾"),
    "358": PhoneRegion("358", "FI", "🇫🇮"),
    "359": PhoneRegion("359", "BG", "🇧🇬"),
    "370": PhoneRegion("370", "LT", "🇱🇹"),
    "371": PhoneRegion("371", "LV", "🇱🇻"),
    "372": PhoneRegion("372", "EE", "🇪🇪"),
    "373": PhoneRegion("373", "MD", "🇲🇩"),
    "374": PhoneRegion("374", "AM", "🇦🇲"),
    "375": PhoneRegion("375", "BY", "🇧🇾"),
    "380": PhoneRegion("380", "UA", "🇺🇦"),
    "381": PhoneRegion("381", "RS", "🇷🇸"),
    "382": PhoneRegion("382", "ME", "🇲🇪"),
    "383": PhoneRegion("383", "XK", "🇽🇰"),
    "385": PhoneRegion("385", "HR", "🇭🇷"),
    "386": PhoneRegion("386", "SI", "🇸🇮"),
    "387": PhoneRegion("387", "BA", "🇧🇦"),
    "389": PhoneRegion("389", "MK", "🇲🇰"),
    "420": PhoneRegion("420", "CZ", "🇨🇿"),
    "421": PhoneRegion("421", "SK", "🇸🇰"),
    "852": PhoneRegion("852", "HK", "🇭🇰"),
    "853": PhoneRegion("853", "MO", "🇲🇴"),
    "855": PhoneRegion("855", "KH", "🇰🇭"),
    "856": PhoneRegion("856", "LA", "🇱🇦"),
    "880": PhoneRegion("880", "BD", "🇧🇩"),
    "886": PhoneRegion("886", "TW", "🇹🇼"),
    "888": PhoneRegion("888", "Fragment", "💎"),
    "961": PhoneRegion("961", "LB", "🇱🇧"),
    "962": PhoneRegion("962", "JO", "🇯🇴"),
    "963": PhoneRegion("963", "SY", "🇸🇾"),
    "964": PhoneRegion("964", "IQ", "🇮🇶"),
    "965": PhoneRegion("965", "KW", "🇰🇼"),
    "966": PhoneRegion("966", "SA", "🇸🇦"),
    "967": PhoneRegion("967", "YE", "🇾🇪"),
    "968": PhoneRegion("968", "OM", "🇴🇲"),
    "970": PhoneRegion("970", "PS", "🇵🇸"),
    "971": PhoneRegion("971", "AE", "🇦🇪"),
    "972": PhoneRegion("972", "IL", "🇮🇱"),
    "973": PhoneRegion("973", "BH", "🇧🇭"),
    "974": PhoneRegion("974", "QA", "🇶🇦"),
    "976": PhoneRegion("976", "MN", "🇲🇳"),
    "977": PhoneRegion("977", "NP", "🇳🇵"),
    "992": PhoneRegion("992", "TJ", "🇹🇯"),
    "993": PhoneRegion("993", "TM", "🇹🇲"),
    "994": PhoneRegion("994", "AZ", "🇦🇿"),
    "995": PhoneRegion("995", "GE", "🇬🇪"),
    "996": PhoneRegion("996", "KG", "🇰🇬"),
    "998": PhoneRegion("998", "UZ", "🇺🇿"),
}


MIN_PHONE_DIGITS = 7


def extract_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D+", "", value)
    return digits


def detect_phone_region(phone: str | None) -> PhoneRegion | None:
    digits = extract_phone(phone)
    if len(digits) < MIN_PHONE_DIGITS:
        return None
    for length in range(4, 0, -1):
        region = PHONE_REGIONS.get(digits[:length])
        if region:
            return region
    return None


def country_flag(country: str) -> str:
    if country == "unknown":
        return "🌐"
    if country == "Fragment":
        return "💎"
    if country == "US/CA":
        return "🇺🇸/🇨🇦"
    code = country.split("/", 1)[0]
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code.upper())


def display_region(country: str) -> str:
    return f"{country_flag(country)} {country}"


def format_phone_with_region(phone: str | None, fallback: str | None = None) -> str:
    digits = extract_phone(phone) or extract_phone(fallback)
    if not digits:
        return "none"
    region = detect_phone_region(digits)
    value = f"+{digits}"
    if not region:
        return value
    return f"{country_flag(region.country)} {value} ({region.country})"


def phone_group_name(phone: str | None, fallback: str | None = None) -> str:
    digits = extract_phone(phone) or extract_phone(fallback)
    region = detect_phone_region(digits)
    if not region:
        return "phone_unknown"
    return f"phone_{region.country.lower().replace('/', '_')}_{region.code}"
