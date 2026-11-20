from decimal import Decimal

from data import load


def test_clean_locations_keeps_only_country_rows(monkeypatch):
    rows = {
        "locations.jsonl.gz": [
            {
                "id": 1,
                "osm_address_country_code": "FR",
                "osm_brand": "Carrefour Market",
                "osm_name": "Carrefour Market Paris",
                "osm_address_city": "Paris",
                "osm_address_postcode": "75001",
            },
            {
                "id": 2,
                "osm_address_country_code": "BE",
                "osm_brand": "Delhaize",
                "osm_name": "Delhaize Bruxelles",
            },
        ]
    }
    monkeypatch.setattr(load, "read_jsonl", lambda name: rows[name])

    assert load.clean_locations() == {
        1: (1, "Carrefour", "Carrefour Market", "Carrefour Market Paris", "Paris", "75001", "FR")
    }


def test_clean_products_applies_name_size_and_category_rules(monkeypatch):
    rows = {
        "products.jsonl.gz": [
            {
                "code": "valid-g",
                "product_name": "  Butter  ",
                "brands": "  Brand  ",
                "categories_tags": ["en:dairies", "fr:butters"],
                "product_quantity": 250,
                "product_quantity_unit": "g",
            },
            {
                "code": "valid-ml",
                "product_name": "Milk",
                "categories_tags": ["en:milks"],
                "product_quantity": 1000,
                "product_quantity_unit": "ml",
            },
            {
                "code": "invalid-size",
                "product_name": "Oil",
                "categories_tags": [],
                "product_quantity": 750,
                "product_quantity_unit": "cl",
            },
            {
                "code": "invalid-quantity",
                "product_name": "Salt",
                "product_quantity": 0,
                "product_quantity_unit": "g",
            },
            {"code": "no-name", "product_name": "  "},
        ]
    }
    monkeypatch.setattr(load, "read_jsonl", lambda name: rows[name])

    assert load.clean_products() == {
        "valid-g": (
            "valid-g",
            "Butter",
            "Brand",
            "butters",
            ["en:dairies", "fr:butters"],
            250,
            "g",
        ),
        "valid-ml": ("valid-ml", "Milk", None, "milks", ["en:milks"], 1000, "ml"),
        "invalid-size": ("invalid-size", "Oil", None, None, [], None, None),
        "invalid-quantity": ("invalid-quantity", "Salt", None, None, [], None, None),
    }


def test_clean_prices_counts_rules_and_keeps_highest_duplicate_id(monkeypatch):
    locations = {1: object()}
    products = {"kept": object()}
    rows = {
        "prices.jsonl.gz": [
            {"id": 1, "location_id": 99, "type": "PRODUCT"},
            {"id": 2, "location_id": 1, "type": "CATEGORY"},
            {"id": 3, "location_id": 1, "type": "PRODUCT", "duplicate_of": 8},
            {"id": 4, "location_id": 1, "type": "PRODUCT", "currency": "USD"},
            {"id": 5, "location_id": 1, "type": "PRODUCT", "currency": "EUR", "date": ""},
            {
                "id": 6,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-01-01",
                "price": "bad",
            },
            {
                "id": 7,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-01-02",
                "price": "0",
            },
            {
                "id": 8,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-01-03",
                "price": "201",
            },
            {
                "id": 9,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-01-04",
                "price": "2",
                "product_code": "missing",
            },
            {
                "id": 50,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-02-01",
                "price": "3.50",
                "product_code": "kept",
            },
            {
                "id": 49,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-02-01",
                "price": "3.00",
                "product_code": "kept",
            },
            {
                "id": 40,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-02-02",
                "price": "4.00",
                "product_code": "kept",
            },
            {
                "id": 41,
                "location_id": 1,
                "type": "PRODUCT",
                "currency": "EUR",
                "date": "2026-02-02",
                "price": "4.25",
                "product_code": "kept",
            },
        ]
    }
    monkeypatch.setattr(load, "read_jsonl", lambda name: rows[name])

    cleaned, dropped = load.clean_prices(locations, products)

    assert [row[0] for row in cleaned] == [50, 41]
    assert [row[3] for row in cleaned] == [Decimal("3.50"), Decimal("4.25")]
    assert dropped == {
        "0_total_in_country": 12,
        "category_price_no_barcode": 1,
        "flagged_duplicate_by_source": 1,
        "not_eur": 1,
        "no_date": 1,
        "price_not_a_number": 1,
        "price_out_of_range": 2,
        "product_without_name": 1,
        "same_product_store_day": 2,
    }
