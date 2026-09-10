"""The same seventeen answers, in Hindi.

One Hindi renderer per tool, mirroring `aether/hotel/tools.py` sentence for sentence. The tools, the
router, `ToolRunner` and the database are untouched -- a tool still returns `(records, summary)` and
only the final string differs. That is the whole point of having kept rendering separate from
lookup: a second language is a second renderer, not a second pipeline.

**The claim this protects.** AETHER's central promise is that hotel facts never pass through a
language model. Handing a tool result to Gemini and asking for Hindi would have been an afternoon's
work and would have broken exactly that: a model free to phrase a price is a model free to say the
wrong one, in a language fewer people in the room can check. So Hindi is templates, like English,
and `llm_ms` stays 0.

**Devanagari, decided by listening.** Both scripts were sent to `arcana`/`anaya` over `/ws3`
(`scripts/verify_rime_hindi.py`); Devanagari was the one that sounded right, and came back 9.47 s
against 12.21 s for identical content -- the shape of an engine parsing natively rather than
spelling out.

**What deliberately stays in English.** Dish names, room-type names and service names are the
hotel's own proper nouns, printed on its own menu and door signs; a caller asking for the "Chicken
Kebab" wants to hear "Chicken Kebab". Amenity words are translated where a translation is what
people actually say, and left alone where the loanword is (`Wi-Fi`, `minibar`). This is Hinglish on
purpose, because that is how hotels in India are spoken about.

**Not certified by its author.** The Hindi here is written to be reviewed by someone who speaks it
before it is spoken to anyone. Where it is wrong it is wrong in a template, which is one file and
one line, not in a model's output.
"""

from __future__ import annotations

from collections.abc import Callable

from .speech_hi import (
    say_date,
    say_list,
    say_number,
    say_price,
    say_room_number,
    say_time,
)

# Spoken when a tool ran but could not answer. The Hindi counterpart of `tools.NOT_FOUND`, and it
# means the same thing: a true "I do not have that" is a useful answer and may be spoken.
NOT_FOUND = "माफ़ कीजिए, मुझे वह नहीं मिला। क्या आप दोबारा बता सकते हैं?"

# The vocabulary the database actually contains, translated once. Read from the live values
# (`categories()`, the amenity and allergen columns, the service rows), so a word that appears in a
# sentence is a word that exists in the hotel.
_CATEGORIES = {
    "starters": "स्टार्टर",
    "mains": "मेन कोर्स",
    "vegetarian mains": "शाकाहारी मेन कोर्स",
    "desserts": "मिठाई",
    "drinks": "पेय",
}

_DIETS = {
    "vegetarian": "शाकाहारी",
    "vegan": "वीगन",
    "non-vegetarian": "मांसाहारी",
}

_ALLERGENS = {
    "nuts": "मेवा",
    "dairy": "डेयरी",
    "gluten": "ग्लूटेन",
    "fish": "मछली",
    "egg": "अंडा",
    "eggs": "अंडे",
    "shellfish": "शेलफ़िश",
}

# Translated where a translation is what people say; left as the loanword where it is not. "Wi-Fi"
# and "minibar" are said in English in every Indian hotel, and rendering them in Devanagari would be
# a translation nobody uses.
_AMENITIES = {
    "King bed": "किंग बेड",
    "Two twin beds": "दो सिंगल बेड",
    "twin sofa beds": "दो सोफ़ा बेड",
    "living room": "लिविंग रूम",
    "work desk": "वर्क डेस्क",
    "air conditioning": "एयर कंडीशनिंग",
    "city view": "शहर का नज़ारा",
    "breakfast": "नाश्ता",
    "TV": "टीवी",
    "smart TV": "स्मार्ट टीवी",
    "two TVs": "दो टीवी",
    "Wi-Fi": "Wi-Fi",
    "minibar": "minibar",
}

_SERVICES = {
    "Front Desk": "फ़्रंट डेस्क",
    "Housekeeping": "हाउसकीपिंग",
    "Luggage Assistance": "सामान की सहायता",
    "Maintenance": "मेंटेनेंस",
    "Room Service": "रूम सर्विस",
    "Wake-up Call": "वेक-अप कॉल",
}

