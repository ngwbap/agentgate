"""Turn a free-text purchasing request (English or Finnish) into a structured need,
and judge which candidate products really are what the buyer asked for.

Claude does the language work; if Claude is unavailable, a deterministic parser takes over
so the demo never breaks.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import llm
from .catalog import ALLERGENS, CLAIM_NAMES, LABEL_NAMES

UNITS = ["L", "KG", "PCS"]
PRIORITIES = ["price", "reliability", "low_co2", "finnish_origin", "organic"]

NEED_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short English name of what is needed"},
                    "product_terms": {"type": "array", "items": {"type": "string"},
                                      "description": "Words for the product itself in English, Finnish and Swedish (e.g. milk, maito, maitojuoma, mjölk). No attributes like lactose-free."},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string", "enum": UNITS},
                    "brand": {"type": ["string", "null"],
                              "description": "Brand the buyer named (e.g. Arla, Valio, Oatly), exactly as written; null if none"},
                    "pack_size": {"type": ["number", "null"],
                                  "description": "Size of ONE pack the buyer asked for, in `unit` (e.g. '1 l cartons' -> 1 "
                                                 "with unit L, '500 g packs' -> 0.5 with unit KG); null if no pack size was named"},
                    "must_claims": {"type": "array", "items": {"type": "string", "enum": list(CLAIM_NAMES)}},
                    "exclude_allergens": {"type": "array", "items": {"type": "string", "enum": list(ALLERGENS)}},
                    "strict_allergens": {"type": "boolean", "description": "True when 'may contain' traces must also be excluded (e.g. allergy, school or hospital kitchen)"},
                    "preferred_labels": {"type": "array", "items": {"type": "string", "enum": list(LABEL_NAMES)}},
                    "substitutes": {
                        "type": "array",
                        "description": "Up to 2 other product types a chef could use for the same purpose if the requested one cannot be delivered. Empty if none is sensible.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "product_terms": {"type": "array", "items": {"type": "string"}},
                                "trade_off": {"type": "string", "description": "What the kitchen gives up, max 15 words"},
                            },
                            "required": ["label", "product_terms", "trade_off"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["label", "product_terms", "quantity", "unit", "brand", "pack_size", "must_claims",
                             "exclude_allergens", "strict_allergens", "preferred_labels", "substitutes"],
                "additionalProperties": False,
            },
        },
        "deliver_by": {"type": ["string", "null"], "description": "YYYY-MM-DD, or null if the request gives no date"},
        "budget_eur": {"type": ["number", "null"],
                       "description": "Maximum total in EUR the buyer mentioned (e.g. 'max 1,200 €'), or null if none"},
        "priorities": {"type": "array", "items": {"type": "string", "enum": PRIORITIES}},
        "other_wishes": {"type": "array", "items": {"type": "string"},
                         "description": "Anything else the buyer asked for that none of the other fields can hold "
                                        "(e.g. 'deliver to the back door'), as short English phrases. Empty if none."},
        "language": {"type": "string", "enum": ["en", "fi", "sv", "other"]},
    },
    "required": ["items", "deliver_by", "budget_eur", "priorities", "other_wishes", "language"],
    "additionalProperties": False,
}

SYSTEM = (
    "You turn a professional kitchen's purchasing request into structured data for a food wholesaler's catalog. "
    "Requests may be in English, Finnish or Swedish. product_terms name the product itself in all three languages, "
    "including common Finnish compound forms (e.g. maito, maitojuoma). Put dietary needs in must_claims and allergens "
    "to avoid in exclude_allergens (GS1 codes). Convert relative dates to YYYY-MM-DD using today's date. "
    "Default unit: L for liquids, KG for foods sold by weight, PCS otherwise. Do not invent requirements. "
    "If the buyer names a brand, put it in brand (not in label or product_terms). If the buyer names the size of "
    "one pack (e.g. '1 l cartons', '10 l boxes'), put it in pack_size in the same unit as quantity; the total "
    "amount is never a pack size. "
    "substitutes are other product types for the same culinary purpose (not other brands or fat levels of the same "
    "product); dietary and allergen requirements still apply to them, so never suggest one that cannot meet them. "
    "Set strict_allergens for allergies and for school, daycare or hospital kitchens. "
    "Put a stated spending limit in budget_eur; leave deliver_by and budget_eur null when not stated. "
    "A wish for Finnish products goes in preferred_labels (Finnish labels) and priorities (finnish_origin); organic "
    "likewise. Put every remaining wish that no field can hold in other_wishes, never drop it. "
    "If the request names no product at all (it only adds a budget, date, allergy, priority or delivery note to an "
    "order already being made), return exactly one item with label \"\", product_terms [], quantity 0 and only the "
    "requirements that were stated; never invent a product."
)


def understand(request: str, today: date) -> dict:
    """Structured need + how it was produced (engine, cost)."""
    if llm.available():
        try:
            data, usage = llm.ask_json(SYSTEM, f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\nRequest: {request}",
                                       NEED_SCHEMA)
            if isinstance(data.get("items"), list):          # an empty list is a valid answer: no product named
                return {**data, "engine": usage["model"], "usage": usage}
        except llm.LLMUnavailable:
            pass
    return {**parse_offline(request, today), "engine": "offline parser", "usage": None}


# ---------------------------------------------------------------- offline parser

_WEEKDAYS = [("monday", "maanantai"), ("tuesday", "tiistai"), ("wednesday", "keskiviikko"),
             ("thursday", "torstai"), ("friday", "perjantai"), ("saturday", "lauantai"), ("sunday", "sunnuntai")]
_UNIT_WORDS = {"l": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L", "litraa": "L", "litra": "L",
               "kg": "KG", "kilo": "KG", "kiloa": "KG", "kilos": "KG", "pcs": "PCS", "kpl": "PCS", "pieces": "PCS"}
_FI_EN = {"maito": "milk", "maitoa": "milk", "kaurajuoma": "oat drink", "kaurajuomaa": "oat drink",
          "kerma": "cream", "kermaa": "cream", "voi": "butter", "voita": "butter", "juusto": "cheese",
          "juustoa": "cheese", "kananmunia": "eggs", "kananmuna": "eggs", "perunaa": "potatoes", "peruna": "potatoes",
          "kahvia": "coffee", "kahvi": "coffee", "jogurttia": "yoghurt", "jogurtti": "yoghurt"}
_CLAIM_WORDS = {"lactose-free": "FREE_FROM_LACTOSE", "lactose free": "FREE_FROM_LACTOSE", "laktoositon": "FREE_FROM_LACTOSE",
                "laktoositonta": "FREE_FROM_LACTOSE", "gluten-free": "FREE_FROM_GLUTEN", "gluten free": "FREE_FROM_GLUTEN",
                "gluteeniton": "FREE_FROM_GLUTEN", "gluteenitonta": "FREE_FROM_GLUTEN", "vegan": "VEGAN", "vegaaninen": "VEGAN"}
_ALLERGEN_WORDS = {"nut": "AN", "nuts": "AN", "pähkinä": "AN", "peanut": "AP", "peanuts": "AP", "maapähkinä": "AP",
                   "egg": "AE", "eggs": "AE", "soy": "AY", "fish": "AF", "sesame": "AS", "milk": "AM",
                   "hazelnut": "SH", "hazelnuts": "SH", "hasselpähkinä": "SH", "gluten": "AW", "wheat": "UW",
                   "mustard": "BM", "celery": "BC", "shellfish": "AC", "crustaceans": "AC", "lupin": "NL"}
_WISH_START = ("deliver", "delivery", "please", "call", "leave", "ring", "bring", "put", "drop", "place", "send it",
               "toimita", "toimitus", "soita", "jätä")
_WISH_WORDS = ("door", "entrance", "loading dock", "reception", "gate", "ovelle", "ovi", "takaovi")
_DATE_WORDS = re.compile(r"\b(by|before|until|mennessä|tomorrow|huomenna|\d{4}-\d{2}-\d{2})\b|"
                         r"(mon|tues|wednes|thurs|fri|satur|sun)day|maanantai|tiistai|keskiviikko|torstai|perjantai|lauantai|sunnuntai",
                         re.I)


def _split_wishes(request: str) -> tuple[list[str], str]:
    """Delivery notes ("put it at the back door") are wishes for people, not search words.
    Splits on sentences, ", " and " and " / " then "; a part that also holds the date stays in the text too."""
    wishes, kept = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", request.strip()):
        pieces = re.split(r"(,\s+|\s+and\s+|\s+then\s+)", sentence)
        out = pieces[0:1]
        for i in range(0, len(pieces), 2):
            part = pieces[i].strip().strip(".!?").strip()
            low = part.lower()
            is_wish = bool(low) and (low.startswith(_WISH_START) or any(w in low for w in _WISH_WORDS))
            if is_wish:
                clean = re.sub(r"^(?:order|and|then|also|ja)\s+", "", part, flags=re.I)
                wishes.append(clean[0].upper() + clean[1:])
            if i == 0:
                out = [pieces[0]] if (not is_wish or _DATE_WORDS.search(low)) else []
            elif not is_wish or _DATE_WORDS.search(low):
                out += [pieces[i - 1], pieces[i]]
        kept.append("".join(out))
    return wishes, " ".join(k for k in kept if k.strip())
_STOP = {"i", "we", "need", "please", "buy", "order", "get", "of", "by", "the", "a", "an", "for", "find", "option",
         "that", "meets", "our", "requirements", "based", "on", "price", "and", "availability", "next", "before",
         "until", "tarvitsen", "tarvitaan", "mennessä", "ensi", "tilaa", "no", "without", "free", "lactose",
         "gluten", "kitchen", "hotel", "school", "hospital", "with", "some", "about", "needs", "our", "us",
         "pupil", "child", "children", "kids", "has", "have", "allergy", "allergic", "allergies", "is", "are",
         "my", "budget", "euro", "euros", "eur", "max", "maximum", "cost", "costs", "spend", "change", "make", "it",
         "also", "add", "set", "only", "want", "would", "like", "can", "you", "budjetti", "euroa"}


_BUDGET = re.compile(r"(?:max(?:imum)?|budget(?: of)?|under|up to|at most|enintään|maks(?:imissaan)?|alle)\s*"
                     r"(\d[\d\s,.]*\d|\d)\s*(?:€|eur|euros?|euroa)?|(\d[\d\s,.]*\d|\d)\s*(?:€|eur\b|euros?\b|euroa\b)")


def _number(text: str) -> float:
    return float(re.sub(r"[\s,]", "", text))


def parse_offline(request: str, today: date) -> dict:
    # Notes like "Deliver to the back door" or "put it at my back door" are wishes for people, not search words.
    wishes, kept_text = _split_wishes(request)
    text = kept_text.lower()
    budget = None
    if b := _BUDGET.search(text):
        budget = _number(b.group(1) or b.group(2))
        text = text[:b.start()] + " " + text[b.end():]       # so the budget is not read as the quantity
    qty_match = re.search(r"(\d[\d\s,.]*\d|\d)\s*([a-zäö]+)?", text)
    quantity, unit = 1.0, "PCS"
    if qty_match:
        quantity = _number(qty_match.group(1))
        unit = _UNIT_WORDS.get((qty_match.group(2) or "").strip(), "PCS")
    deliver_by = None
    if m := re.search(r"\d{4}-\d{2}-\d{2}", text):
        deliver_by = m.group(0)
    else:
        for i, names in enumerate(_WEEKDAYS):
            if any(re.search(rf"\b{n}", text) for n in names):
                deliver_by = (today + timedelta(days=(i - today.weekday()) % 7 or 7)).isoformat()
                break
    claims = sorted({c for w, c in _CLAIM_WORDS.items() if w in text})
    exclude = sorted({code for w, code in _ALLERGEN_WORDS.items()
                      if re.search(rf"\b(no|without|free of|ilman)\s+{w}\b", text) or f"{w}-free" in text
                      or re.search(rf"\b{w}\s+allerg", text) or re.search(rf"allergic\s+to\s+{w}\b", text)})
    # The product is usually named right after the amount: "40 kg of chocolate cookies by Monday".
    source = text
    if qty_match:
        unit_word = (qty_match.group(2) or "").strip()
        rest = text[qty_match.end():] if unit_word in _UNIT_WORDS else text[qty_match.end(1):]
        clause = re.split(r"\b(?:by|before|until|for|mennessä|ennen)\b|[,.;!?]", rest, maxsplit=1)[0]
        if re.search(r"[a-zäöå]", clause):
            source = clause
    words = [w for w in re.findall(r"[a-zäöå-]+", source)
             if w not in _STOP and w not in _UNIT_WORDS and not any(w.startswith(n) for p in _WEEKDAYS for n in p)
             and w not in _CLAIM_WORDS]
    terms = []
    for w in words:
        terms.append(_FI_EN.get(w, w))
    product = " ".join(dict.fromkeys(terms))   # empty = no product named (e.g. "my budget is 50 euro")
    return {
        # Search for the whole name ("chocolate cookies" = both words), plus the Finnish words we translated,
        # never each English word alone ("chocolate" alone would bring every chocolate bar).
        "items": [{"label": product, "product_terms": list(dict.fromkeys([product, *[w for w in words if w in _FI_EN]])) if product else [],
                   "quantity": quantity if product else 0,
                   "unit": unit, "brand": None, "pack_size": None, "must_claims": claims, "exclude_allergens": exclude,
                   "strict_allergens": any(w in text for w in ("allergy", "allergic", "school", "hospital", "daycare",
                                                                "koulu", "sairaala", "päiväkoti", "allergia")),
                   "preferred_labels": [], "substitutes": []}],
        "deliver_by": deliver_by,
        "budget_eur": budget,
        "priorities": ["price"],
        "other_wishes": [w for w in wishes if w.strip()],
        "language": "fi" if re.search(r"[äö]|tarvitsen|litraa", text) else "en",
    }


# ---------------------------------------------------------------- relevance judging

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "match": {"type": "string", "enum": ["exact", "close", "substitute", "different"]},
                    "reason": {"type": "string", "description": "Max 12 words. For close and substitute: what the kitchen gives up."},
                },
                "required": ["product_id", "match", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}

RELEVANCE_SYSTEM = (
    "You check which wholesale products match a professional kitchen's need. "
    "exact = the product the buyer asked for (any brand or pack size). "
    "close = the same product type in a variant the buyer did not ask for (e.g. different fat %, UHT), "
    "which a chef would usually accept. "
    "substitute = a different product type that serves the same culinary purpose (e.g. oat drink for milk) and "
    "still meets every stated requirement; the reason says what the kitchen gives up. "
    "different = a different kind of product that does not serve the purpose (e.g. sour milk or coffee creamer "
    "when milk was asked), or one that cannot meet the requirements. "
    "Judge only by product type and the stated requirements."
)


def _has_word(word: str, text_tokens: list[str]) -> bool:
    """Does the product name contain this word (allowing plural endings and Finnish compounds)?"""
    w = word.lower()
    stem = w if len(w) <= 4 else w[: max(4, len(w) - 2)]
    return any(t.startswith(stem) or (len(stem) >= 5 and stem in t) for t in text_tokens)


def _offline_match(item: dict, c: dict) -> tuple[str, str]:
    """Without the AI: exact if every word of the product appears, close if the main noun (last word) does."""
    tokens = re.findall(r"[a-zåäöéü]+", f"{c['name']} {c['name_fi']}".lower())
    words = [w for w in re.findall(r"[a-zåäöéü]+", item["label"].lower()) if len(w) > 2] or [item["label"].lower()]
    if all(_has_word(w, tokens) for w in words):
        return "exact", "name matches the request"
    if _has_word(words[-1], tokens):
        return "close", f"a kind of {words[-1]}, not exactly as asked"
    return "different", f"not a {item['label']}"


def judge_relevance(item: dict, candidates: list[dict],
                    substitute_candidates: list[dict] = (), valio_ids=()) -> tuple[dict[str, dict], dict | None]:
    """{product_id: {match, reason}} for the primary candidates and the substitute ideas, in ONE Claude call.

    substitute_candidates = [{"product": p, "label": ..., "trade_off": ...}] found for the need's substitutes.
    """
    everything = list(candidates) + [s["product"] for s in substitute_candidates]
    if llm.available() and everything:
        lines = "\n".join(f"{c['id']}: {c['name']} | {c['name_fi']}" for c in candidates)
        if substitute_candidates:
            lines += "\n\nPossible substitutes (other product types, only useful if the above cannot be delivered):\n"
            lines += "\n".join(f"{s['product']['id']}: {s['product']['name']} | {s['product']['name_fi']} "
                               f"[idea: {s['label']}]" for s in substitute_candidates)
        need = (f"Need: {item['label']}; requirements: "
                f"{', '.join(CLAIM_NAMES.get(c, c) for c in item['must_claims']) or 'none'}; "
                f"avoid allergens: {', '.join(ALLERGENS.get(a, a) for a in item['exclude_allergens']) or 'none'}")
        try:
            data, usage = llm.ask_json(RELEVANCE_SYSTEM, f"{need}\n\nCandidates:\n{lines}", RELEVANCE_SCHEMA)
            known = {c["id"] for c in everything}
            result = {j["product_id"]: {"match": j["match"], "reason": j["reason"]} for j in data["judgements"]
                      if j["product_id"] in known}
            if result:
                return result, usage
        except llm.LLMUnavailable:
            pass
    result = {}
    for c in candidates:
        match, reason = _offline_match(item, c)
        if match == "different" and c["id"] in valio_ids:       # Valio itself lists it as a replacement
            match, reason = "close", "Valio lists it as a replacement"
        result[c["id"]] = {"match": match, "reason": reason}
    for s in substitute_candidates:
        result.setdefault(s["product"]["id"], {"match": "substitute", "reason": s["trade_off"] or f"{s['label']} instead"})
    return result, None
