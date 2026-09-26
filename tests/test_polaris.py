# -*- coding: utf-8 -*-
"""Tests zonder model of netwerk: chunking, beveiliging, zoekvoorbereiding, config.

    python -m unittest discover -s tests
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polaris import beveiliging, config, index  # noqa: E402


class TestChunking(unittest.TestCase):
    def test_knipt_op_koppen_met_pad(self):
        tekst = "## A\n" + "a" * 400 + "\n### B\n" + "b" * 400
        stukken = index.chunk_markdown(tekst, "doc")
        self.assertEqual([s for s, _ in stukken], ["A", "A › B"])

    def test_korte_secties_worden_samengevoegd(self):
        tekst = "## A\n" + "a" * 400 + "\n## Kort\nklein stukje"
        stukken = index.chunk_markdown(tekst, "doc")
        self.assertEqual(len(stukken), 1)
        self.assertIn("klein stukje", stukken[0][1])

    def test_lange_sectie_wordt_op_alineas_geknipt(self):
        tekst = "## A\n" + "\n\n".join(["x" * 700] * 4)
        stukken = index.chunk_markdown(tekst, "doc")
        self.assertGreater(len(stukken), 1)
        self.assertTrue(all(len(t) <= index.MAX_STUK for _, t in stukken))

    def test_kop_in_codeblok_telt_niet(self):
        tekst = "## Echt\n" + "a" * 400 + "\n```\n## geen kop\n```\n" + "b" * 50
        stukken = index.chunk_markdown(tekst, "doc")
        self.assertEqual([s for s, _ in stukken], ["Echt"])


class TestBeveiliging(unittest.TestCase):
    def test_wachtwoordwaarde_wordt_gemaskeerd(self):
        uit, n = beveiliging.maskeer("Wachtwoord: Zomer2026!Kantoor")
        self.assertEqual(uit, "Wachtwoord: [verborgen]")
        self.assertEqual(n, 1)

    def test_private_key_wordt_gemaskeerd(self):
        sleutel = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        uit, n = beveiliging.maskeer("hier: " + sleutel)
        self.assertNotIn("abc", uit)
        self.assertEqual(n, 1)

    def test_bekende_token_wordt_gemaskeerd(self):
        uit, _ = beveiliging.maskeer("token ghp_" + "A1b2" * 10)
        self.assertNotIn("ghp_", uit)

    def test_gewone_tekst_blijft_staan(self):
        tekst = "Het access point krijgt stroom via de switch."
        self.assertEqual(beveiliging.maskeer(tekst), (tekst, 0))

    def test_lange_reeks_zonder_hoofdletters_blijft_staan(self):
        # Een hash of pad van alleen kleine letters en cijfers is geen geheim.
        tekst = "commit " + "a1" * 20
        self.assertEqual(beveiliging.maskeer(tekst)[1], 0)

    def test_optout_in_frontmatter(self):
        self.assertTrue(beveiliging.heeft_optout("---\npolaris: nee\n---\n# x"))
        self.assertTrue(beveiliging.heeft_optout("---\nindex: false\n---\n"))
        self.assertFalse(beveiliging.heeft_optout("# polaris: nee\nniet in frontmatter"))

    def test_uitsluiten_wint_van_alleen(self):
        self.assertFalse(beveiliging.mag_indexeren("hr/salaris.md", ["hr/*"], ["*salaris*"]))
        self.assertTrue(beveiliging.mag_indexeren("hr/verlof.md", ["hr/*"], ["*salaris*"]))
        self.assertFalse(beveiliging.mag_indexeren("it/netwerk.md", ["hr/*"], []))
        self.assertTrue(beveiliging.mag_indexeren("it/netwerk.md", [], []))

    def test_is_waar(self):
        for v in (True, "ja", "true", 1, "Yes"):
            self.assertTrue(beveiliging.is_waar(v))
        for v in (False, None, "nee", 0, ""):
            self.assertFalse(beveiliging.is_waar(v))


class TestZoekvoorbereiding(unittest.TestCase):
    def test_fts_pogingen_exact_dan_stam(self):
        pogingen = index._fts_pogingen("Waar staat de herstelprocedure?")
        self.assertEqual([g for _, g in pogingen], [1.0, 0.7, 0.5])
        self.assertIn('"herstelprocedure"', pogingen[0][0])
        self.assertIn("herstelproced*", pogingen[1][0])

    def test_alleen_stopwoorden_geeft_niets(self):
        self.assertEqual(index._fts_pogingen("wat is het"), [])

    def test_verwijzingen_zonder_eigen_id(self):
        patronen = [re.compile(p) for p in config.STANDAARD_VERWIJZINGSPATRONEN]
        self.assertEqual(index._verwijzingen("zie TK-1 en TCK-1042", patronen, "TK-1"),
                         "TCK-1042")


class TestConfig(unittest.TestCase):
    def test_paden_relatief_aan_configbestand(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "polaris.toml")
            with open(pad, "w", encoding="utf-8") as f:
                f.write('[algemeen]\ndb_pad = "x.db"\n[[bron]]\npad = "docs"\n')
            c = config.laad(pad)
            self.assertEqual(c.db_pad, os.path.join(d, "x.db"))
            self.assertEqual(c.bronnen[0].pad, os.path.join(d, "docs"))
            self.assertTrue(c.maskeer_geheimen)

    def test_zonder_bronnen_is_fout(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "polaris.toml")
            with open(pad, "w", encoding="utf-8") as f:
                f.write('[algemeen]\n')
            with self.assertRaises(ValueError):
                config.laad(pad)


if __name__ == "__main__":
    unittest.main()
