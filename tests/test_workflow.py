"""Tests for the live-validation workflow, acceptance report, provider adapters
and the mechanic sheet.

Every vehicle and sale here is SYNTHETIC and flagged as such.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

from porschehunter import acceptance, comps, db, http_util, sheets, validate
from porschehunter.sources import classic_com, jsonld, marketcheck
from test_core import TODAY, add_synthetic_comp, fresh_db
from porschehunter.sources import manual


class TestValidationWorkflow(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def test_offline_run_marks_nothing_live_verified(self):
        res = validate.run(self.conn, destination_state="OH", skip_network=True)
        steps = {s["step"]: s for s in res["steps"]}
        self.assertEqual(steps["vpic_live_call"]["status"], "skip")
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM sources WHERE live_verified_at IS NOT NULL"
        ).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_network_steps_never_report_pass_when_offline(self):
        res = validate.run(self.conn, destination_state="OH", skip_network=True)
        network_steps = {"vpic_live_call", "ingest_real_listing",
                         "duplicate_handling", "source_permission_robots"}
        for s in res["steps"]:
            if s["step"] in network_steps:
                self.assertNotEqual(s["status"], "pass", s["step"])

    def test_synthetic_data_aborts_the_run(self):
        add_synthetic_comp(self.conn, 1, 60000, 50000)
        res = validate.run(self.conn, destination_state="OH", skip_network=True)
        steps = [s["step"] for s in res["steps"]]
        self.assertEqual(steps, ["synthetic_data_absent"])
        self.assertEqual(res["counts"]["fail"], 1)

    def test_allow_dirty_continues_but_still_fails_that_step(self):
        add_synthetic_comp(self.conn, 1, 60000, 50000)
        res = validate.run(self.conn, destination_state="OH", skip_network=True,
                           allow_dirty=True)
        first = res["steps"][0]
        self.assertEqual(first["status"], "fail")
        self.assertGreater(len(res["steps"]), 1)

    def test_steps_are_persisted_with_the_run(self):
        res = validate.run(self.conn, destination_state="OH", skip_network=True)
        rows = self.conn.execute(
            "SELECT * FROM validation_steps WHERE run_id=?", (res["run_id"],)).fetchall()
        self.assertEqual(len(rows), len(res["steps"]))
        run = self.conn.execute(
            "SELECT * FROM validation_runs WHERE id=?", (res["run_id"],)).fetchone()
        self.assertIsNotNone(run["finished_at"])

    def test_report_states_nothing_is_live_verified(self):
        res = validate.run(self.conn, destination_state="OH", skip_network=True)
        text = validate.format_report(res, self.conn)
        self.assertIn("Live-verified sources: NONE", text)

    def test_mark_live_verified_is_recorded(self):
        validate.mark_live_verified(self.conn, "nhtsa_vpic", "proof note")
        row = self.conn.execute(
            "SELECT * FROM sources WHERE key='nhtsa_vpic'").fetchone()
        self.assertIsNotNone(row["live_verified_at"])
        self.assertEqual(row["live_verified_note"], "proof note")


class TestAcceptanceReport(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.lid, _, _ = manual.add_listing(
            self.conn, "https://example.invalid/SYNTHETIC-TEST/subject",
            title="2008 Porsche 911 Carrera S Coupe", mileage=51000, price=39500,
            seller_type="private", decode_vin=False)
        self.conn.execute("UPDATE listings SET seller_state='CA' WHERE id=?", (self.lid,))
        self.conn.commit()

    def test_missing_listing(self):
        text, passed = acceptance.report(self.conn, 999, "OH")
        self.assertFalse(passed)
        self.assertIn("INSUFFICIENT DATA", text)

    def test_no_comps_gives_insufficient_data_and_the_gap(self):
        text, passed = acceptance.report(self.conn, self.lid, "OH")
        self.assertFalse(passed)
        self.assertIn("RESULT: INSUFFICIENT DATA", text)
        self.assertIn("WHAT IS NEEDED", text)
        self.assertIn("datasupport@classic.com", text)
        self.assertNotIn("MAXIMUM OFFER", text)

    def test_synthetic_database_is_refused(self):
        add_synthetic_comp(self.conn, 1, 60000, 50000)
        text, passed = acceptance.report(self.conn, self.lid, "OH")
        self.assertFalse(passed)
        self.assertIn("ABORTED", text)

    def test_unusable_comps_do_not_unlock_a_number(self):
        """Enough comps, but they are asking prices -- must still refuse."""
        conn = fresh_db()
        lid, _, _ = manual.add_listing(
            conn, "https://example.invalid/SYNTHETIC-TEST/s2",
            title="2008 Porsche 911 Carrera S", mileage=51000, price=39500,
            decode_vin=False)
        for i in range(8):
            comps.add_comp(
                conn, generation="997.1", variant="Carrera S", year=2008,
                mileage=50000, sale_price=60000,
                sale_date=(TODAY - dt.timedelta(days=30)).isoformat(),
                venue="dealer", source_url=f"https://example.invalid/ASK/{i}",
                price_basis="last_asking", permission_basis="licensed_api")
        text, passed = acceptance.report(conn, lid, "OH")
        self.assertFalse(passed)
        self.assertIn("INSUFFICIENT DATA", text)
        self.assertIn("last_asking", text)

    def test_full_report_when_verified_comps_exist(self):
        """The happy path, proven on flagged synthetic data. Real runs refuse
        synthetic rows -- this exercises the report body only."""
        conn = fresh_db()
        lid, _, _ = manual.add_listing(
            conn, "https://example.invalid/SYNTHETIC-TEST/s3",
            title="2008 Porsche 911 Carrera S", mileage=50000, price=39500,
            seller_type="private", decode_vin=False)
        conn.execute("UPDATE listings SET seller_state='CA' WHERE id=?", (lid,))
        for i in range(6):
            add_synthetic_comp(conn, i, 60000, 50000)
        conn.execute("UPDATE comps SET is_synthetic=0")
        conn.commit()
        text, passed = acceptance.report(conn, lid, "OH")
        self.assertTrue(passed)
        for token in ("MAXIMUM OFFER", "TOTAL INVESTMENT", "PROJECTED NET PROFIT",
                      "COMPARABLE TRANSACTIONS", "verified_transaction"):
            self.assertIn(token, text)

    def test_provisional_profit_is_never_called_confirmed(self):
        conn = fresh_db()
        lid, _, _ = manual.add_listing(
            conn, "https://example.invalid/SYNTHETIC-TEST/s4",
            title="2008 Porsche 911 Carrera S", mileage=50000, price=20000,
            decode_vin=False)
        for i in range(6):
            add_synthetic_comp(conn, i, 60000, 50000)
        conn.execute("UPDATE comps SET is_synthetic=0")
        conn.commit()
        text, _ = acceptance.report(conn, lid, "OH")
        self.assertIn("PROVISIONAL -- NOT CONFIRMED PROFIT", text)


class TestClassicComAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "classic.json"

    def test_disabled_without_config(self):
        with self.assertRaises(classic_com.NotLicensed):
            classic_com.load_config(self.tmp)

    def test_template_is_not_confirmed_by_default(self):
        classic_com.write_template(self.tmp)
        cfg = json.loads(self.tmp.read_text())
        self.assertFalse(cfg["confirmed"])
        with self.assertRaises(classic_com.NotLicensed):
            classic_com.load_config(self.tmp)

    def test_confirmed_but_incomplete_is_rejected(self):
        classic_com.write_template(self.tmp)
        cfg = json.loads(self.tmp.read_text())
        cfg["confirmed"] = True
        self.tmp.write_text(json.dumps(cfg))
        with self.assertRaises(classic_com.NotLicensed) as ctx:
            classic_com.load_config(self.tmp)
        self.assertIn("Missing", str(ctx.exception))

    def test_preflight_never_raises(self):
        pf = classic_com.preflight(self.tmp)
        self.assertFalse(pf["ready"])
        self.assertTrue(pf["blockers"])

    def test_ingest_skips_cleanly_when_unlicensed(self):
        res = classic_com.ingest_sales_history(None, {}, config_path=self.tmp)
        self.assertEqual(res.status, "skipped")
        self.assertEqual(res.seen, 0)

    # --- the mapping decision that matters ---------------------------------
    def _cfg(self, **over):
        cfg = {
            "response": {
                "fields": {"sale_price": "price", "sale_date": "date", "url": "url"},
                "sold_flag_field": "status",
                "sold_flag_true_values": ["sold"],
                "asking_price_field": "last_asking",
            }
        }
        cfg["response"].update(over)
        return cfg

    def test_sold_record_is_a_verified_transaction(self):
        basis, price = classic_com.classify_price_basis(
            {"status": "sold", "price": "$62,500"}, self._cfg())
        self.assertEqual(basis, "verified_transaction")
        self.assertEqual(price, 62500.0)

    def test_unsold_record_is_last_asking_not_a_sale(self):
        basis, price = classic_com.classify_price_basis(
            {"status": "removed", "last_asking": 59000}, self._cfg())
        self.assertEqual(basis, "last_asking")
        self.assertEqual(price, 59000.0)

    def test_fails_closed_without_a_sold_flag(self):
        """If we cannot prove a completed sale, it is never verified."""
        basis, _ = classic_com.classify_price_basis(
            {"price": 60000}, self._cfg(sold_flag_field=""))
        self.assertNotEqual(basis, "verified_transaction")

    def test_licensed_records_carry_permission_basis(self):
        cfg = self._cfg()
        cfg["response"]["items_path"] = "data"
        cfg["response"]["fields"].update({"year": "year", "model": "model"})
        rec = classic_com.to_comp(
            {"status": "sold", "price": 62500, "date": "2026-07-14",
             "url": "https://www.classic.com/veh/x", "year": 2008,
             "model": "911 Carrera S"}, cfg)
        self.assertEqual(rec["permission_basis"], "licensed_api")
        self.assertEqual(rec["price_basis"], "verified_transaction")
        self.assertEqual(rec["generation"], "997.1")


class TestMarketCheckAdapter(unittest.TestCase):
    """Endpoint paths are operator-confirmed against MarketCheck's docs.
    The RESPONSE SCHEMA is not, so it is externalised and gated."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "fields.json"
        self._old_key = os.environ.get("MARKETCHECK_API_KEY")
        os.environ.pop("MARKETCHECK_API_KEY", None)

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("MARKETCHECK_API_KEY", None)
        else:
            os.environ["MARKETCHECK_API_KEY"] = self._old_key

    def test_documented_host(self):
        self.assertIn("api.marketcheck.com", marketcheck.BASE_URL)

    def test_three_confirmed_endpoints(self):
        self.assertEqual(marketcheck.ENDPOINTS["active"], "search/car/active")
        self.assertEqual(marketcheck.ENDPOINTS["fsbo"], "search/car/fsbo/active")
        self.assertEqual(marketcheck.ENDPOINTS["auction"], "search/car/auction/active")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            marketcheck.endpoint_for("past")

    def test_no_past_inventory_endpoint(self):
        """Past inventory is removals, not sales. It must not be reachable."""
        self.assertNotIn("past", marketcheck.ENDPOINTS)

    def test_ingest_skips_without_key(self):
        res = marketcheck.ingest(None, fieldmap_path=self.tmp)
        self.assertEqual(res.status, "skipped")
        self.assertIn("MARKETCHECK_API_KEY", res.message)

    def test_ingest_blocked_while_schema_unconfirmed(self):
        os.environ["MARKETCHECK_API_KEY"] = "dummy-key-for-test"
        res = marketcheck.ingest(None, fieldmap_path=self.tmp)
        self.assertEqual(res.status, "skipped")
        self.assertIn("not confirmed", res.message)

    def test_template_ships_unconfirmed(self):
        marketcheck.write_fieldmap_template(self.tmp)
        cfg = json.loads(self.tmp.read_text())
        self.assertFalse(cfg["confirmed"])
        with self.assertRaises(marketcheck.SchemaNotConfirmed):
            marketcheck.load_fieldmap(self.tmp)

    def test_dig_follows_dotted_paths(self):
        item = {"build": {"year": 2008, "trim": "Carrera S"}, "miles": 51000}
        self.assertEqual(marketcheck.dig(item, "build.year"), 2008)
        self.assertEqual(marketcheck.dig(item, "miles"), 51000)
        self.assertIsNone(marketcheck.dig(item, "build.missing"))
        self.assertIsNone(marketcheck.dig(item, "nope.nope"))

    def test_fallback_paths_are_used(self):
        cfg = {"fields": {"url": "vdp_url"}, "fallbacks": {"url": ["source_url"]}}
        self.assertEqual(
            marketcheck._resolve({"source_url": "https://x.test/a"}, cfg, "url"),
            "https://x.test/a")

    def test_kind_determines_seller_and_listing_type(self):
        cfg = marketcheck.FIELDMAP_TEMPLATE
        item = {"vdp_url": "https://x.test/1", "heading": "2008 Porsche 911 Carrera S",
                "build": {"year": 2008, "make": "Porsche", "model": "911"},
                "miles": 51000, "price": 39500}
        fsbo = marketcheck.to_record(item, cfg, "fsbo")["record"]
        auction = marketcheck.to_record(item, cfg, "auction")["record"]
        dealer = marketcheck.to_record(item, cfg, "active")["record"]
        self.assertEqual(fsbo["seller_type"], "private")
        self.assertEqual(fsbo["listing_type"], "fixed")
        self.assertEqual(auction["seller_type"], "auction_house")
        self.assertEqual(auction["listing_type"], "auction")
        self.assertEqual(dealer["seller_type"], "dealer")

    def test_mapping_produces_expected_record(self):
        cfg = marketcheck.FIELDMAP_TEMPLATE
        item = {"id": "abc", "vdp_url": "https://x.test/1",
                "heading": "2008 Porsche 911 Carrera S Coupe",
                "build": {"year": 2008, "make": "Porsche", "model": "911",
                          "trim": "Carrera S", "transmission": "6-Speed Manual"},
                "miles": "51,000", "price": "39500", "vin": "WP0AB29958S781234",
                "dealer": {"name": "X Motors", "city": "Fresno", "state": "CA"}}
        rec = marketcheck.to_record(item, cfg, "active")["record"]
        self.assertEqual(rec["year"], 2008)
        self.assertEqual(rec["generation"], "997.1")
        self.assertEqual(rec["variant"], "Carrera S")
        self.assertEqual(rec["transmission"], "manual")
        self.assertEqual(rec["mileage"], 51000)
        self.assertEqual(rec["price"], 39500.0)
        self.assertEqual(rec["seller_state"], "CA")

    def test_non_porsche_is_filtered(self):
        cfg = marketcheck.FIELDMAP_TEMPLATE
        item = {"vdp_url": "https://x.test/2", "heading": "2019 Honda Civic",
                "build": {"year": 2019, "make": "Honda", "model": "Civic"}}
        rec = marketcheck.to_record(item, cfg, "active")["record"]
        self.assertFalse(marketcheck.is_911(rec, item, cfg))


