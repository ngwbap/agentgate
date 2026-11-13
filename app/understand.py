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
                    "must_claims": {"type": "array", "items": {"type": "string", "enum": list(CLAIM_NAMES)}},
                    "exclude_allergens": {"type": "array", "items": {"type": "string", "enum": list(ALLERGENS)}},
                    "strict_allergens": {"type": "boolean", "description": "True when 'may contain' traces must also be excluded (e.g. allergy, school or hospital kitchen)"},
                    "preferred_labels": {"type": "array", "items": {"type": "string", "enum": list(LABEL_NAMES)}},
                },
                "required": ["label", "product_terms", "quantity", "unit", "must_claims",
                             "exclude_allergens", "strict_allergens", "preferred_labels"],
                "additionalProperties": False,
            },
        },
        "deliver_by": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
        "priorities": {"type": "array", "items": {"type": "string", "enum": PRIORITIES}},
        "language": {"type": "string", "enum": ["en", "fi", "sv", "other"]},
    },
    "required": ["items", "deliver_by", "priorities", "language"],
    "additionalProperties": False,
}

SYSTEM = (
    "You turn a professional kitchen's purchasing request into structured data for a food wholesaler's catalog. "
    "Requests may be in English, Finnish or Swedish. product_terms name the product itself in all three languages, "
    "including common Finnish compound forms (e.g. maito, maitojuoma). Put dietary needs in must_claims and allergens "
    "to avoid in exclude_allergens (GS1 codes). Convert relative dates to YYYY-MM-DD using today's date. "
    "Default unit: L for liquids, KG for foods sold by weight, PCS otherwise. Do not invent requirements."
)


def understand(request: str, today: date) -> dict:
    """Structured need + how it was produced (engine, cost)."""
    if llm.available():
        try:
            data, usage = llm.ask_json(SYSTEM, f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\nRequest: {request}",
                                       NEED_SCHEMA)
            if data.get("items"):
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
_ALLERGEN_WORDS = {"nut": "AN", "nuts": "AN", "pähkinä": "AN", "peanut": "AP", "peanuts": "AP", "egg": "AE",
                   "eggs": "AE", "soy": "AY", "fish": "AF", "sesame": "AS", "milk": "AM"}
_STOP = {"i", "we", "need", "please", "buy", "order", "get", "of", "by", "the", "a", "an", "for", "find", "option",
         "that", "meets", "our", "requirements", "based", "on", "price", "and", "availability", "next", "before",
         "until", "tarvitsen", "tarvitaan", "mennessä", "ensi", "tilaa", "no", "without", "free", "lactose",
         "gluten", "kitchen", "hotel", "school", "hospital", "with", "some", "about"}


def parse_offline(request: str, today: date) -> dict:
    text = request.lower()
    qty_match = re.search(r"(\d[\d\s,.]*\d|\d)\s*([a-zäö]+)?", text)
    quantity, unit = 1.0, "PCS"
    if qty_match:
        quantity = float(re.sub(r"[\s,]", "", qty_match.group(1)))
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
                      if re.search(rf"\b(no|without|free of|ilman)\s+{w}\b", text) or f"{w}-free" in text})
    words = [w for w in re.findall(r"[a-zäöå-]+", text)
             if w not in _STOP and w not in _UNIT_WORDS and not any(w.startswith(n) for p in _WEEKDAYS for n in p)
             and w not in _CLAIM_WORDS]
    terms = []
    for w in words:
        terms.append(_FI_EN.get(w, w))
    product = " ".join(dict.fromkeys(terms)) or "product"
    return {
        "items": [{"label": product, "product_terms": list(dict.fromkeys([product, *words])), "quantity": quantity,
                   "unit": unit, "must_claims": claims, "exclude_allergens": exclude,
                   "strict_allergens": any(w in text for w in ("allergy", "allergic", "school", "hospital")),
                   "preferred_labels": []}],
        "deliver_by": deliver_by,
        "priorities": ["price"],
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
                    "match": {"type": "string", "enum": ["exact", "close", "different"]},
                    "reason": {"type": "string", "description": "Max 12 words"},
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
    "close = a reasonable substitute a chef might accept (e.g. different fat %), but not what was asked. "
    "different = a different kind of product (e.g. sour milk or coffee creamer when milk was asked). "
    "Judge only by product type and the stated requirements."
)


def judge_relevance(item: dict, candidates: list[dict]) -> tuple[dict[str, dict], dict | None]:
    """{product_id: {match, reason}} for each candidate."""
    if llm.available() and candidates:
        lines = "\n".join(f"{c['id']}: {c['name']} | {c['name_fi']}" for c in candidates)
        need = (f"Need: {item['label']}; requirements: "
                f"{', '.join(CLAIM_NAMES.get(c, c) for c in item['must_claims']) or 'none'}; "
                f"avoid allergens: {', '.join(ALLERGENS.get(a, a) for a in item['exclude_allergens']) or 'none'}")
        try:
            data, usage = llm.ask_json(RELEVANCE_SYSTEM, f"{need}\n\nCandidates:\n{lines}", RELEVANCE_SCHEMA)
            result = {j["product_id"]: {"match": j["match"], "reason": j["reason"]} for j in data["judgements"]}
            if result:
                return result, usage
        except llm.LLMUnavailable:
            pass
    terms = [t.lower() for t in item["product_terms"]]
    return {c["id"]: {"match": "exact" if any(t in f"{c['name']} {c['name_fi']}".lower() for t in terms) else "close",
                      "reason": "name matches the request" if any(t in f"{c['name']} {c['name_fi']}".lower() for t in terms)
                      else "related product"} for c in candidates}, None
