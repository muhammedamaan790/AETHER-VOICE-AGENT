"""The same seventeen answers, in Spanish.

One renderer per tool, mirroring `aether/hotel/tools.py` sentence for sentence, exactly as
`tools_hi.py` does for Hindi. **The tools, the router, `ToolRunner` and the database are untouched**
-- a tool still returns `(records, summary)` and only the final string differs.

That is the whole design: **one hotel, one set of facts, three sets of words.** A price lives in
exactly one place, `data/aether_hotel.db`, so English, Hindi and Spanish cannot disagree about it.
Adding a language is a renderer plus a number module, never a second copy of the hotel -- which is
what makes "the Chicken Kebab is 420" impossible to change in one language and forget in another.

`llm_ms` stays 0 here too. Handing a price to a model to phrase in Spanish would hand it the chance
to say the wrong one, in a language fewer people in the room can check.

**What deliberately stays in English.** Dish names, room-type names and service names are the
hotel's own proper nouns, printed on its own menu and door signs. A caller asking for the "Chicken
Kebab" wants to hear "Chicken Kebab".

**Not certified by its author.** Written to be reviewed by a Spanish speaker before it is spoken to
anyone. Where it is wrong it is wrong in a template -- one file, one line -- not in a model's output.
"""

from __future__ import annotations

from collections.abc import Callable

from .speech_es import say_date, say_list, say_number, say_price, say_room_number, say_time

NOT_FOUND = "Lo siento, no he encontrado eso. ¿Puede repetirlo, por favor?"

_CATEGORIES = {
    "starters": "entrantes",
    "mains": "platos principales",
    "vegetarian mains": "platos principales vegetarianos",
    "desserts": "postres",
    "drinks": "bebidas",
}

# Two forms, because Spanish uses these words in two different grammatical roles. As a NOUN
# phrase -- "opciones veganas" -- the adjective agrees with the feminine plural `opciones`; as a
# standalone scope it reads as a category. A single form gives "con opciones vegetariano", which is
# the sort of agreement error a Spanish speaker hears immediately.
_DIETS = {
    "vegetarian": "vegetariano",
    "vegan": "vegano",
    "non-vegetarian": "no vegetariano",
}

_DIETS_FEM_PL = {
    "vegetarian": "vegetarianas",
    "vegan": "veganas",
    "non-vegetarian": "no vegetarianas",
}


def _diet_fem_pl(name: str) -> str:
    """The form that agrees with `opciones` (feminine, plural)."""
    return _DIETS_FEM_PL.get(str(name).lower(), _DIETS.get(str(name).lower(), str(name)))

_ALLERGENS = {
    "nuts": "frutos secos",
    "dairy": "lácteos",
    "gluten": "gluten",
    "fish": "pescado",
    "egg": "huevo",
    "eggs": "huevos",
    "shellfish": "mariscos",
}

_AMENITIES = {
    "King bed": "cama de matrimonio grande",
    "Two twin beds": "dos camas individuales",
    "twin sofa beds": "dos sofás cama",
    "living room": "sala de estar",
    "work desk": "escritorio",
    "air conditioning": "aire acondicionado",
    "city view": "vistas a la ciudad",
    "breakfast": "desayuno",
    "TV": "televisión",
    "smart TV": "televisión inteligente",
    "two TVs": "dos televisiones",
    "Wi-Fi": "Wi-Fi",
    "minibar": "minibar",
}

_SERVICES = {
    "Front Desk": "la recepción",
    "Housekeeping": "el servicio de limpieza",
    "Luggage Assistance": "el servicio de equipaje",
    "Maintenance": "el mantenimiento",
    "Room Service": "el servicio de habitaciones",
    "Wake-up Call": "el servicio de despertador",
}

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
    more = "uno más" if rest == 1 else f"{say_number(rest)} más"
    return f"{say_list(names[:_SPOKEN_LIST_MAX])}, y {more}"


# --- the seventeen ---------------------------------------------------------------------------