# How many names to read before summarising. Same reason as the English side: nobody wants twelve
# dish names read down a telephone.
_SPOKEN_LIST_MAX = 6


def _category(name: str) -> str:
    return _CATEGORIES.get(str(name).lower(), str(name))


def _diet(name: str) -> str:
    return _DIETS.get(str(name).lower(), str(name))


def _allergen(name: str) -> str:
    return _ALLERGENS.get(str(name).lower(), str(name))


def _amenity(name: str) -> str:
    return _AMENITIES.get(str(name), str(name))


def _service(name: str) -> str:
    return _SERVICES.get(str(name), str(name))


def _say_names(names: list[str]) -> str:
    if len(names) <= _SPOKEN_LIST_MAX:
        return say_list(names)
    rest = len(names) - _SPOKEN_LIST_MAX
    # `say_list` already ends "... और X", so a tail of "और N और" stutters. अन्य is the natural word
    # for "other" here and does not repeat the joiner.
    more = "एक अन्य" if rest == 1 else f"{say_number(rest)} अन्य"
    return f"{say_list(names[:_SPOKEN_LIST_MAX])}, और {more}"


# --- the seventeen ---------------------------------------------------------------------------

def _speak_menu_overview(result) -> str:
    categories = [_category(c) for c in (result.summary.get("categories") or [])]
    diets = [_diet(d) for d in (result.summary.get("diets") or [])]
    if not categories:
        return "माफ़ कीजिए, आज हमारे पास कुछ भी उपलब्ध नहीं है।"
    lead = f"हमारे पास {say_list(categories)} हैं"
    return f"{lead}, और {say_list(diets)} विकल्प भी हैं।" if diets else f"{lead}।"


def _speak_list_category(result) -> str:
    rows = result.records
    category = _category(result.summary.get("category", "menu"))
    sold_out = result.summary.get("sold_out") or []
    if not rows:
        return f"माफ़ कीजिए, इस समय {category} में कुछ भी उपलब्ध नहीं है।"
    names = _say_names([r["name"] for r in rows])
    cheapest = min(rows, key=lambda r: r["price"])
    # Hindi marks number on the verb: one dish takes है, several take हैं. "Butter Chicken हैं" is
    # the mistake a template with a fixed verb makes, and `mains` currently holds exactly one dish.
    lead = f"{category} में हमारे पास {names} {'है' if len(rows) == 1 else 'हैं'}।"
    price = (f" इसकी कीमत {say_price(rows[0]['price'])} है।" if len(rows) == 1
             else f" कीमत {say_price(cheapest['price'])} से शुरू है।")
    if sold_out:
        verb = "है" if len(sold_out) == 1 else "हैं"
        tail = f" {say_list(sold_out)} आज उपलब्ध नहीं {verb}।"
    else:
        tail = ""
    return lead + price + tail


def _speak_price_of(result) -> str:
    dish = result.records[0]
    price = say_price(dish["price"])
    if not dish["available"]:
        return f"{dish['name']} की कीमत {price} है, लेकिन आज यह उपलब्ध नहीं है।"
    return f"{dish['name']} की कीमत {price} है।"


def _speak_find_by_diet(result) -> str:
    rows = result.records
    diet_raw = result.summary.get("diet", "")
    category_raw = result.summary.get("category")
    # Same stutter the English renderer avoids: the database makes "vegetarian mains" a category of
    # its own, so naming the diet again would repeat शाकाहारी twice.
    if category_raw and diet_raw.split("-")[-1] in category_raw:
        scope = _category(category_raw)
    elif category_raw:
        scope = f"{_diet(diet_raw)} {_category(category_raw)}"
    else:
        scope = _diet(diet_raw)
    if not rows:
        return f"माफ़ कीजिए, आज {scope} विकल्प उपलब्ध नहीं हैं।"
    return f"जी हाँ। {scope} में हमारे पास {_say_names([r['name'] for r in rows])} हैं।"


