"""Static product index built from Valio Aimo's product data file (no API calls).

Lookup narrows 17,546 products down to a handful here, offline, before any live price or
availability query - exactly what Valio's API docs ask for.
"""
from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from . import config

# GS1 allergen type codes used in the data -> readable names
ALLERGENS = {
    "AM": "milk", "ML": "lactose", "AW": "cereals containing gluten", "UW": "wheat", "GB": "barley",
    "NR": "rye", "GO": "oats", "GS": "spelt", "AE": "egg", "AF": "fish", "AC": "crustaceans",
    "UM": "molluscs", "AP": "peanuts", "AN": "tree nuts", "SA": "almond", "SH": "hazelnut",
    "SW": "walnut", "SC": "cashew", "SP": "pecan", "SR": "brazil nut", "ST": "pistachio",
    "SM": "macadamia", "AY": "soy", "AS": "sesame", "BC": "celery", "BM": "mustard",
    "NL": "lupin", "AU": "sulphites", "X99": "other allergen",
}
CLAIM_NAMES = {
    "FREE_FROM_LACTOSE": "lactose-free", "LOW_LACTOSE": "low-lactose", "FREE_FROM_GLUTEN": "gluten-free",
    "VEGAN": "vegan", "VEGETARIAN": "vegetarian", "FREE_FROM_EGGS": "egg-free", "FREE_FROM_SOY": "soy-free",
    "NO_ADDED_SUGARS": "no added sugar", "HIGH_PROTEIN": "high protein", "HIGH_FIBRE": "high fibre",
    "FREE_FROM_PRESERVATIVES": "no preservatives", "FREE_FROM_ADDITIVES": "no additives",
    "LOW_FAT": "low fat", "FREE_FROM_FAT": "fat-free", "LOW_SODIUM_SALT": "low salt",
    "FREE_FROM_WHEAT": "wheat-free",
}
LABEL_NAMES = {
    "EU_ORGANIC_FARMING": "EU organic", "GOODS_FROM_FINLAND_BLUE_SWAN": "Goods from Finland (Hyvää Suomesta)",
    "MADE_IN_FINLAND_FLAG_WITH_KEY": "Made in Finland (Key Flag)", "FINNISH_HEART_SYMBOL": "Finnish Heart symbol",
    "SVANEN": "Nordic Swan Ecolabel", "RAINFOREST_ALLIANCE": "Rainforest Alliance", "EU_ECO_LABEL": "EU Ecolabel",
    "MARINE_STEWARDSHIP_COUNCIL_LABEL": "MSC", "FAIR_TRADE_MARK": "Fairtrade",
    "AQUACULTURE_STEWARDSHIP_COUNCIL": "ASC", "KRAV_MARK": "KRAV",
}
# Claims can also be evidenced by product names when the structured field is missing.
CLAIM_NAME_HINTS = {
    "FREE_FROM_LACTOSE": ("laktoositon", "lactose free", "lactose-free", "laktosfri"),
    "FREE_FROM_GLUTEN": ("gluteeniton", "gluten free", "gluten-free", "glutenfri"),
    "VEGAN": ("vegaani", "vegan"),
}
# A claim can be proven by a stronger one: a vegan product has no milk, so no lactose and no egg.
CLAIM_IMPLIED_BY = {
    "FREE_FROM_LACTOSE": ("VEGAN",), "LOW_LACTOSE": ("FREE_FROM_LACTOSE", "VEGAN"),
    "FREE_FROM_EGGS": ("VEGAN",), "VEGETARIAN": ("VEGAN",),
}
VOLUME_TO_L ={"LTR": 1.0, "MLT": 0.001, "CLT": 0.01, "DLT": 0.1, "MTQ": 1000.0}
WEIGHT_TO_KG = {"KGM": 1.0, "GRM": 0.001, "MGM": 0.000001}
_NAME_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|l|kg|g)\b", re.I)
_TOKEN = re.compile(r"[a-zåäöéü0-9]+(?:-[a-zåäöéü0-9]+)*", re.I)
NAME_UNIT = {"ml": ("L", 0.001), "cl": ("L", 0.01), "dl": ("L", 0.1), "l": ("L", 1.0),
             "g": ("KG", 0.001), "kg": ("KG", 1.0)}


def _name(names: list[dict], lang: str) -> str:
    return next((n["value"] for n in names or [] if n.get("language") == lang), "")


def _classification(sd: dict, name: str) -> list[dict]:
    for c in sd.get("classifications") or []:
        if c.get("name") == name:
            return c.get("values") or []
    return []