def _speak_menu_overview(result) -> str:
    categories = [_category(c) for c in (result.summary.get("categories") or [])]
    diets = [_diet_fem_pl(d) for d in (result.summary.get("diets") or [])]
    if not categories:
        return "Lo siento, hoy no estamos sirviendo nada."
    lead = f"Tenemos {say_list(categories)}"
    return f"{lead}, con opciones {say_list(diets)}." if diets else f"{lead}."


def _speak_list_category(result) -> str:
    rows = result.records
    category = _category(result.summary.get("category", "menú"))
    sold_out = result.summary.get("sold_out") or []
    if not rows:
        return f"Lo siento, ahora mismo no tenemos nada en {category}."
    names = _say_names([r["name"] for r in rows])
    cheapest = min(rows, key=lambda r: r["price"])
    lead = f"De {category} tenemos {names}."
    price = (f" Cuesta {say_price(rows[0]['price'])}." if len(rows) == 1
             else f" Los precios empiezan en {say_price(cheapest['price'])}.")
    if sold_out:
        verb = "está" if len(sold_out) == 1 else "están"
        tail = f" {say_list(sold_out)} no {verb} disponible hoy."
    else:
        tail = ""
    return lead + price + tail


def _speak_price_of(result) -> str:
    dish = result.records[0]
    price = say_price(dish["price"])
    if not dish["available"]:
        return f"{dish['name']} cuesta {price}, pero hoy no está disponible."
    return f"{dish['name']} cuesta {price}."


def _speak_find_by_diet(result) -> str:
    rows = result.records
    diet_raw = result.summary.get("diet", "")
    category_raw = result.summary.get("category")
    # The database makes "vegetarian mains" a category of its own, so naming the diet again would
    # stutter -- the same case the English and Hindi renderers handle.
    names = _say_names([r["name"] for r in rows]) if rows else ""
    # Two shapes, because the agreement differs. Following the feminine plural `opciones` the diet
    # is an adjective and must agree with it ("opciones veganas"); naming a category instead uses
    # the category's own already-correct phrase ("platos principales vegetarianos").
    if category_raw:
        scope = _category(category_raw)
        if diet_raw.split("-")[-1] not in category_raw:
            scope = f"{scope} {_diet(diet_raw)}"
        if not rows:
            return f"Lo siento, hoy no tenemos {scope}."
        return f"Sí. De {scope} tenemos {names}."
    scope = _diet_fem_pl(diet_raw)
    if not rows:
        return f"Lo siento, hoy no tenemos opciones {scope}."
    return f"Sí. Entre nuestras opciones {scope} tenemos {names}."


def _speak_check_availability(result) -> str:
    dish = result.records[0]
    if dish["available"]:
        return f"Sí, {dish['name']} está disponible hoy, por {say_price(dish['price'])}."
    return f"Lo siento, {dish['name']} no está disponible hoy."


def _speak_check_allergens(result) -> str:
    dish = result.records[0]
    allergens = [_allergen(a) for a in dish["allergens"]]
    if not allergens:
        return f"{dish['name']} no tiene alérgenos declarados."
    return f"{dish['name']} contiene {say_list(allergens)}."


def _speak_safe_for(result) -> str:
    """Suggest, then warn -- the English shape, because the shape is the safety property."""
    allergen = _allergen(result.summary.get("allergen", ""))
    rows = result.records
    if not rows:
        return f"Lo siento, todo lo que tenemos hoy contiene {allergen}."
    picks, seen = [], set()
    for row in rows:
        if row["category"] not in seen:
            seen.add(row["category"])
            picks.append(row["name"])
        if len(picks) == 3:
            break
    lead = f"Si evita los {allergen}, le sugeriría {say_list(picks)}."
    avoid = result.summary.get("avoid") or []
    if not avoid:
        return f"{lead} Nada más en nuestra carta contiene {allergen}."
    if len(avoid) == 1:
        count = "Otro plato de la carta contiene"
    else:
        count = f"Otros {say_number(len(avoid))} platos de la carta contienen"
    return f"{lead} {count} {allergen}, así que consúlteme antes de pedir."