def _speak_check_availability(result) -> str:
    dish = result.records[0]
    if dish["available"]:
        return f"जी हाँ, {dish['name']} आज उपलब्ध है, कीमत {say_price(dish['price'])}।"
    return f"माफ़ कीजिए, {dish['name']} आज उपलब्ध नहीं है।"


def _speak_check_allergens(result) -> str:
    dish = result.records[0]
    allergens = [_allergen(a) for a in dish["allergens"]]
    if not allergens:
        return f"{dish['name']} में कोई सूचीबद्ध एलर्जन नहीं है।"
    verb = "है" if len(allergens) == 1 else "हैं"
    return f"{dish['name']} में {say_list(allergens)} {verb}।"


def _speak_safe_for(result) -> str:
    """Suggest, then warn -- the English shape, because the shape is the safety property.

    One suggestion per course and the size of what to avoid. The picks are menu order, not a
    choice, so the same question gives the same answer every time.
    """
    allergen = _allergen(result.summary.get("allergen", ""))
    rows = result.records
    if not rows:
        return f"माफ़ कीजिए, आज हमारे पास जो कुछ है उसमें {allergen} है।"
    picks, seen = [], set()
    for row in rows:
        if row["category"] not in seen:
            seen.add(row["category"])
            picks.append(row["name"])
        if len(picks) == 3:
            break
    lead = f"अगर आप {allergen} से बच रहे हैं, तो मैं {say_list(picks)} सुझाऊँगी।"
    avoid = result.summary.get("avoid") or []
    if not avoid:
        return f"{lead} आज मेन्यू में और कहीं {allergen} नहीं है।"
    # Hindi marks number on the noun and the verb, so "एक और व्यंजनों" would be as wrong as
    # "one other dishes". The English renderer already carries this distinction; so must this one.
    if len(avoid) == 1:
        count_phrase = "एक और व्यंजन में"
    else:
        count_phrase = f"{say_number(len(avoid))} और व्यंजनों में"
    return (f"{lead} मेन्यू में {count_phrase} {allergen} है, "
            f"इसलिए ऑर्डर करने से पहले मुझसे ज़रूर पूछ लीजिए।")


def _speak_describe_item(result) -> str:
    dish = result.records[0]
    description = (result.summary.get("description") or "").strip()
    if not description:
        return f"माफ़ कीजिए, {dish['name']} का विवरण मेरे पास नहीं है।"
    # The description is the hotel's own English prose, read back as written -- inventing a Hindi
    # paraphrase would be exactly the fabrication this whole layer exists to prevent.
    tail = "" if dish["available"] else " हालाँकि यह आज उपलब्ध नहीं है।"
    return f"{description}{tail}"


def _speak_room_status(result) -> str:
    if not result.summary.get("exists", True):
        return (f"हमारे यहाँ कमरा {say_room_number(result.summary['number'])} नहीं है। "
                f"हमारे कमरे {say_room_number(result.summary['lowest'])} से "
                f"{say_room_number(result.summary['highest'])} तक हैं।")

    room = result.records[0]
    number = say_room_number(room["number"])
    spoken = {
        "available": f"कमरा {number} खाली है। यह {room['room_type']} है, "
                     f"{say_price(room['rate'])} प्रति रात।",
        "occupied": f"कमरा {number} इस समय भरा हुआ है।",
        "reserved": f"कमरा {number} पहले से बुक है।",
        "housekeeping": f"कमरा {number} अभी हाउसकीपिंग के पास है।",
        "maintenance": f"कमरा {number} मेंटेनेंस के लिए बंद है।",
    }
    return spoken.get(room["status"], f"कमरा {number} की स्थिति {room['status']} है।")


def _speak_room_availability(result) -> str:
    count = result.summary.get("count", 0)
    room_type = result.summary.get("room_type")
    if not count:
        return (f"माफ़ कीजिए, इस समय कोई {room_type} खाली नहीं है।"
                if room_type else "माफ़ कीजिए, इस समय कोई कमरा खाली नहीं है।")
    if count == 1:
        only = result.records[0]
        return (f"हमारे पास एक {room_type or only['room_type']} खाली है, कमरा "
                f"{say_room_number(only['number'])}, {say_price(only['rate'])} प्रति रात।")
    cheapest = min(result.records, key=lambda r: r["rate"])
    what = f"{room_type} कमरे" if room_type else "कमरे"
    return (f"हमारे पास {say_number(count)} {what} खाली हैं, कीमत "
            f"{say_price(cheapest['rate'])} प्रति रात से शुरू होती है।")