def _content_per_base_unit(p: dict, sd: dict) -> tuple[str | None, float | None, str]:
    """(unit 'L'|'KG', amount per base unit, source) from structured data, else from the name."""
    if p.get("baseUnit") == "KG":
        return "KG", 1.0, "base unit is kg"
    each = next((c for c in sd.get("unitConversions") or [] if c.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH"), None)
    if each:
        vol = each.get("volume") or {}
        if vol.get("unit") in VOLUME_TO_L and vol.get("value"):
            return "L", vol["value"] * VOLUME_TO_L[vol["unit"]], "product data (volume)"
        net = each.get("netWeight") or {}
        if net.get("unit") in WEIGHT_TO_KG and net.get("value"):
            return "KG", net["value"] * WEIGHT_TO_KG[net["unit"]], "product data (net weight)"
    for lang in ("en", "fi"):
        m = list(_NAME_SIZE.finditer(_name(sd.get("names"), lang)))
        if m:
            amount, unit = m[-1].group(1), m[-1].group(2).lower()
            kind, factor = NAME_UNIT[unit]
            return kind, float(amount.replace(",", ".")) * factor, "parsed from name"
    return None, None, "unknown"


def _read_csv(path: Path, value_col: str) -> dict[str, float]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        return {row["productId"]: float(row[value_col]) for row in csv.DictReader(f) if row.get(value_col)}


def build_product(p: dict, reliability: dict, co2: dict) -> dict:
    sd = p.get("synkkaData") or {}
    names = sd.get("names") or []
    units = {u["unitId"]: float(u.get("sizeInBaseUnits") or 1) for u in p.get("units") or []}
    per_sales = units.get(p["salesUnit"], 1.0)
    kind, per_base, source = _content_per_base_unit(p, sd)
    allergens = _classification(sd, "allergen")
    name_en, name_fi, name_sv = _name(names, "en"), _name(names, "fi"), _name(names, "sv")
    claims = {v["id"] for v in _classification(sd, "nutritionalClaim")}
    lowered = f"{name_en} {name_fi} {name_sv}".lower()
    for claim, hints in CLAIM_NAME_HINTS.items():
        if claim not in claims and any(h in lowered for h in hints):
            claims.add(claim)
    return {
        "id": p["id"],
        "name": name_en or name_fi or name_sv or p["id"],
        "name_fi": name_fi,
        "brand": sd.get("brand") or "",
        "vendor": p.get("vendorName") or "",
        "origin": (p.get("countryOfOrigin") or "").upper(),
        "category": p.get("category"),
        "sales_unit": p["salesUnit"],
        "base_unit": p["baseUnit"],
        "base_units_per_sales_unit": per_sales,
        "lot_size": p.get("allowedLotSize") or 1,
        "content_unit": kind,
        "content_per_sales_unit": round(per_base * per_sales, 4) if per_base else None,
        "content_source": source,
        "contains": sorted({v["id"] for v in allergens if v.get("unit") == "CONTAINS"}),
        "may_contain": sorted({v["id"] for v in allergens if v.get("unit") == "MAY_CONTAIN"}),
        "free_from": sorted({v["id"] for v in _classification(sd, "nonAllergen") if v.get("unit") == "FREE_FROM"}),
        "claims": sorted(claims),
        "labels": sorted(v["id"] for v in _classification(sd, "packagingMarkedLabel") if v["id"] in LABEL_NAMES),
        "temperature": p.get("temperatureCondition"),
        "data_updated": sd.get("lastUpdated"),
        "gtin": p.get("salesUnitGtin") or sd.get("gtin"),
        "reliability": reliability.get(p["id"]),
        "co2_per_kg": co2.get(p["id"]),
        "_tokens": {t.lower() for t in _TOKEN.findall(f"{name_en} {name_fi} {name_sv} {sd.get('brand') or ''}")},
    }


class Catalog:
    def __init__(self, products: list[dict]):
        self.products = {p["id"]: p for p in products}
        self.by_category: dict[str, list[str]] = defaultdict(list)
        self.index: dict[str, set[str]] = defaultdict(set)
        for p in products:
            self.by_category[p["category"]].append(p["id"])
            for t in p["_tokens"]:
                self.index[t].add(p["id"])
        self.vocabulary = sorted(self.index)

    @classmethod
    def from_files(cls, data_dir: Path = config.VALIO_DATA_DIR) -> "Catalog":
        raw = json.loads((data_dir / "products.json").read_text(encoding="utf-8"))
        reliability = _read_csv(data_dir / "delivery_reliability.csv", "deliveryReliability")
        co2 = _read_csv(data_dir / "carbon_footprint.csv", "AVG kg CO2-eq/kg")
        return cls([build_product(p, reliability, co2) for p in raw if not p.get("deleted")])

    def __len__(self) -> int:
        return len(self.products)

    # ------------------------------------------------------------ search

    def _match_term(self, term: str) -> set[str]:
        """Products whose name has a word starting with the term's stem (handles Finnish endings)."""
        term = term.lower().strip()
        if not term:
            return set()
        words = term.split()
        hits = None
        for w in words:
            stem = w if len(w) <= 4 else w[: max(4, len(w) - 2)]
            ids = set()
            for token in self.vocabulary:
                # Prefix match handles Finnish endings (maitoa -> maito); for longer stems also match
                # inside compound words (viini -> punaviini, maito -> kevytmaitojuoma).
                if token.startswith(stem) or (len(stem) >= 5 and stem in token):
                    ids |= self.index[token]
            hits = ids if hits is None else hits & ids
        return hits or set()

    def search(self, product_terms: list[str], *, must_claims: list[str] = (), exclude_allergens: list[str] = (),
               strict_allergens: bool = False, content_unit: str | None = None, limit: int = 40) -> dict:
        """Offline candidate search. Returns ranked candidates plus an explanation of each filter step."""
        steps = []
        matched: set[str] = set()
        for term in product_terms:
            matched |= self._match_term(term)
        steps.append({"step": "name match", "terms": list(product_terms), "remaining": len(matched)})

        # Category coherence: "milk" should mean milk drinks, not milk chocolate.
        cat_hits = defaultdict(int)
        for pid in matched:
            cat_hits[self.products[pid]["category"]] += 1
        cat_score = {c: (n / len(self.by_category[c])) * math.log1p(n) for c, n in cat_hits.items()}
        top = max(cat_score.values(), default=0)
        coherent = {c for c, s in cat_score.items() if s >= 0.25 * top}
        matched = {pid for pid in matched if self.products[pid]["category"] in coherent}
        steps.append({"step": "category coherence", "categories_kept": sorted(coherent), "remaining": len(matched)})

        for claim in must_claims:
            matched = {pid for pid in matched if claim_proof(self.products[pid], claim)}
            steps.append({"step": f"must be {CLAIM_NAMES.get(claim, claim)}", "remaining": len(matched)})
        for code in exclude_allergens:
            def safe(p):
                return code not in p["contains"] and not (strict_allergens and code in p["may_contain"])
            matched = {pid for pid in matched if safe(self.products[pid])}
            steps.append({"step": f"no {ALLERGENS.get(code, code)}" + (" (incl. may contain)" if strict_allergens else ""),
                          "remaining": len(matched)})
        if strict_allergens and exclude_allergens:
            # Strict mode (allergy, school, hospital): a product with no allergen declaration at all
            # is UNVERIFIED, not safe. Absence of data is not evidence of absence.
            unverified = {pid for pid in matched if not has_allergen_data(self.products[pid])}
            matched -= unverified
            steps.append({"step": f"with allergen data ({len(unverified)} excluded: no allergen data, needs a human check)",
                          "remaining": len(matched), "excluded_unverified": len(unverified)})
        if content_unit in ("L", "KG"):
            matched = {pid for pid in matched if self.products[pid]["content_unit"] == content_unit}
            steps.append({"step": f"sold by {'volume' if content_unit == 'L' else 'weight'}", "remaining": len(matched)})

        def rank(pid):
            p = self.products[pid]
            term_hits = sum(1 for t in product_terms if pid in self._match_term(t))
            return (-cat_score.get(p["category"], 0), -term_hits, p["reliability"] is None,
                    -(p["content_per_sales_unit"] or 0))
        ranked = sorted(matched, key=rank)[:limit]
        return {"candidates": [self.products[pid] for pid in ranked], "steps": steps,
                "catalog_size": len(self.products)}


def claim_proof(p: dict, claim: str) -> str | None:
    """How the product proves a required claim: 'declared', 'implied by <claim>', or None."""
    if claim in p["claims"]:
        return "declared"
    for stronger in CLAIM_IMPLIED_BY.get(claim, ()):
        if stronger in p["claims"]:
            return f"implied by the {CLAIM_NAMES.get(stronger, stronger)} claim"
    return None


def has_allergen_data(p: dict) -> bool:
    """True when the supplier declared anything about allergens (contains, may contain or free from)."""
    return bool(p["contains"] or p["may_contain"] or p["free_from"])


def allergen_evidence(p: dict, exclude_allergens: list[str]) -> list[str]:
    """Per excluded allergen: how strong the proof is that the product is safe."""
    out = []
    for code in exclude_allergens:
        name = ALLERGENS.get(code, code)
        if code in p["free_from"]:
            out.append(f"{name}: declared free from by the supplier")
        elif code in p["may_contain"]:
            out.append(f"{name}: MAY CONTAIN traces")
        elif has_allergen_data(p):
            out.append(f"{name}: not listed in the supplier's allergen declaration")
        else:
            out.append(f"{name}: no allergen data, unverified")
    return out


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog.from_files()
    return _catalog


def public_view(p: dict) -> dict:
    """What an external agent may see about a product (no internal fields)."""
    return {
        "product_id": p["id"], "name": p["name"], "name_fi": p["name_fi"], "brand": p["brand"],
        "sales_unit": p["sales_unit"], "content_per_sales_unit": p["content_per_sales_unit"],
        "content_unit": p["content_unit"], "origin": p["origin"],
        "claims": [CLAIM_NAMES.get(c, c) for c in p["claims"]],
        "labels": [LABEL_NAMES.get(l, l) for l in p["labels"]],
        "contains_allergens": [ALLERGENS.get(a, a) for a in p["contains"]],
        "may_contain_allergens": [ALLERGENS.get(a, a) for a in p["may_contain"]],
        "delivery_reliability": p["reliability"], "co2_kg_per_kg": p["co2_per_kg"],
    }