def _speak_describe_item(result) -> str:
    dish = result.records[0]
    description = (result.summary.get("description") or "").strip()
    if not description:
        return f"Lo siento, no tengo una descripción de {dish['name']}."
    # The hotel's own English prose, read back as written. Inventing a Spanish paraphrase would be
    # the fabrication this whole layer exists to prevent.
    tail = "" if dish["available"] else " Aunque hoy no está disponible."
    return f"{description}{tail}"


def _speak_room_status(result) -> str:
    if not result.summary.get("exists", True):
        return (f"No tenemos la habitación {say_room_number(result.summary['number'])}. "
                f"Nuestras habitaciones van de la {say_room_number(result.summary['lowest'])} "
                f"a la {say_room_number(result.summary['highest'])}.")

    room = result.records[0]
    number = say_room_number(room["number"])
    spoken = {
        "available": f"La habitación {number} está libre. Es una {room['room_type']}, "
                     f"por {say_price(room['rate'])} la noche.",
        "occupied": f"La habitación {number} está ocupada en este momento.",
        "reserved": f"La habitación {number} ya está reservada.",
        "housekeeping": f"La habitación {number} está con el servicio de limpieza ahora mismo.",
        "maintenance": f"La habitación {number} está en mantenimiento.",
    }
    return spoken.get(room["status"], f"La habitación {number} figura como {room['status']}.")


def _speak_room_availability(result) -> str:
    count = result.summary.get("count", 0)
    room_type = result.summary.get("room_type")
    if not count:
        return (f"Lo siento, no tenemos ninguna {room_type} libre en este momento."
                if room_type else "Lo siento, no tenemos ninguna habitación libre en este momento.")
    if count == 1:
        only = result.records[0]
        return (f"Tenemos una {room_type or only['room_type']} libre, la habitación "
                f"{say_room_number(only['number'])}, por {say_price(only['rate'])} la noche.")
    cheapest = min(result.records, key=lambda r: r["rate"])
    what = f"habitaciones {room_type}" if room_type else "habitaciones"
    # `habitación` is feminine, so the count agrees: "cuarenta y una habitaciones".
    return (f"Tenemos {say_number(count, feminine=True)} {what} libres, desde "
            f"{say_price(cheapest['rate'])} la noche.")


def _speak_list_room_types(result) -> str:
    rows = result.records
    if not rows:
        return "Lo siento, no tengo a mano nuestros tipos de habitación."
    cheapest = min(rows, key=lambda r: r["rate"])
    return (f"Tenemos {_say_names([r['name'] for r in rows])}, desde "
            f"{say_price(cheapest['rate'])} la noche.")


def _speak_room_price(result) -> str:
    row = result.records[0]
    return (f"{row['name']} cuesta {say_price(row['rate'])} la noche, "
            f"y admite hasta {say_number(row['max_guests'])} personas.")


def _speak_room_amenities(result) -> str:
    row = result.records[0]
    amenities = [_amenity(a) for a in row["amenities"]]
    if not amenities:
        return f"Lo siento, no tengo la lista de servicios de {row['name']}."
    return f"{row['name']} incluye {say_list(amenities)}."


def _speak_list_services(result) -> str:
    rows = result.records
    if not rows:
        return "Lo siento, no tengo a mano nuestra lista de servicios."
    # Stripped here: "Ofrecemos la recepción, el servicio de limpieza..." is not how a list is read.
    bare = [_service(r["name"]).split(" ", 1)[1] if _service(r["name"]).startswith(("el ", "la "))
            else _service(r["name"]) for r in rows]
    return f"Ofrecemos {_say_names(bare)}."


