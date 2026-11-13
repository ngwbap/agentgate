"""Synthetic catalog + fake Valio API for offline tests (no real data, no network)."""
from __future__ import annotations

from app.catalog import Catalog, build_product


def _raw(pid, name_en, name_fi, category, sales_unit, per_sales, volume_l=None, net_g=None, claims=(), contains=(),
         labels=(), base_unit="ST"):
    each = {"synkkaPackagingType": "BASE_UNIT_OR_EACH"}
    if volume_l:
        each["volume"] = {"unit": "LTR", "value": volume_l}
    if net_g:
        each["netWeight"] = {"unit": "GRM", "value": net_g}
    return {
        "id": pid, "salesUnitGtin": f"64{pid}", "salesUnit": sales_unit, "baseUnit": base_unit, "category": category,
        "allowedLotSize": 0.01, "deleted": False, "temperatureCondition": 4, "vendorName": "TEST VENDOR",
        "countryOfOrigin": "fi",
        "units": [{"unitId": sales_unit, "sizeInBaseUnits": per_sales}, {"unitId": base_unit, "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"value": name_en, "language": "en"}, {"value": name_fi, "language": "fi"}],
            "brand": "Testbrand", "lastUpdated": "2026-06-01T10:00:00+0300",
            "unitConversions": [each],
            "classifications": [
                {"type": "ENUM", "name": "nutritionalClaim", "values": [{"id": c} for c in claims]},
                {"type": "ENUM", "name": "allergen", "values": [{"id": a, "unit": "CONTAINS"} for a in contains]},
                {"type": "ENUM", "name": "nonAllergen", "values": []},
                {"type": "ENUM", "name": "packagingMarkedLabel", "values": [{"id": l} for l in labels]},
            ],
        },
    }


RAW = [
    _raw("900001", "Lactose-free milk drink 1 l", "Laktoositon maitojuoma 1 l", "10400", "KI", 10, volume_l=1,
         claims=["FREE_FROM_LACTOSE"], contains=["AM"], labels=["GOODS_FROM_FINLAND_BLUE_SWAN"]),
    _raw("900002", "Semi skimmed milk drink 10 l novobox lactose free", "Kevytmaitojuoma 10 l laktoositon", "10400",
         "KI", 2, volume_l=10, claims=["FREE_FROM_LACTOSE"], contains=["AM"]),
    _raw("900006", "Skimmed sour milk 1 l lactose free", "Rasvaton piimä 1 l laktoositon", "10400", "ST", 1,
         volume_l=1, claims=["FREE_FROM_LACTOSE"], contains=["AM"]),
    _raw("900007", "Whole milk 1 l", "Täysmaito 1 l", "10400", "ST", 1, volume_l=1, contains=["AM"]),
    _raw("900003", "Milk chocolate bar 100 g lactose free", "Maitosuklaa 100 g laktoositon", "22000", "ST", 1,
         net_g=100, claims=["FREE_FROM_LACTOSE"], contains=["AM"]),
    _raw("900008", "Dark chocolate 70% 100 g", "Tumma suklaa 100 g", "22000", "ST", 1, net_g=100),
    _raw("900009", "Hazelnut chocolate 200 g", "Pähkinäsuklaa 200 g", "22000", "ST", 1, net_g=200, contains=["SH"]),
    _raw("900010", "Chocolate praline box 250 g", "Konvehtirasia 250 g", "22000", "ST", 1, net_g=250),
    _raw("900004", "Oat drink barista 1 l", "Kaurajuoma barista 1 l", "10402", "ST", 1, volume_l=1,
         claims=["VEGAN", "FREE_FROM_LACTOSE"]),
    _raw("900005", "Red wine 0,75 l", "Punaviini 0,75 l", "60000", "BOT", 1, volume_l=0.75),
    _raw("900011", "Rice wine vinegar 1 l", "Riisiviinietikka 1 l", "30000", "ST", 1, volume_l=1),
]
RELIABILITY = {"900001": 0.99, "900002": 1.0, "900004": 0.97}
CO2 = {"900001": 1.1, "900002": 1.1, "900006": 1.2, "900007": 1.3, "900004": 0.43}


def make_catalog() -> Catalog:
    return Catalog([build_product(p, RELIABILITY, CO2) for p in RAW])


# per product: (logistic model, available amount in sales units, price per sales unit, comparison price, unit, tax %)
LIVE = {
    "900001": ("Stock", 60, 13.0, 1.30, "L", 13.5),
    "900002": ("Terminal", 999999, 31.0, 1.55, "L", 13.5),
    "900006": ("Stock", 0, 1.9, 1.90, "L", 13.5),
    "900007": ("Stock", 500, 1.2, 1.20, "L", 13.5),
    "900004": ("Stock", 100, 1.79, 1.79, "L", 13.5),
    "900005": ("Stock", 50, 9.5, 12.67, "L", 25.5),
    "900011": ("Stock", 50, 4.0, 4.0, "L", 13.5),
    "900003": ("Stock", 100, 1.5, 15.0, "KG", 13.5),
}
NOW = "2026-11-14T00:53:08+0200"


class FakeValio:
    available = True

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.stats = {"calls": 0, "products_queried": 0, "cache_hits": 0}

    def _check(self, ids):
        from app.valio_api import ValioApiError
        if self.fail:
            raise ValioApiError("Valio API unreachable (test)")
        self.stats["calls"] += 1
        self.stats["products_queried"] += len(ids)

    def availability(self, ids, requested_date):
        self._check(ids)
        out = {}
        for pid in ids:
            if pid not in LIVE:
                continue
            model, amount, *_ = LIVE[pid]
            closing = "2026-11-12T15:00:00+0200" if model == "Terminal" else "2026-11-15T17:00:00+0200"
            out[pid] = {"productId": pid, "closingTime": closing, "salesBlock": False, "logisticModel": model,
                        "scheduleLines": [{"salesBlock": False, "availableAmount": float(amount),
                                           "availableDate": requested_date, "salesUnit": "KI"}],
                        "_meta": {"timeNow": NOW, "requestedDate": requested_date}}
        return out

    def price(self, ids, requested_date):
        self._check(ids)
        out = {}
        for pid in ids:
            if pid not in LIVE:
                continue
            _, _, price, cmp_price, unit, tax = LIVE[pid]
            out[pid] = {"productId": pid, "quantity": 1.0, "error": False, "netPriceFound": False,
                        "catalogPrice": {"value": price, "validFrom": "2026-09-01T00:00:00+0300", "validTo": "9999-12-31"},
                        "comparisonPrice": {"value": cmp_price}, "comparisonUnit": unit,
                        "tax": {"taxRate": tax, "taxValue": 0},
                        "_meta": {"pricingDate": requested_date + "T00:00:00+0200"}}
        return out
