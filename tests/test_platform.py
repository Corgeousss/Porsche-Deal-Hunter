"""Tests for the filter engine, taxonomy, saved searches, alerts, recon and the
dashboard API payloads.

Listings here are inserted into a throwaway temp database (never the production
DB) and are ordinary listing rows, not comps, so nothing synthetic can reach a
valuation. No network calls are made.
"""

from __future__ import annotations

import unittest

from porschehunter import (db, filters, taxonomy, searches, recon, dashboard)


def fresh_db():
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = db.connect(tmp.name)
    db.init_db(conn)
    return conn


def add_listing(conn, **kw):
    rec = {
        "source_key": kw.get("source_key", "dealer_jsonld"),
        "url": kw["url"],
        "title": kw.get("title"),
        "year": kw.get("year"),
        "generation": kw.get("generation"),
        "variant": kw.get("variant"),
        "body_style": kw.get("body_style"),
        "transmission": kw.get("transmission"),
        "drivetrain": kw.get("drivetrain"),
        "mileage": kw.get("mileage"),
        "vin": kw.get("vin"),
        "price": kw.get("price"),
        "seller_type": kw.get("seller_type", "dealer"),
        "seller_city": kw.get("seller_city"),
        "seller_state": kw.get("seller_state"),
        "listing_type": kw.get("listing_type", "fixed"),
    }
    lid, _ = db.upsert_listing(conn, rec)
    return lid


def seed(conn):
    # A spread of real-shaped cars across generations, prices and specs.
    add_listing(conn, url="https://d.invalid/a", title="2008 Porsche 911 Carrera S",
                year=2008, generation="997.1", variant="Carrera S", body_style="coupe",
                transmission="manual", mileage=48000, price=45000,
                seller_state="CA", vin="WP0AB29958S700001")
    add_listing(conn, url="https://d.invalid/b", title="2012 Porsche 911 Carrera",
                year=2012, generation="991.1", variant="Carrera", body_style="coupe",
                transmission="pdk", mileage=60000, price=62000,
                seller_state="TX", vin="WP0AB2A99CS700002")
    add_listing(conn, url="https://d.invalid/c", title="2016 Porsche 911 Turbo S",
                year=2016, generation="991.1", variant="Turbo S", body_style="coupe",
                transmission="pdk", mileage=20000, price=145000,
                seller_state="FL", vin="WP0AD2A96GS700003")
    add_listing(conn, url="https://d.invalid/d", title="2004 Porsche 911 Carrera",
                year=2004, generation="996.2", variant="Carrera", body_style="cabriolet",
                transmission=None, mileage=None, price=28000,  # unknown trans + mileage
                seller_state="OH", vin="WP0CA29984S700004")
    add_listing(conn, url="https://d.invalid/e", title="1997 Porsche 911 Carrera",
                year=1997, generation="993", variant="Carrera", body_style="coupe",
                transmission="manual", mileage=85000, price=90000,
                seller_state="NY", vin="WP0AA2993VS700005")
    # Same VIN as (a) but a different source listing -> duplicate.
    add_listing(conn, url="https://other.invalid/a2", title="2008 Porsche 911 Carrera S",
                year=2008, generation="997.1", variant="Carrera S", body_style="coupe",
                transmission="manual", mileage=48000, price=44000,
                seller_state="CA", vin="WP0AB29958S700001", source_key="manual")
    return conn