def _speak_service_hours(result) -> str:
    row = result.records[0]
    hours = str(row["availability"])
    if "24" in hours:
        when = "está disponible las veinticuatro horas"
    else:
        opens, _, closes = hours.partition("-")
        when = f"está disponible desde {say_time(opens)} hasta {say_time(closes)}"
    # The extension is read digit by digit, the way a phone number is given, not as a quantity.
    # The pronoun agrees with the service's own gender: "la recepción ... contactarla", but
    # "el servicio de habitaciones ... contactarlo".
    spoken_name = _service(row["name"])
    pronoun = "contactarla" if spoken_name.startswith("la ") else "contactarlo"
    tail = (f" Puede {pronoun} en la extensión {say_room_number(row['extension'])}."
            if str(row["extension"]).isdigit() else "")
    # The article is part of the stored name because it varies with gender: "la recepción" but
    # "el servicio de limpieza". A fixed "El" in the template produced "El recepción".
    return f"{_service(row['name']).capitalize()} {when}.{tail}"


def _speak_check_in_out(result) -> str:
    return (f"La entrada es a partir de {say_time(result.summary['check_in'])}, "
            f"y la salida es hasta {say_time(result.summary['check_out'])}.")


def _speak_reservation_for_room(result) -> str:
    """States that a booking exists and its dates. Deliberately never the guest's name.

    The same rule as the other renderers, and for the same reason: a hotel line answers to whoever
    dials it. Translating the sentence must not quietly relax the privacy property.
    """
    s = result.summary
    number = say_room_number(s["room_number"])
    status = {
        "checked_in": "está ocupada por un huésped que ya ha llegado",
        "confirmed": "está reservada con una reserva confirmada",
        "cancelled": "ya no está reservada",
    }.get(s["status"], f"figura como {s['status']}")
    return (f"La habitación {number} {status}. Es una {s['room_type']}, "
            f"reservada desde {say_date(s['check_in'])} hasta {say_date(s['check_out'])}.")


_POLICY_NAMES = {
    "parking": "aparcamiento", "wifi": "Wi-Fi", "breakfast": "desayuno",
    "pets": "mascotas", "smoking": "fumar", "children": "niños",
    "airport_transfer": "traslado al aeropuerto", "early_check_in": "entrada anticipada",
    "late_check_out": "salida tardía", "luggage_storage": "consigna de equipaje",
    "accessibility": "acceso sin escalones", "cancellation": "cancelación gratuita",
    "payment": "pago", "currency_exchange": "cambio de divisas",
    "laundry": "servicio de lavandería",
    "swimming_pool": "una piscina", "gym": "un gimnasio", "spa": "un spa",
    "extra_bed": "una cama supletoria", "doctor_on_call": "un médico de guardia",
    "taxi_booking": "reserva de taxi", "conference_room": "una sala de conferencias",
    "power_backup": "generador de emergencia", "restaurant": "el restaurante", "bar": "el bar",
    "deposit": "un depósito", "id_proof": "documento de identidad",
}

_PAYMENT_NAMES = {"card": "tarjeta", "cash": "efectivo", "upi": "UPI"}

_ID_NAMES = {"passport": "un pasaporte", "aadhaar": "una tarjeta Aadhaar",
             "driving_licence": "un permiso de conducir"}

# Horarios, no servicios -- ver el renderizador en inglés para el porqué de esta rama aparte.
_OPENING_HOURS = {"restaurant": "El restaurante", "bar": "El bar"}