def _speak_list_room_types(result) -> str:
    rows = result.records
    if not rows:
        return "माफ़ कीजिए, कमरों की सूची इस समय मेरे पास नहीं है।"
    cheapest = min(rows, key=lambda r: r["rate"])
    return (f"हमारे पास {_say_names([r['name'] for r in rows])} हैं, कीमत "
            f"{say_price(cheapest['rate'])} प्रति रात से शुरू होती है।")


def _speak_room_price(result) -> str:
    row = result.records[0]
    return (f"{row['name']} की कीमत {say_price(row['rate'])} प्रति रात है, "
            f"और इसमें {say_number(row['max_guests'])} लोग रह सकते हैं।")


def _speak_room_amenities(result) -> str:
    row = result.records[0]
    amenities = [_amenity(a) for a in row["amenities"]]
    if not amenities:
        return f"माफ़ कीजिए, {row['name']} की सुविधाओं की सूची मेरे पास नहीं है।"
    verb = "है" if len(amenities) == 1 else "हैं"
    return f"{row['name']} में {say_list(amenities)} {verb}।"


def _speak_list_services(result) -> str:
    rows = result.records
    if not rows:
        return "माफ़ कीजिए, सेवाओं की सूची इस समय मेरे पास नहीं है।"
    return f"हम {_say_names([_service(r['name']) for r in rows])} देते हैं।"


def _speak_service_hours(result) -> str:
    row = result.records[0]
    hours = str(row["availability"])
    if "24" in hours:
        when = "चौबीसों घंटे उपलब्ध है"
    else:
        opens, _, closes = hours.partition("-")
        when = f"{say_time(opens)} से {say_time(closes)} तक उपलब्ध है"
    # The extension is read digit by digit, the way a phone number is given -- not as a quantity.
    tail = (f" आप इसे एक्सटेंशन {say_room_number(row['extension'])} पर संपर्क कर सकते हैं।"
            if str(row["extension"]).isdigit() else "")
    return f"{_service(row['name'])} {when}।{tail}"


def _speak_check_in_out(result) -> str:
    return (f"चेक-इन {say_time(result.summary['check_in'])} से है, "
            f"और चेक-आउट {say_time(result.summary['check_out'])} तक है।")


def _speak_reservation_for_room(result) -> str:
    """States that a booking exists and its dates. Deliberately never the guest's name.

    The same rule as the English renderer, and for the same reason: a hotel line answers to whoever
    dials it. Translating the sentence must not quietly relax the privacy property.
    """
    s = result.summary
    number = say_room_number(s["room_number"])
    status = {
        "checked_in": "में मेहमान ठहरे हुए हैं",
        "confirmed": "एक पुष्ट बुकिंग के लिए रखा गया है",
        "cancelled": "अब बुक नहीं है",
    }.get(s["status"], f"की स्थिति {s['status']} है")
    return (f"कमरा {number} {status}। यह {s['room_type']} है, "
            f"{say_date(s['check_in'])} से {say_date(s['check_out'])} तक बुक है।")


_POLICY_NAMES = {
    "parking": "पार्किंग", "wifi": "Wi-Fi", "breakfast": "नाश्ता", "pets": "पालतू जानवर",
    "smoking": "धूम्रपान", "children": "बच्चे", "airport_transfer": "एयरपोर्ट ट्रांसफ़र",
    "early_check_in": "जल्दी चेक-इन", "late_check_out": "देर से चेक-आउट",
    "luggage_storage": "सामान रखने की सुविधा", "accessibility": "बिना सीढ़ी के रास्ता",
    "cancellation": "मुफ़्त रद्दीकरण", "payment": "भुगतान",
    "currency_exchange": "मुद्रा विनिमय", "laundry": "लॉन्ड्री सेवा",
    "swimming_pool": "स्विमिंग पूल", "gym": "जिम", "spa": "स्पा",
    "extra_bed": "अतिरिक्त बिस्तर", "doctor_on_call": "डॉक्टर की सुविधा",
    "taxi_booking": "टैक्सी बुकिंग", "conference_room": "कॉन्फ़्रेंस रूम",
    "power_backup": "बिजली का बैकअप", "restaurant": "रेस्टोरेंट", "bar": "बार",
    "deposit": "जमा राशि", "id_proof": "पहचान पत्र",
}