class TestTaxonomy(unittest.TestCase):
    def test_variants_for_generation(self):
        v = taxonomy.variants_for(["997.2"])
        self.assertIn("Carrera GTS", v)
        self.assertIn("Carrera S", v)
        self.assertNotIn("GT2", v)   # 997.2 had GT2 RS, not GT2

    def test_variant_exists_and_unknown_is_permissive(self):
        self.assertTrue(taxonomy.variant_exists("991.2", "GT2 RS"))
        self.assertFalse(taxonomy.variant_exists("964", "GT3"))  # no GT3 on a 964
        self.assertTrue(taxonomy.variant_exists(None, "GT3"))     # unknown -> not a contradiction

    def test_tiers_do_not_mix(self):
        self.assertEqual(taxonomy.variant_tier("Carrera S"), "s")
        self.assertEqual(taxonomy.variant_tier("Turbo S"), "turbo")
        self.assertIn("Carrera 4S", taxonomy.tier_variants("Carrera S"))
        self.assertNotIn("Turbo", taxonomy.tier_variants("Carrera S"))

    def test_family_expands(self):
        self.assertEqual(set(taxonomy.normalize_generation("997")), {"997.1", "997.2"})


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.conn = seed(fresh_db())
        self.rows = filters.enrich(self.conn, "OH")

    def test_default_under_100k(self):
        r = filters.apply_filters(self.rows, {"price_max": 100000})
        self.assertEqual({v["price"] for v in r}, {45000, 62000, 28000, 90000, 44000})

    def test_all_prices(self):
        self.assertEqual(len(filters.apply_filters(self.rows, {})), 6)

    def test_multiple_generations(self):
        r = filters.apply_filters(self.rows, {"generations": ["997", "991"]})
        self.assertEqual({v["generation"] for v in r}, {"997.1", "991.1"})

    def test_model_year_range(self):
        r = filters.apply_filters(self.rows, {"year_min": 2005, "year_max": 2012})
        self.assertEqual({v["year"] for v in r}, {2008, 2012})

    def test_manual_transmission_excludes_pdk_and_unknown(self):
        r = filters.apply_filters(self.rows, {"transmissions": ["manual"]})
        self.assertTrue(all(v["transmission"] == "manual" for v in r))
        # the unknown-transmission car (d) is excluded when a value is required
        self.assertNotIn("https://d.invalid/d", {v["url"] for v in r})

    def test_unknown_kept_when_explicitly_included(self):
        r = filters.apply_filters(self.rows, {"transmissions": ["manual", "unknown"]})
        self.assertIn("https://d.invalid/d", {v["url"] for v in r})

    def test_unknown_kept_when_no_filter(self):
        # No transmission filter -> the unknown-transmission car is retained.
        self.assertIn("https://d.invalid/d", {v["url"] for v in self.rows})

    def test_mileage_range(self):
        r = filters.apply_filters(self.rows, {"mileage_min": 40000, "mileage_max": 70000})
        self.assertEqual({v["mileage"] for v in r}, {48000, 60000})  # unknown-mileage excluded

    def test_combined_filters(self):
        # 2005-2012 Carrera S, manual, coupe, under 55k, under 70k mi.
        # Both source listings of the same VIN match and are preserved (deduping
        # flags them, it does not hide the individual listings).
        r = filters.apply_filters(self.rows, {
            "year_min": 2005, "year_max": 2012, "variants": ["Carrera S"],
            "transmissions": ["manual"], "body_styles": ["coupe"],
            "price_max": 55000, "mileage_max": 70000})
        self.assertEqual({v["url"] for v in r},
                         {"https://d.invalid/a", "https://other.invalid/a2"})
        self.assertTrue(all(v["is_duplicate_vin"] for v in r))

    def test_state_filter(self):
        r = filters.apply_filters(self.rows, {"states": ["CA"]})
        self.assertTrue(all(v["seller_state"] == "CA" for v in r))

    def test_duplicate_vin_detected(self):
        dup = [v for v in self.rows if v["is_duplicate_vin"]]
        self.assertEqual({v["vin"] for v in dup}, {"WP0AB29958S700001"})
        only = filters.apply_filters(self.rows, {"duplicates_only": True})
        self.assertEqual(len(only), 2)

    def test_deal_status_unvalued(self):
        # No comps in this DB, so every car is unvalued.
        r = filters.apply_filters(self.rows, {"deal_statuses": ["unvalued"]})
        self.assertEqual(len(r), 6)
        self.assertTrue(all(v["valuation_status"] != "ok" for v in r))

    def test_valuation_filter_excludes_unvalued(self):
        # net_profit_min must not match any unvalued car.
        r = filters.apply_filters(self.rows, {"net_profit_min": 8000})
        self.assertEqual(len(r), 0)

    def test_sort_price_asc(self):
        r = filters.sort_rows(self.rows, "price_asc")
        prices = [v["price"] for v in r if v["price"] is not None]
        self.assertEqual(prices, sorted(prices))

    def test_sort_mileage_asc_puts_unknown_last(self):
        r = filters.sort_rows(self.rows, "mileage_asc")
        self.assertIsNone(r[-1]["mileage"])


