"""911-only enforcement tests (permanent rule: this app is exclusively for 911s).

Proves that Cayman / 718 Cayman / Boxster / Cayenne / Macan / Panamera / Taycan
listings cannot enter the active 911 inventory through ANY path -- dealer JSON-LD,
manual/assisted import, or the model classifier itself -- and that the database
audit quarantines a non-911 while keeping an audit trail.
"""

from __future__ import annotations

import tempfile
import unittest

from porschehunter import db, model_guard as guard
from porschehunter.sources import jsonld, manual


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = db.connect(tmp.name)
    db.init_db(conn)
    return conn


class TestClassifier(unittest.TestCase):
    def test_confirms_real_911s(self):
        for t in ["2008 Porsche 911 Carrera S", "2016 Porsche 911 GT3 RS",
                  "1997 Porsche 911 Targa", "2003 Porsche 911"]:
            v, _ = guard.classify_911(title=t)
            self.assertEqual(v, guard.VERDICT_CONFIRMED, t)

    def test_rejects_non_911_models_by_title(self):
        for t in ["2007 Porsche Cayman S", "2018 Porsche 718 Cayman GTS",
                  "2016 Porsche Boxster Spyder", "2020 Porsche Cayenne Turbo",
                  "2019 Porsche Macan S", "2021 Porsche Panamera",
                  "2022 Porsche Taycan 4S"]:
            v, _ = guard.classify_911(title=t)
            self.assertEqual(v, guard.VERDICT_REJECTED, t)

    def test_cayman_with_carrera_wheels_is_rejected(self):
        v, _ = guard.classify_911(
            title="2007 Porsche Cayman S - 6 Speed Manual, Carrera S Wheels",
            variant="Carrera S")
        self.assertEqual(v, guard.VERDICT_REJECTED)

    def test_vin_decode_is_authoritative(self):
        # Title says 911 but the VIN decodes to a Cayman -> rejected.
        v, _ = guard.classify_911(title="2014 Porsche 911 Carrera",
                                  vin_model="718 Cayman")
        self.assertEqual(v, guard.VERDICT_REJECTED)
        # Title sparse but VIN says 911 -> confirmed.
        v2, _ = guard.classify_911(title="Porsche", vin_model="911")
        self.assertEqual(v2, guard.VERDICT_CONFIRMED)

    def test_stock_number_with_911_digits_is_not_a_confirmation(self):
        v, _ = guard.classify_911(title="2020 Porsche Macan", url="/id-65391149")
        self.assertEqual(v, guard.VERDICT_REJECTED)  # Macan named -> rejected
        v2, _ = guard.classify_911(title="Porsche")   # only a bare make
        self.assertEqual(v2, guard.VERDICT_QUARANTINE)


class TestJsonLdRejectsNon911(unittest.TestCase):
    def _page(self, name, vin="WP0AB2A99CS700099"):
        return f"""<html><head><script type="application/ld+json">
        {{"@context":"https://schema.org","@type":"Vehicle","name":"{name}",
          "brand":{{"name":"Porsche"}},"modelDate":"2014",
          "vehicleIdentificationNumber":"{vin}",
          "offers":{{"@type":"Offer","price":55000,"priceCurrency":"USD"}}}}
        </script></head></html>"""

    def test_cayman_cannot_enter_via_jsonld(self):
        conn = fresh_db()
        conn.execute("INSERT INTO listings (source_key,url,title,status,first_seen_at,"
                     "last_seen_at,fetched_at) VALUES ('dealer_jsonld','x','x','active',"
                     "'2026-01-01','2026-01-01','2026-01-01')")  # noise row
        objs = jsonld.extract_jsonld(self._page("2018 Porsche 718 Cayman GTS"))
        rec = jsonld.parse_vehicle(objs, "https://d.invalid/718-cayman")["record"]
        # classify directly (no network VIN decode in the test)
        v, _ = guard.classify_911(title=rec["title"], variant=rec.get("variant"))
        self.assertEqual(v, guard.VERDICT_REJECTED)


class TestManualRejectsNon911(unittest.TestCase):
    def test_manual_add_rejects_cayman(self):
        conn = fresh_db()
        with self.assertRaises(manual.NotA911):
            manual.add_listing(conn, "https://www.facebook.com/marketplace/item/1",
                               title="2016 Porsche Cayman GT4", decode_vin=False)

    def test_manual_add_accepts_911(self):
        conn = fresh_db()
        lid, created, _ = manual.add_listing(
            conn, "https://www.facebook.com/marketplace/item/2",
            title="2008 Porsche 911 Carrera S", decode_vin=False)
        self.assertTrue(created)


class TestDbAudit(unittest.TestCase):
    def test_audit_quarantines_non_911_with_trail(self):
        conn = fresh_db()
        # Insert a legit 911 and a sneaked-in Cayman directly.
        for title, url in [("2008 Porsche 911 Carrera S", "u1"),
                           ("2007 Porsche Cayman S", "u2")]:
            conn.execute(
                "INSERT INTO listings (source_key,url,title,status,first_seen_at,"
                "last_seen_at,fetched_at) VALUES ('dealer_jsonld',?,?,'active',"
                "'2026-01-01','2026-01-01','2026-01-01')", (url, title))
        conn.commit()
        res = guard.audit_db(conn)
        self.assertEqual(res["quarantined"], 1)
        self.assertEqual(res["rejected_model"], 1)
        # The Cayman is now quarantined (out of active), with a reason preserved.
        row = conn.execute("SELECT status, quarantine_reason FROM listings WHERE url='u2'").fetchone()
        self.assertEqual(row["status"], "quarantined")
        self.assertTrue(row["quarantine_reason"])
        # The 911 stays active.
        self.assertEqual(conn.execute("SELECT status FROM listings WHERE url='u1'").fetchone()["status"], "active")


if __name__ == "__main__":
    unittest.main()