def _speak_hotel_policy(result) -> str:
    row = result.records[0]
    topic = row["topic"]
    name = _POLICY_NAMES.get(topic, topic.replace("_", " "))

    if not row["available"]:
        if topic == "pets":
            return "Lo siento, no se admiten mascotas en el hotel."
        if topic == "smoking":
            return "Lo siento, todo el hotel es libre de humo."
        return f"Lo siento, no ofrecemos {name}."

    if topic == "payment" and row["options"]:
        return f"Aceptamos {say_list([_PAYMENT_NAMES.get(o, o) for o in row['options']])}."
    if topic == "cancellation" and row["limit_hours"]:
        return (f"Puede cancelar sin coste hasta {say_number(row['limit_hours'])} "
                f"horas antes de su llegada.")
    if topic == "children":
        return "Sí, los niños son muy bienvenidos y se alojan sin coste adicional."
    if topic == "accessibility":
        return "Sí, el hotel tiene acceso sin escalones."
    if topic in _OPENING_HOURS and row["hours"]:
        opens, _, closes = str(row["hours"]).partition("-")
        return (f"{_OPENING_HOURS[topic]} abre desde {say_time(opens)} "
                f"hasta {say_time(closes)}.")
    if topic == "deposit" and row["fee"]:
        return (f"Se toma un depósito reembolsable de {say_price(row['fee'])} a la entrada, "
                f"que se devuelve al salir.")
    if topic == "id_proof" and row["options"]:
        # "o", no "y": basta con uno de los tres.
        names = [_ID_NAMES.get(o, o.replace("_", " ")) for o in row["options"]]
        documents = ", ".join(names[:-1]) + f" o {names[-1]}" if len(names) > 1 else names[0]
        return f"Cada huésped necesita un documento de identidad a la entrada: {documents}."

    bits = [f"Sí, ofrecemos {name}"]
    if row["fee"]:
        bits.append(f"por {say_price(row['fee'])}")
    else:
        bits.append("sin coste")
    if row["hours"]:
        hours = str(row["hours"])
        if "24" in hours:
            bits.append("las veinticuatro horas")
        else:
            opens, _, closes = hours.partition("-")
            bits.append(f"desde {say_time(opens)} hasta {say_time(closes)}")
    return " ".join(bits) + "."


def _speak_hotel_info(result) -> str:
    row = result.records[0]
    return (f"{row['name']} está en {row['address']}. Tenemos {say_number(row['floors'])} plantas "
            f"y {say_number(row['rooms'])} habitaciones.")


def _refusal(summary) -> str:
    why = summary.get("why")
    if why == "room_not_free":
        return f"Lo siento, la habitación {say_room_number(summary['room'])} no está libre."
    if why == "none_of_that_type_free":
        return f"Lo siento, no tenemos ninguna {summary['room_type']} libre ahora mismo."
    if why == "hotel_full":
        return "Lo siento, esta noche estamos completos."
    if why == "outside_hours":
        return (f"El restaurante sirve de {say_time(summary['opens'])} a "
                f"{say_time(summary['closes'])}, así que no puedo reservar a esa hora.")
    if why == "party_too_large":
        return f"Lo siento, nuestra mesa más grande es para {say_number(summary['most'])}."
    if why == "party_too_small":
        return "¿Para cuántas personas reservo la mesa?"
    if why == "min_one_night":
        return "La estancia mínima es de una noche. ¿Cuántas noches desea?"
    return "Lo siento, no he podido hacer esa reserva."


def _speak_reserve_room(result) -> str:
    s = result.summary
    if not s.get("booked"):
        return _refusal(s)
    noches = "noche" if s["nights"] == 1 else "noches"
    return (f"Hecho. He reservado la {s['room_type']}, habitación "
            f"{say_room_number(s['room'])}, para {say_number(s['nights'])} {noches} "
            f"a {say_price(s['rate'])} por noche. Su referencia es "
            f"{say_room_number(s['reference'])}.")


def _speak_reserve_table(result) -> str:
    s = result.summary
    if not s.get("booked"):
        return _refusal(s)
    return (f"Hecho. Una mesa para {say_number(s['party_size'])} a {say_time(s['sitting'])}. "
            f"Su referencia es {say_room_number(s['reference'])}.")


def _speak_table_availability(result) -> str:
    s = result.summary
    free = s["free"]
    if not free:
        return f"Lo siento, estamos completos a {say_time(s['sitting'])}."
    mesas = "mesa libre" if free == 1 else "mesas libres"
    return f"Sí, tenemos {say_number(free)} {mesas} a {say_time(s['sitting'])}."


def _speak_cancel_booking(result) -> str:
    s = result.summary
    if not s.get("cancelled"):
        return (f"No encuentro ninguna reserva con la referencia "
                f"{say_room_number(s['reference'])}. ¿Puede comprobarla?")
    return "Queda cancelada. Su reserva ya no está retenida."


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