class TestSavedSearchesAndAlerts(unittest.TestCase):
    def setUp(self):
        self.conn = seed(fresh_db())

    def test_save_and_list(self):
        searches.save_search(self.conn, "Manual under 60k",
                             {"transmissions": ["manual"], "price_max": 60000},
                             "price_asc", {"new_match": True})
        rows = searches.list_searches(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Manual under 60k")

    def test_alerts_are_created_and_deduplicated(self):
        searches.save_search(self.conn, "All under 100k", {"price_max": 100000},
                             "newest", {"new_match": True})
        first = searches.run_alerts(self.conn, "OH")
        self.assertGreater(first["created"], 0)
        # Running again over unchanged data creates nothing new.
        second = searches.run_alerts(self.conn, "OH")
        self.assertEqual(second["created"], 0)

    def test_unvalued_never_triggers_profit_alert(self):
        searches.save_search(self.conn, "Profit", {"price_max": 200000},
                             "newest", {"meets_profit": True})
        res = searches.run_alerts(self.conn, "OH")
        kinds = {n["kind"] for n in searches.list_notifications(self.conn)}
        self.assertNotIn("meets_profit", kinds)


class TestRecon(unittest.TestCase):
    def setUp(self):
        self.conn = seed(fresh_db())

    def test_set_and_summary(self):
        recon.set_cost(self.conn, 1, "ppi", 350, "quote")
        recon.set_cost(self.conn, 1, "tires_brakes", 2200, "estimate")
        s = recon.summary(self.conn, 1)
        self.assertEqual(s["total"], 2550.0)
        self.assertEqual(s["weakest_basis"], "estimate")

    def test_rejects_unknown_category(self):
        with self.assertRaises(recon.ReconError):
            recon.set_cost(self.conn, 1, "nonsense", 100)

    def test_recon_feeds_cost_estimate(self):
        recon.set_cost(self.conn, 1, "engine_mechanical", 5000, "quote")
        rows = filters.enrich(self.conn, "OH")
        car = next(v for v in rows if v["id"] == 1)
        self.assertEqual(car["recon_estimate"], 5000.0)


class TestDashboardPayloads(unittest.TestCase):
    def setUp(self):
        self.conn = seed(fresh_db())

    def test_options_payload(self):
        op = dashboard.options_payload(self.conn, "OH")
        self.assertEqual(op["target_net_profit"], 8000.0)
        self.assertIn("CA", op["facets"]["states"])
        self.assertEqual(op["counts"]["total_active"], 6)
        self.assertEqual(op["counts"]["unique_vins"], 5)  # 6 listings, one dup VIN

    def test_inventory_payload_summary(self):
        inv = dashboard.inventory_payload(self.conn, {"price_max": 100000}, "price_asc", "OH")
        self.assertEqual(inv["summary"]["under_50k"], 3)   # 45000, 28000, 44000
        self.assertEqual(inv["summary"]["under_100k"], 5)
        self.assertTrue(inv["count"] >= 5)

    def test_render_is_html(self):
        html = dashboard.render(self.conn, "OH")
        self.assertIn("<!doctype", html.lower())
        self.assertIn("911 Deal Hunter", html)


if __name__ == "__main__":
    unittest.main()
