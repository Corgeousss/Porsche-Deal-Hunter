"""Unit tests. Run: python3 -m unittest discover -s tests -v

IMPORTANT: every vehicle and sale in this file is SYNTHETIC. It exists only to
exercise arithmetic. Each comp is inserted with is_synthetic=1 and a
non-resolvable example.invalid URL, and the production code paths exclude
synthetic rows, so none of it can leak into a real valuation.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from porschehunter import comps, db, deal, generations, geo, valuation, vin
from porschehunter.sources import jsonld, manual, rss

TODAY = dt.date(2026, 9, 17)


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = db.connect(tmp.name)
    db.init_db(conn)
    return conn


def add_synthetic_comp(conn, n, price, mileage, year=2008, variant="Carrera S",
                       days_ago=30, venue="dealer",
                       price_basis="verified_transaction",
                       permission_basis="own_transaction"):
    return comps.add_comp(
        conn, generation="997.1", variant=variant, year=year, body_style="coupe",
        transmission="manual", mileage=mileage, sale_price=price,
        sale_date=(TODAY - dt.timedelta(days=days_ago)).isoformat(),
        venue=venue, source_url=f"https://example.invalid/SYNTHETIC-TEST/{n}",
        price_basis=price_basis, permission_basis=permission_basis,
        is_synthetic=True, condition_note="SYNTHETIC TEST ROW")


class TestVin(unittest.TestCase):
    def test_check_digit_math(self):
        # Constructed so the check digit is correct by construction.
        base = list("WP0AB2A9_CS721234")
        for d in "0123456789X":
            candidate = "".join(base).replace("_", d)
            if vin.is_valid_check_digit(candidate):
                self.assertTrue(vin.offline_summary(candidate)["check_digit_ok"])
                break
        else:
            self.fail("no valid check digit found for the constructed pattern")

    def test_rejects_bad_format(self):
        self.assertIsNone(vin.normalize("WP0AB2A99CS72123"))   # 16 chars
        self.assertIsNone(vin.normalize("WP0AB2A99CS72123I"))  # I is not allowed
        self.assertEqual(vin.normalize("wp0ab2a99cs721234"), "WP0AB2A99CS721234")

    def test_model_year_and_generation(self):
        s = vin.offline_summary("WP0AB2A99CS721234")
        self.assertEqual(s["model_year"], 2012)
        self.assertTrue(s["generation_ambiguous"])
        self.assertTrue(s["is_porsche_wmi"])

    def test_non_porsche_wmi_warns(self):
        s = vin.offline_summary("1HGCM82633A004352")
        self.assertFalse(s["is_porsche_wmi"])
        self.assertTrue(any("not a Porsche" in w for w in s["warnings"]))


class TestGenerations(unittest.TestCase):
    def test_year_mapping(self):
        self.assertEqual(generations.from_year(2003)[0], "996.2")
        self.assertEqual(generations.from_year(2007)[0], "997.1")
        self.assertEqual(generations.from_year(2018)[0], "991.2")
        self.assertTrue(generations.from_year(2012)[1])  # ambiguous
        self.assertEqual(generations.from_year(None), (None, False))

    def test_expand_family(self):
        self.assertEqual(generations.expand("997"), ["997.1", "997.2"])
        self.assertEqual(generations.expand("991.2"), ["991.2"])
        self.assertEqual(generations.family("991.1"), "991")

    def test_variant_specificity(self):
        self.assertEqual(generations.normalize_variant("2011 911 GT3 RS"), "GT3 RS")
        self.assertEqual(generations.normalize_variant("2011 911 Turbo S"), "Turbo S")
        self.assertEqual(generations.normalize_variant("Carrera 4S Cabriolet"), "Carrera 4S")

    def test_transmission(self):
        self.assertEqual(generations.normalize_transmission("7-Speed PDK"), "pdk")
        self.assertEqual(generations.normalize_transmission("Tiptronic S"), "tiptronic")
        self.assertEqual(generations.normalize_transmission("6-speed manual"), "manual")


class TestCompGuards(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def test_requires_real_url(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=50000,
                           sale_date="2026-01-01", source_url="i-heard-it-somewhere")

    def test_rejects_future_sale(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=50000,
                           sale_date="2099-01-01", source_url="https://x.test/a")

    def test_rejects_unknown_generation(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="928", sale_price=50000,
                           sale_date="2026-01-01", source_url="https://x.test/a")

    def test_rejects_nonpositive_price(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=0,
                           sale_date="2026-01-01", source_url="https://x.test/a")

    def test_synthetic_comps_are_excluded_from_real_valuations(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        lid, _, _ = manual.add_listing(
            self.conn, "https://example.invalid/SYNTHETIC-TEST/subject",
            title="2008 Porsche 911 Carrera S", mileage=50000, decode_vin=False)
        row = self.conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        real = valuation.value_listing(self.conn, row, today=TODAY, store=False)
        self.assertEqual(real["status"], "insufficient_comps")
        self.assertEqual(real["n_comps"], 0)
        with_synth = valuation.value_listing(self.conn, row, today=TODAY, store=False,
                                             include_synthetic=True)
        self.assertEqual(with_synth["status"], "ok")


class TestProvenanceGates(unittest.TestCase):
    """A figure has to earn its way into a valuation."""

    def setUp(self):
        self.conn = fresh_db()
        self.subject = {"id": None, "generation": "997.1", "year": 2008,
                        "mileage": 50000, "variant": "Carrera S",
                        "body_style": "coupe", "price": 40000}

    def _value(self):
        return valuation.value_listing(self.conn, self.subject, today=TODAY,
                                       store=False, include_synthetic=True)

    def test_last_asking_price_is_not_a_sale(self):
        """CLASSIC.COM preserves a removed listing's final ASK when no sold
        price was given. That must never be valued as a transaction."""
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000, price_basis="last_asking")
        res = self._value()
        self.assertEqual(res["status"], "insufficient_comps")
        self.assertIn("INSUFFICIENT DATA", res["detail"]["explanation"])
        self.assertIn("last_asking", res["detail"]["explanation"])

    def test_inferred_dealer_sale_is_not_a_verified_price(self):
        """A listing vanishing from dealer inventory is a removal, not a sale."""
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000,
                               price_basis="inferred_from_removal")
        res = self._value()
        self.assertEqual(res["status"], "insufficient_comps")
        self.assertEqual(res["n_comps"], 0)

    def test_records_without_permission_basis_are_excluded(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000, permission_basis="unknown")
        res = self._value()
        self.assertEqual(res["status"], "insufficient_comps")
        self.assertIn("permission_basis=unknown", res["detail"]["explanation"])

    def test_excluded_records_are_counted_and_explained(self):
        for i in range(5):
            add_synthetic_comp(self.conn, i, 60000, 50000, price_basis="last_asking")
        for i in range(5, 8):
            add_synthetic_comp(self.conn, i, 60000, 50000, permission_basis="unknown")
        res = self._value()
        self.assertEqual(res["detail"]["selection"]["excluded"]["total"], 8)

    def test_mixed_set_uses_only_the_verified_permitted_ones(self):
        for i in range(5):
            add_synthetic_comp(self.conn, i, 60000, 50000)           # usable
        for i in range(5, 12):
            add_synthetic_comp(self.conn, i, 90000, 50000, price_basis="last_asking")
        res = self._value()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["n_comps"], 5)
        self.assertEqual(res["point_value"], 60000.0)   # the 90k asks had no effect

    def test_defaults_are_unusable(self):
        """A comp added without declaring provenance is stored but not used."""
        for i in range(8):
            comps.add_comp(
                self.conn, generation="997.1", variant="Carrera S", year=2008,
                body_style="coupe", mileage=50000, sale_price=60000,
                sale_date=(TODAY - dt.timedelta(days=30)).isoformat(),
                venue="dealer", source_url=f"https://example.invalid/DEFAULTS/{i}",
                is_synthetic=True)
        self.assertEqual(self._value()["status"], "insufficient_comps")

    def test_rejects_unknown_basis_values(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=1,
                           sale_date="2026-01-01", source_url="https://x.test/a",
                           price_basis="probably_sold")
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=1,
                           sale_date="2026-01-01", source_url="https://x.test/a",
                           permission_basis="i_found_it")

    def test_asserted_permission_requires_a_note(self):
        with self.assertRaises(comps.CompRejected):
            comps.add_comp(self.conn, generation="997.1", sale_price=1,
                           sale_date="2026-01-01", source_url="https://x.test/a",
                           permission_basis="operator_asserts_permission")
        cid = comps.add_comp(
            self.conn, generation="997.1", sale_price=1, sale_date="2026-01-01",
            source_url="https://x.test/a",
            permission_basis="operator_asserts_permission",
            permission_note="Written permission from the auction house, 2026-09-01.")
        self.assertIsInstance(cid, int)

    def test_coverage_separates_stored_from_usable(self):
        add_synthetic_comp(self.conn, 1, 60000, 50000)
        add_synthetic_comp(self.conn, 2, 60000, 50000, price_basis="last_asking")
        add_synthetic_comp(self.conn, 3, 60000, 50000, permission_basis="unknown")
        conn2 = self.conn
        row = conn2.execute(
            f"""SELECT COUNT(*) AS stored,
                SUM(CASE WHEN {comps.usable_sql_clause()} THEN 1 ELSE 0 END) AS usable
                FROM comps""").fetchone()
        self.assertEqual(row["stored"], 3)
        self.assertEqual(row["usable"], 1)


class TestValuation(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.subject = {"id": None, "generation": "997.1", "year": 2008,
                        "mileage": 50000, "variant": "Carrera S",
                        "body_style": "coupe", "price": 40000}

    def test_no_comps_means_no_number(self):
        res = valuation.value_listing(self.conn, self.subject, today=TODAY, store=False)
        self.assertEqual(res["status"], "insufficient_comps")
        self.assertIsNone(res["point_value"])

    def test_two_comps_still_refuses(self):
        for i, p in enumerate([60000, 62000]):
            add_synthetic_comp(self.conn, i, p, 50000)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        self.assertEqual(res["status"], "insufficient_comps")

    def test_missing_mileage_blocks_valuation(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        subject = {**self.subject, "mileage": None}
        res = valuation.value_listing(self.conn, subject, today=TODAY,
                                      store=False, include_synthetic=True)
        self.assertEqual(res["status"], "missing_inputs")
        self.assertIn("mileage", res["detail"]["missing_fields"])

    def test_median_of_identical_comps(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["point_value"], 60000.0)
        self.assertEqual(res["n_comps"], 8)
        self.assertEqual(res["confidence"], "high")

    def test_mileage_adjustment_direction(self):
        # Comps have MORE miles than the subject -> subject is worth MORE.
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 70000)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        per_mile = db.get_assumption(self.conn, "mileage_adjust_per_mile")
        self.assertAlmostEqual(res["point_value"], 60000 + 20000 * per_mile, places=2)

    def test_mileage_adjustment_is_capped(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 500000)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        cap = db.get_assumption(self.conn, "mileage_adjust_cap")
        self.assertAlmostEqual(res["point_value"], 60000 + cap, places=2)

    def test_auction_comp_gets_buyer_premium(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000, venue="bring_a_trailer")
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        premium = db.get_assumption(self.conn, "buyer_premium_pct")
        self.assertAlmostEqual(res["point_value"], 60000 * (1 + premium), places=2)

    def test_stale_comps_excluded(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000, days_ago=800)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        self.assertEqual(res["status"], "insufficient_comps")

    def test_every_comp_is_traceable(self):
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        res = valuation.value_listing(self.conn, self.subject, today=TODAY,
                                      store=False, include_synthetic=True)
        for c in res["detail"]["comps"]:
            self.assertTrue(c["source_url"].startswith("http"))
            self.assertTrue(c["sale_date"])


class TestDealMath(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        self.listing = {"id": None, "generation": "997.1", "year": 2008,
                        "mileage": 50000, "variant": "Carrera S",
                        "body_style": "coupe", "price": 40000, "seller_state": "CA"}

    def _eval(self, **kw):
        return deal.evaluate(self.conn, self.listing, destination_state="OH",
                             store=False, include_synthetic=True, **kw)

    def test_profit_identity(self):
        r = self._eval()
        self.assertEqual(r["status"], "ok")
        expected = r["expected_resale"] - r["total_cost"]
        self.assertAlmostEqual(r["net_profit"], expected, places=2)

    def test_total_cost_is_the_sum_of_its_parts(self):
        r = self._eval()
        parts = sum(l["amount"] for l in r["detail"]["cost_lines"])
        self.assertAlmostEqual(r["total_cost"],
                               r["asking_price"] + parts + r["risk_reserve"], places=2)

    def test_max_purchase_price_yields_exactly_the_target(self):
        """Buying at max_purchase_price must produce exactly the target profit."""
        r = self._eval()
        at_max = deal.evaluate(self.conn, {**self.listing, "price": r["max_purchase_price"]},
                               destination_state="OH", store=False, include_synthetic=True)
        self.assertAlmostEqual(at_max["net_profit"],
                               db.get_assumption(self.conn, "target_net_profit"), places=2)

    def test_threshold_flag(self):
        cheap = deal.evaluate(self.conn, {**self.listing, "price": 20000},
                              destination_state="OH", store=False, include_synthetic=True)
        rich = deal.evaluate(self.conn, {**self.listing, "price": 59000},
                             destination_state="OH", store=False, include_synthetic=True)
        self.assertTrue(cheap["meets_threshold"])
        self.assertFalse(rich["meets_threshold"])

    def test_unknown_seller_state_costs_more_not_less(self):
        near = deal.evaluate(self.conn, {**self.listing, "seller_state": "OH"},
                             destination_state="OH", store=False, include_synthetic=True)
        unknown = deal.evaluate(self.conn, {**self.listing, "seller_state": None},
                                destination_state="OH", store=False, include_synthetic=True)
        near_t = next(l for l in near["detail"]["cost_lines"] if l["key"] == "transport")
        unk_t = next(l for l in unknown["detail"]["cost_lines"] if l["key"] == "transport")
        self.assertGreater(unk_t["amount"], near_t["amount"])

    def test_overrides_are_marked_verified(self):
        r = self._eval(repairs_override=1200, transport_override=900)
        lines = {l["key"]: l for l in r["detail"]["cost_lines"]}
        self.assertEqual(lines["repairs"]["amount"], 1200)
        self.assertTrue(lines["repairs"]["verified"])
        self.assertEqual(lines["transport"]["amount"], 900)

    def test_no_comps_produces_no_profit_claim(self):
        conn = fresh_db()
        r = deal.evaluate(conn, self.listing, destination_state="OH", store=False)
        self.assertEqual(r["status"], "insufficient_comps")
        self.assertIsNone(r["net_profit"])
        self.assertIsNone(r["max_purchase_price"])
        self.assertFalse(r["meets_threshold"])

    def test_explanation_names_unverified_assumptions(self):
        r = self._eval()
        text = " ".join(r["detail"]["explanation"])
        self.assertIn("UNVERIFIED", text)


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        self.lid, _, _ = manual.add_listing(
            self.conn, "https://example.invalid/SYNTHETIC-TEST/override",
            title="2008 Porsche 911 Carrera S", mileage=50000, price=40000,
            decode_vin=False)
        self.conn.execute("UPDATE listings SET seller_state='CA' WHERE id=?", (self.lid,))
        self.conn.commit()

    def _row(self):
        return self.conn.execute("SELECT * FROM listings WHERE id=?", (self.lid,)).fetchone()

    def test_stored_override_replaces_placeholder(self):
        before = deal.evaluate(self.conn, self._row(), destination_state="OH",
                               store=False, include_synthetic=True)
        db.set_override(self.conn, self.lid, repairs=1800, transport=1100)
        after = deal.evaluate(self.conn, self._row(), destination_state="OH",
                              store=False, include_synthetic=True)
        lines = {l["key"]: l for l in after["detail"]["cost_lines"]}
        self.assertEqual(lines["repairs"]["amount"], 1800)
        self.assertEqual(lines["transport"]["amount"], 1100)
        self.assertTrue(lines["repairs"]["verified"])
        self.assertTrue(lines["transport"]["verified"])
        self.assertGreater(after["net_profit"], before["net_profit"])

    def test_override_shrinks_the_unverified_list(self):
        db.set_override(self.conn, self.lid, repairs=1800, transport=1100)
        r = deal.evaluate(self.conn, self._row(), destination_state="OH",
                          store=False, include_synthetic=True)
        self.assertNotIn("repairs", r["detail"]["unverified_lines"])
        self.assertNotIn("transport", r["detail"]["unverified_lines"])

    def test_partial_override_is_merged_not_erased(self):
        db.set_override(self.conn, self.lid, repairs=1800)
        db.set_override(self.conn, self.lid, transport=1100)
        cur = db.get_override(self.conn, self.lid)
        self.assertEqual(cur["repairs"], 1800)
        self.assertEqual(cur["transport"], 1100)

    def test_explicit_argument_beats_stored_override(self):
        db.set_override(self.conn, self.lid, repairs=1800)
        r = deal.evaluate(self.conn, self._row(), destination_state="OH", store=False,
                          include_synthetic=True, repairs_override=999)
        lines = {l["key"]: l for l in r["detail"]["cost_lines"]}
        self.assertEqual(lines["repairs"]["amount"], 999)


class TestTransactionCosts(unittest.TestCase):
    """Taxes, dealer fees, auction premiums and the PPI."""

    def setUp(self):
        self.conn = fresh_db()
        for i in range(8):
            add_synthetic_comp(self.conn, i, 60000, 50000)
        self.base = {"id": None, "generation": "997.1", "year": 2008,
                     "mileage": 50000, "variant": "Carrera S", "body_style": "coupe",
                     "price": 40000, "seller_state": "CA"}

    def _eval(self, **over):
        return deal.evaluate(self.conn, {**self.base, **over}, destination_state="OH",
                             store=False, include_synthetic=True)

    def test_ppi_is_always_charged(self):
        lines = {l["key"]: l for l in self._eval()["detail"]["cost_lines"]}
        self.assertIn("ppi_cost", lines)
        self.assertGreater(lines["ppi_cost"]["amount"], 0)

    def test_dealer_doc_fee_only_for_dealers(self):
        private = {l["key"] for l in self._eval(seller_type="private")["detail"]["cost_lines"]}
        dealer = {l["key"] for l in self._eval(seller_type="dealer")["detail"]["cost_lines"]}
        self.assertNotIn("dealer_doc_fee", private)
        self.assertIn("dealer_doc_fee", dealer)

    def test_unknown_seller_type_says_so(self):
        text = " ".join(self._eval(seller_type=None)["detail"]["explanation"])
        self.assertIn("Seller type is unknown", text)

    def test_auction_premium_only_for_auctions(self):
        fixed = self._eval(listing_type="fixed")
        auction = self._eval(listing_type="auction")
        self.assertEqual(fixed["detail"]["auction_buyer_premium_pct"], 0.0)
        self.assertGreater(auction["detail"]["auction_buyer_premium_pct"], 0)
        self.assertLess(auction["net_profit"], fixed["net_profit"])

    def test_purchase_tax_reduces_profit_and_max_bid(self):
        before = self._eval()
        db.set_assumption(self.conn, "purchase_tax_pct", 0.0625,
                          basis="test state rate", verified=True)
        after = self._eval()
        self.assertAlmostEqual(before["net_profit"] - after["net_profit"],
                               40000 * 0.0625, places=2)
        self.assertLess(after["max_purchase_price"], before["max_purchase_price"])

    def test_unset_tax_is_never_treated_as_an_exemption(self):
        r = self._eval()
        text = " ".join(r["detail"]["explanation"])
        self.assertIn("PURCHASE TAX IS NOT SET", text)
        self.assertIn("not an exemption", text)
        self.assertTrue(r["before_purchase_tax"])
        self.assertIn("purchase_tax_pct is not set", r["underwriting_blockers"])

    def test_confirmed_zero_tax_is_accepted_and_labelled(self):
        db.set_assumption(self.conn, "purchase_tax_pct", 0.0,
                          basis="resale exemption confirmed", verified=True)
        r = self._eval()
        self.assertFalse(r["before_purchase_tax"])
        self.assertNotIn("purchase_tax_pct is not set", r["underwriting_blockers"])

    def test_unvalued_when_no_comps(self):
        conn = fresh_db()
        r = deal.evaluate(conn, self.base, destination_state="OH", store=False)
        self.assertEqual(r["underwriting_status"], "unvalued")

    def test_preliminary_while_placeholders_remain(self):
        self.assertEqual(self._eval()["underwriting_status"], "preliminary")

    def test_fully_underwritten_once_everything_is_confirmed(self):
        for key in ("repairs_997", "detail_and_photography", "title_and_admin",
                    "ppi_cost", "sale_fee_pct", "sale_fee_cap", "carrying_cost_per_day",
                    "days_to_sell", "risk_reserve_pct", "resale_haircut_pct",
                    "transport_base", "transport_per_mile", "purchase_tax_pct",
                    "dealer_doc_fee", "auction_buyer_premium_pct"):
            db.set_assumption(self.conn, key,
                              db.get_assumption(self.conn, key),
                              basis="confirmed in test", verified=True)
        r = deal.evaluate(self.conn, {**self.base, "seller_type": "private",
                                      "listing_type": "fixed"},
                          destination_state="OH", store=False,
                          include_synthetic=True, transport_override=1100)
        self.assertEqual(r["underwriting_blockers"], [])
        self.assertEqual(r["underwriting_status"], "underwritten")
        self.assertIn("FULLY UNDERWRITTEN", " ".join(r["detail"]["explanation"]))

    def test_max_bid_identity_holds_with_tax_and_auction(self):
        """P_max must still yield exactly the target once tax and an auction
        premium are in play -- this is the whole point of the solve."""
        db.set_assumption(self.conn, "purchase_tax_pct", 0.0625)
        r = self._eval(listing_type="auction", seller_type="dealer")
        at_max = self._eval(listing_type="auction", seller_type="dealer",
                            price=r["max_purchase_price"])
        # max_purchase_price is rounded to the cent before being re-fed, so a
        # sub-cent residual is expected and harmless.
        self.assertAlmostEqual(at_max["net_profit"],
                               db.get_assumption(self.conn, "target_net_profit"),
                               delta=0.05)

    def test_total_cost_identity_with_all_rates(self):
        db.set_assumption(self.conn, "purchase_tax_pct", 0.0625)
        r = self._eval(listing_type="auction", seller_type="dealer")
        fixed = sum(l["amount"] for l in r["detail"]["cost_lines"])
        rates = sum(rl["amount"] for rl in r["detail"]["rate_lines"])
        self.assertAlmostEqual(r["total_cost"], r["asking_price"] + fixed + rates,
                               places=2)
        self.assertAlmostEqual(r["net_profit"], r["expected_resale"] - r["total_cost"],
                               places=2)

    def test_applied_rates_appear_in_unverified_list(self):
        db.set_assumption(self.conn, "purchase_tax_pct", 0.0625)
        r = self._eval(listing_type="auction")
        self.assertIn("auction_buyer_premium", r["detail"]["unverified_lines"])


class TestManualEntry(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def test_requires_url(self):
        with self.assertRaises(ValueError):
            manual.add_listing(self.conn, "just some car I saw")

    def test_routes_url_to_marketplace(self):
        self.assertEqual(manual.source_key_for_url("https://bringatrailer.com/listing/x/"),
                         "bring_a_trailer")
        self.assertEqual(manual.source_key_for_url("https://www.facebook.com/marketplace/item/1"),
                         "facebook_marketplace")
        self.assertEqual(manual.source_key_for_url("https://sfbay.craigslist.org/x.html"),
                         "craigslist_rss")
        self.assertEqual(manual.source_key_for_url("https://someshop.test/inventory/1"), "manual")

    def test_prohibited_source_is_flagged_not_fetched(self):
        _, _, rep = manual.add_listing(
            self.conn, "https://carsandbids.com/auctions/example",
            title="2008 Porsche 911 Carrera S", decode_vin=False)
        self.assertEqual(rep["source_key"], "cars_and_bids")
        self.assertTrue(any("prohibit" in n for n in rep["notices"]))

    def test_parses_title_and_records_missing(self):
        lid, created, rep = manual.add_listing(
            self.conn, "https://example.invalid/SYNTHETIC-TEST/listing",
            title="2003 Porsche 911 Carrera 4S Cabriolet 6-speed manual",
            price=31000, decode_vin=False)
        self.assertTrue(created)
        row = self.conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        self.assertEqual(row["year"], 2003)
        self.assertEqual(row["generation"], "996.2")
        self.assertEqual(row["variant"], "Carrera 4S")
        self.assertEqual(row["body_style"], "cabriolet")
        self.assertIn("mileage", rep["missing_fields"])
        self.assertIn("vin", rep["missing_fields"])

    def test_price_history_records_changes(self):
        url = "https://example.invalid/SYNTHETIC-TEST/pricecut"
        manual.add_listing(self.conn, url, price=45000, decode_vin=False)
        lid, _, _ = manual.add_listing(self.conn, url, price=42000, decode_vin=False)
        manual.add_listing(self.conn, url, price=42000, decode_vin=False)
        rows = self.conn.execute(
            "SELECT price FROM price_history WHERE listing_id=? ORDER BY id", (lid,)).fetchall()
        self.assertEqual([r["price"] for r in rows], [45000, 42000])

    def test_update_never_erases_known_data(self):
        url = "https://example.invalid/SYNTHETIC-TEST/keepdata"
        lid, _, _ = manual.add_listing(self.conn, url, mileage=48000, price=50000,
                                       decode_vin=False)
        manual.add_listing(self.conn, url, price=49000, decode_vin=False)
        row = self.conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        self.assertEqual(row["mileage"], 48000)


class TestJsonLd(unittest.TestCase):
    SAMPLE = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Car",
     "name":"2013 Porsche 911 Carrera S Coupe","brand":{"@type":"Brand","name":"Porsche"},
     "modelDate":"2013","vehicleTransmission":"7-Speed PDK",
     "mileageFromOdometer":{"@type":"QuantitativeValue","value":"48,210","unitCode":"SMI"},
     "vehicleIdentificationNumber":"WP0AB2A99DS123456","color":"GT Silver",
     "image":["https://example.invalid/1.jpg","https://example.invalid/2.jpg"],
     "offers":{"@type":"Offer","price":"71995","priceCurrency":"USD",
       "url":"https://example.invalid/inventory/1",
       "seller":{"@type":"AutoDealer","name":"Example Motors",
         "address":{"addressLocality":"Denver","addressRegion":"CO","postalCode":"80202"}}}}
    </script></head></html>"""

    def test_parses_schema_org_vehicle(self):
        rec = jsonld.parse_vehicle(jsonld.extract_jsonld(self.SAMPLE),
                                   "https://example.invalid/inventory/1")["record"]
        self.assertEqual(rec["year"], 2013)
        self.assertEqual(rec["generation"], "991.1")
        self.assertEqual(rec["variant"], "Carrera S")
        self.assertEqual(rec["transmission"], "pdk")
        self.assertEqual(rec["mileage"], 48210)
        self.assertEqual(rec["price"], 71995.0)
        self.assertEqual(rec["seller_state"], "CO")
        self.assertEqual(rec["vin"], "WP0AB2A99DS123456")

    def test_no_jsonld_returns_nothing(self):
        self.assertEqual(jsonld.extract_jsonld("<html><body>a car</body></html>"), [])

    def test_domain_allowlist_blocks_by_default(self):
        self.assertFalse(jsonld.domain_allowed("https://example.invalid/x", allowlist=set()))
        self.assertTrue(jsonld.domain_allowed("https://www.example.invalid/x",
                                              allowlist={"example.invalid"}))