_PAYMENT_NAMES = {"card": "कार्ड", "cash": "नकद", "upi": "UPI"}

_ID_NAMES = {"passport": "पासपोर्ट", "aadhaar": "आधार कार्ड",
             "driving_licence": "ड्राइविंग लाइसेंस"}

# Timings, not offers -- see the English renderer for why these need their own branch.
_OPENING_HOURS = {"restaurant": "रेस्टोरेंट", "bar": "बार"}


def _speak_hotel_policy(result) -> str:
    row = result.records[0]
    topic = row["topic"]
    name = _POLICY_NAMES.get(topic, topic.replace("_", " "))

    if not row["available"]:
        if topic == "pets":
            return "माफ़ कीजिए, होटल में पालतू जानवरों की अनुमति नहीं है।"
        if topic == "smoking":
            return "माफ़ कीजिए, पूरा होटल धूम्रपान-मुक्त है।"
        return f"माफ़ कीजिए, हम {name} की सुविधा नहीं देते।"

    if topic == "payment" and row["options"]:
        return f"हम {say_list([_PAYMENT_NAMES.get(o, o) for o in row['options']])} स्वीकार करते हैं।"
    if topic == "cancellation" and row["limit_hours"]:
        return (f"आप आने से {say_number(row['limit_hours'])} घंटे पहले तक "
                f"बिना किसी शुल्क के रद्द कर सकते हैं।")
    if topic == "children":
        return "जी हाँ, बच्चों का स्वागत है, और उनके लिए कोई अतिरिक्त शुल्क नहीं है।"
    if topic == "accessibility":
        return "जी हाँ, होटल में बिना सीढ़ी के आने-जाने की सुविधा है।"
    if topic in _OPENING_HOURS and row["hours"]:
        opens, _, closes = str(row["hours"]).partition("-")
        return (f"{_OPENING_HOURS[topic]} {say_time(opens)} से {say_time(closes)} तक "
                f"खुला रहता है।")
    if topic == "deposit" and row["fee"]:
        return (f"चेक-इन के समय {say_price(row['fee'])} जमा राशि ली जाती है, "
                f"जो जाते समय वापस कर दी जाती है।")
    if topic == "id_proof" and row["options"]:
        # या, not और: any one of these is enough.
        names = [_ID_NAMES.get(o, o.replace("_", " ")) for o in row["options"]]
        documents = ", ".join(names[:-1]) + f" या {names[-1]}" if len(names) > 1 else names[0]
        return f"चेक-इन के समय हर मेहमान को पहचान पत्र दिखाना होता है: {documents}।"

    # Short separate sentences rather than one comma-spliced clause. Hindi puts nouns into the
    # oblique case before की/के ("नाश्ता" -> "नाश्ते की सुविधा"), which would need a second form of
    # every policy name; following the name with "उपलब्ध है" needs no inflection and reads better
    # aloud than three clauses strung together with commas.
    parts = [f"जी हाँ, {name} उपलब्ध है।"]
    if row["fee"]:
        parts.append(f"इसका शुल्क {say_price(row['fee'])} है।")
    else:
        parts.append("यह मुफ़्त है।")
    if row["hours"]:
        hours = str(row["hours"])
        if "24" in hours:
            parts.append("यह चौबीसों घंटे उपलब्ध है।")
        else:
            opens, _, closes = hours.partition("-")
            parts.append(f"समय {say_time(opens)} से {say_time(closes)} तक है।")
    return " ".join(parts)