class TestDealerSitemapDiscovery(unittest.TestCase):
    SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://d.test/inventory/used-2008-porsche-911-carrera-s-1</loc></url>
      <url><loc>https://d.test/blog/porsche-911-history</loc></url>
      <url><loc>https://d.test/service/schedule</loc></url>
      <url><loc>https://d.test/vehicle/2016-porsche-911-gts-9</loc></url>
    </urlset>"""

    INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://d.test/sitemap-inventory.xml</loc></sitemap>
      <sitemap><loc>https://d.test/sitemap-blog.xml</loc></sitemap>
    </sitemapindex>"""

    def test_parses_urlset(self):
        pages, nested = jsonld.parse_sitemap(self.SITEMAP)
        self.assertEqual(len(pages), 4)
        self.assertEqual(nested, [])

    def test_parses_sitemap_index(self):
        pages, nested = jsonld.parse_sitemap(self.INDEX)
        self.assertEqual(pages, [])
        self.assertEqual(len(nested), 2)

    def test_filters_to_vehicle_pages(self):
        pages, _ = jsonld.parse_sitemap(self.SITEMAP)
        keep = [u for u in pages if jsonld.looks_like_vehicle_url(u)]
        self.assertEqual(len(keep), 2)
        self.assertTrue(all("/inventory/" in u or "/vehicle/" in u for u in keep))

    def test_excludes_blog_and_service(self):
        self.assertFalse(jsonld.looks_like_vehicle_url("https://d.test/blog/porsche-911"))
        self.assertFalse(jsonld.looks_like_vehicle_url("https://d.test/service/porsche"))

    def test_malformed_sitemap_returns_empty(self):
        self.assertEqual(jsonld.parse_sitemap(b"not xml at all"), ([], []))

    def test_discovery_refuses_domains_not_on_the_allowlist(self):
        with self.assertRaises(http_util.NotAllowed):
            jsonld.discover_urls("not-allowlisted.test")

    def test_ingest_domain_reports_the_block_rather_than_crashing(self):
        res = jsonld.ingest_domain(fresh_db(), "not-allowlisted.test")
        self.assertEqual(res.status, "error")
        self.assertIn("allowed_domains", res.message)