class TestRss(unittest.TestCase):
    FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>2001 Porsche 911 Carrera - $28,500</title>
        <link>https://example.invalid/SYNTHETIC-TEST/1.html</link></item>
      <item><title>2019 Porsche Macan S - $34,000</title>
        <link>https://example.invalid/SYNTHETIC-TEST/2.html</link></item>
    </channel></rss>"""

    def test_parse_and_filter(self):
        items = rss.parse_feed(self.FEED)
        self.assertEqual(len(items), 2)
        self.assertTrue(rss.looks_like_911(items[0]["title"]))
        self.assertFalse(rss.looks_like_911(items[1]["title"]))
        self.assertEqual(rss.price_from_title(items[0]["title"]), 28500.0)

    def test_disabled_without_feeds(self):
        res = rss.ingest(fresh_db(), feeds=[])
        self.assertEqual(res.status, "skipped")


class TestGeo(unittest.TestCase):
    def test_distance_is_symmetric_and_sane(self):
        a, _ = geo.estimate_road_miles("CA", "NY")
        b, _ = geo.estimate_road_miles("NY", "CA")
        self.assertAlmostEqual(a, b, places=6)
        self.assertGreater(a, 2000)
        self.assertLess(a, 4000)

    def test_unknown_state(self):
        miles, basis = geo.estimate_road_miles("ZZ", "OH")
        self.assertIsNone(miles)
        self.assertIn("unknown", basis)


class TestSourceRuns(unittest.TestCase):
    def test_run_bookkeeping_updates_timestamps(self):
        conn = fresh_db()
        rid = db.start_run(conn, "manual")
        db.finish_run(conn, rid, "ok", seen=3, new=2)
        row = conn.execute("SELECT * FROM sources WHERE key='manual'").fetchone()
        self.assertIsNotNone(row["last_success_at"])
        self.assertIsNotNone(row["last_attempt_at"])

    def test_error_is_recorded(self):
        conn = fresh_db()
        rid = db.start_run(conn, "marketcheck")
        db.finish_run(conn, rid, "error", message="HTTP 401")
        row = conn.execute("SELECT * FROM sources WHERE key='marketcheck'").fetchone()
        self.assertEqual(row["last_error"], "HTTP 401")


if __name__ == "__main__":
    unittest.main()