def _speak_hotel_info(result) -> str:
    row = result.records[0]
    return (f"{row['name']} {row['address']} में है। यहाँ {say_number(row['floors'])} मंज़िलें "
            f"और {say_number(row['rooms'])} कमरे हैं।")


def _refusal(summary) -> str:
    why = summary.get("why")
    if why == "room_not_free":
        return f"माफ़ कीजिए, कमरा {say_room_number(summary['room'])} अभी खाली नहीं है।"
    if why == "none_of_that_type_free":
        return f"माफ़ कीजिए, इस समय कोई {summary['room_type']} खाली नहीं है।"
    if why == "hotel_full":
        return "माफ़ कीजिए, आज सारे कमरे भरे हुए हैं।"
    if why == "outside_hours":
        return (f"रेस्टोरेंट {say_time(summary['opens'])} से {say_time(summary['closes'])} "
                f"तक खुला रहता है, इसलिए उस समय टेबल नहीं मिल सकती।")
    if why == "party_too_large":
        return f"माफ़ कीजिए, हमारी सबसे बड़ी टेबल {say_number(summary['most'])} लोगों की है।"
    if why == "party_too_small":
        return "कितने लोगों के लिए टेबल बुक करूँ?"
    if why == "min_one_night":
        return "कम से कम एक रात की बुकिंग होती है। आप कितनी रातें रुकेंगे?"
    return "माफ़ कीजिए, यह बुकिंग नहीं हो पाई।"


def _speak_reserve_room(result) -> str:
    s = result.summary
    if not s.get("booked"):
        return _refusal(s)
    return (f"हो गया। मैंने {s['room_type']} बुक कर दिया है, कमरा "
            f"{say_room_number(s['room'])}, {say_number(s['nights'])} रात के लिए, "
            f"{say_price(s['rate'])} प्रति रात। आपका बुकिंग नंबर "
            f"{say_room_number(s['reference'])} है।")


def _speak_reserve_table(result) -> str:
    s = result.summary
    if not s.get("booked"):
        return _refusal(s)
    return (f"हो गया। {say_number(s['party_size'])} लोगों के लिए "
            f"{say_time(s['sitting'])} पर टेबल बुक है। आपका बुकिंग नंबर "
            f"{say_room_number(s['reference'])} है।")


def _speak_table_availability(result) -> str:
    s = result.summary
    free = s["free"]
    if not free:
        return f"माफ़ कीजिए, {say_time(s['sitting'])} पर सारी टेबल बुक हैं।"
    return f"जी हाँ, {say_time(s['sitting'])} पर {say_number(free)} टेबल खाली हैं।"


def _speak_cancel_booking(result) -> str:
    s = result.summary
    if not s.get("cancelled"):
        return (f"मुझे {say_room_number(s['reference'])} नंबर की कोई बुकिंग नहीं मिली। "
                f"क्या आप एक बार जाँच लेंगे?")
    return "आपकी बुकिंग रद्द कर दी गई है।"


SPEAK: dict[str, Callable[..., str]] = {
    "menu_overview": _speak_menu_overview,
    "list_category": _speak_list_category,
    "price_of": _speak_price_of,
    "find_by_diet": _speak_find_by_diet,
    "check_availability": _speak_check_availability,
    "check_allergens": _speak_check_allergens,
    "safe_for": _speak_safe_for,
    "describe_item": _speak_describe_item,
    "room_status": _speak_room_status,
    "room_availability": _speak_room_availability,
    "list_room_types": _speak_list_room_types,
    "room_price": _speak_room_price,
    "room_amenities": _speak_room_amenities,
    "list_services": _speak_list_services,
    "service_hours": _speak_service_hours,
    "check_in_out": _speak_check_in_out,
    "reservation_for_room": _speak_reservation_for_room,
    "hotel_policy": _speak_hotel_policy,
    "reserve_room": _speak_reserve_room,
    "reserve_table": _speak_reserve_table,
    "table_availability": _speak_table_availability,
    "cancel_booking": _speak_cancel_booking,
    "hotel_info": _speak_hotel_info,
}