class TestMechanicSheet(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.dir = Path(tempfile.mkdtemp())

    def test_csv_has_a_row_per_cost_line(self):
        path = sheets.write_csv(self.conn, self.dir / "sheet.csv")
        text = path.read_text()
        for key in ("ppi_cost", "repairs_997", "transport_per_mile", "sale_fee_pct",
                    "carrying_cost_per_day", "purchase_tax_pct", "dealer_doc_fee"):
            self.assertIn(key, text)

    def test_placeholders_are_labelled_unverified(self):
        rows = sheets.build_rows(self.conn)
        self.assertTrue(any(r["status"].startswith("UNVERIFIED") for r in rows))

    def test_verified_values_show_as_verified(self):
        db.set_assumption(self.conn, "ppi_cost", 500, basis="real quote", verified=True)
        rows = {r["key"]: r for r in sheets.build_rows(self.conn)}
        self.assertEqual(rows["ppi_cost"]["status"], "VERIFIED")

    def test_html_states_the_target_and_the_placeholder_warning(self):
        path = sheets.write_html(self.conn, self.dir / "sheet.html")
        text = path.read_text()
        self.assertIn("$8,000", text)
        self.assertIn("placeholders", text)
        self.assertIn("should not be", text)


if __name__ == "__main__":
    unittest.main()
