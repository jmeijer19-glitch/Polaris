# -*- coding: utf-8 -*-
"""Tests zonder model of netwerk: chunking, beveiliging, zoekvoorbereiding, config.

    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from polaris import beveiliging, config, index  # noqa: E402
from polaris import eval as evalmod  # noqa: E402

try:
    import snowballstemmer  # noqa: F401
    HEEFT_SNOWBALL = True
except ImportError:
    HEEFT_SNOWBALL = False


class NepEmbedder(object):
    """Deterministische 'betekenis' zonder model: een vector uit letter-drietallen. Teksten
    die op elkaar lijken krijgen vergelijkbare vectoren, genoeg om de pijplijn te testen."""
    dim = 64

    def _vec(self, tekst):
        v = np.zeros(self.dim, dtype=np.float32)
        t = re.sub(r"\W+", " ", tekst.lower())
        for i in range(len(t) - 2):
            h = int(hashlib.md5(t[i:i + 3].encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def passages(self, teksten):
        return np.stack([self._vec(t) for t in teksten])

    def query(self, tekst):
        return self._vec(tekst)


def _config(d, extra_algemeen="", bronnen=None):
    """Schrijft een config in tijdelijke map d en laadt hem. Model = nep (dim 64)."""
    bronnen = bronnen if bronnen is not None else '[[bron]]\nnaam = "docs"\npad = "docs"\n'
    pad = os.path.join(d, "polaris.toml")
    with open(pad, "w", encoding="utf-8") as f:
        f.write('[algemeen]\nmodel = "nep/model"\ndim = 64\ndb_pad = "x.db"\n'
                + extra_algemeen + "\n" + bronnen)
    return config.laad(pad)


def _schrijf(d, rel, tekst):
    vol = os.path.join(d, rel)
    os.makedirs(os.path.dirname(vol), exist_ok=True)
    with open(vol, "w", encoding="utf-8") as f:
        f.write(tekst)


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

    def test_markdown_opmaak_rond_wachtwoord(self):
        uit, n = beveiliging.maskeer("**Wachtwoord:** `Abcd1-Efgh2-Ijkl3-Mnop4-Qrst5`")
        self.assertEqual(n, 1)
        self.assertNotIn("Abcd1", uit)
        self.assertIn("**Wachtwoord:** `[verborgen]`", uit)

    def test_wachtwoord_in_groepen_zonder_sleutelwoord(self):
        uit, n = beveiliging.maskeer("gebruik Abcd1-Efgh2-Ijkl3-Mnop4-Qrst5 om in te loggen")
        self.assertEqual(n, 1)
        self.assertNotIn("Abcd1", uit)

    def test_uuid_en_datum_blijven_staan(self):
        for t in ("id 550e8400-e29b-41d4-a716-446655440000", "notulen-2026-09-26-overleg"):
            self.assertEqual(beveiliging.maskeer(t), (t, 0))

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

    def test_stemmer_uit_alleen_exact(self):
        self.assertEqual([g for _, g in index._fts_pogingen("herstelprocedure firewall", "uit")], [1.0])

    @unittest.skipUnless(HEEFT_SNOWBALL, "snowballstemmer niet geïnstalleerd")
    def test_stemmer_nl_zoekt_op_stamkolom(self):
        pogingen = index._fts_pogingen("betalingen controleren", "nl")
        self.assertIn('stammen: "betaal"', pogingen[1][0])
        self.assertEqual(index.stammen("Betalingen betaald"), "betaal betaald")

    def test_een_woord_geeft_geen_or_poging(self):
        self.assertEqual([g for _, g in index._fts_pogingen("herstelprocedure")], [1.0, 0.7])

    def test_fts_syntax_herkend(self):
        self.assertTrue(index.is_fts_syntax('"access point" OR switch'))
        self.assertTrue(index.is_fts_syntax("herstel*"))
        self.assertFalse(index.is_fts_syntax("hoe herstel ik een back-up"))

    def test_actualiteit_loopt_af(self):
        vandaag = date.today().isoformat()
        oud = (date.today() - timedelta(days=365)).isoformat()
        self.assertAlmostEqual(index._actualiteit(vandaag, 730), 1.0)
        self.assertAlmostEqual(index._actualiteit(oud, 730), 0.5, places=2)
        self.assertEqual(index._actualiteit("", 730), 0.0)
        self.assertEqual(index._actualiteit("2000-01-01", 730), 0.0)

    def test_verwijzingen_zonder_eigen_id(self):
        patronen = [re.compile(p) for p in config.STANDAARD_VERWIJZINGSPATRONEN]
        self.assertEqual(index._verwijzingen("zie TK-1 en TCK-1042", patronen, "TK-1"),
                         "TCK-1042")


class TestFrontmatter(unittest.TestCase):
    def test_velden_en_romp(self):
        velden, romp = index.frontmatter('---\ntitle: "Back-ups"\nstatus: vervallen\n---\n## A\ntekst')
        self.assertEqual(velden, {"title": "Back-ups", "status": "vervallen"})
        self.assertEqual(romp, "## A\ntekst")

    def test_zonder_frontmatter(self):
        self.assertEqual(index.frontmatter("## A\ntekst"), ({}, "## A\ntekst"))

    def test_omschrijving_gaat_voor_elk_stuk(self):
        cfg = type("C", (), {"verwijzingspatronen": []})()
        tekst = ("---\ndescription: Hoe de back-up werkt\nstatus: geldig\n---\n"
                 "## Nachtelijk\n" + "n" * 400 + "\n## Herstel\n" + "h" * 400)
        stukken = index.stukken_uit_markdown("docs", "backup.md", tekst, cfg)
        self.assertEqual(len(stukken), 2)
        for s in stukken:
            self.assertTrue(s["tekst"].startswith("Hoe de back-up werkt\n\n"))
            self.assertEqual(s["status"], "geldig")
            self.assertEqual(s["titel"], "backup")

    def test_embed_tekst_bevat_titel_en_sectie(self):
        e = index.embed_tekst({"titel": "Back-up", "sectie": "Herstel", "tekst": "x"})
        self.assertEqual(e, "Back-up › Herstel\nx")
        e = index.embed_tekst({"titel": "Back-up", "sectie": "Back-up", "tekst": "x"})
        self.assertEqual(e, "Back-up\nx")


class TestLeesbaar(unittest.TestCase):
    def test_dict_als_regels(self):
        uit = index.leesbaar({"naam": "Switch", "poorten": 24, "tags": ["poe", "core"],
                              "leeg": "", "actief": True})
        self.assertEqual(uit, "naam: Switch\npoorten: 24\ntags: poe, core\nactief: ja")


class TestBouwEnZoek(unittest.TestCase):
    """End-to-end met de nep-embedder: bouwen, zoeken, filteren, herrangschikken."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        _schrijf(d, "docs/backup.md",
                 "---\ndescription: Back-upprocedure van de server\n---\n"
                 "## Nachtelijke back-up\nElke nacht om 02:00 draait de back-up naar de externe schijf. "
                 "De controle staat in het logboek. " * 4 +
                 "\n## Herstel na een storing\nKies het laatste herstelpunt en zet eerst terug naar "
                 "een aparte locatie voordat je de productie overschrijft. " * 4)
        _schrijf(d, "docs/netwerk.md",
                 "## Access points\nDrie access points hangen aan de PoE-switch; het PoE-budget "
                 "is krap sinds de camera's erbij kwamen. " * 5)
        _schrijf(d, "docs/oud/2020-01-01-oude-procedure.md",
                 "---\nstatus: vervallen\n---\n## Nachtelijke back-up\nElke nacht om 02:00 draait "
                 "de back-up naar tape. Vervangen door de nieuwe procedure. " * 5)
        with open(os.path.join(d, "taken.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [
                {"id": "TK-1", "titel": "Kwartaaltest herstel", "omschrijving": "Herstel testen vanaf back-up",
                 "status": "open", "categorie": "back-up", "prijs": 12},
                {"id": "TK-2", "titel": "Switch vervangen", "omschrijving": "PoE-budget te klein",
                 "status": "open", "categorie": "netwerk"},
                {"id": "TK-3", "titel": "Geheim project", "omschrijving": "x", "vertrouwelijk": True},
            ]}, f)
        self.cfg = _config(d, bronnen=(
            '[[bron]]\nnaam = "docs"\npad = "docs"\n'
            '[[bron]]\nnaam = "taken"\ntype = "json_lijst"\npad = "taken.json"\n'
            'lijst_veld = "items"\ntekst_veld = "omschrijving"\nsectie_veld = "categorie"\n'
            'uitsluit_veld = "vertrouwelijk"\nalle_velden = true\n'))
        self.emb = NepEmbedder()
        index.bouw(self.cfg, verbose=False, embedder=self.emb)
        self.zoeker = index.Zoeker(self.cfg, embedder=self.emb)

    def tearDown(self):
        self.tmp.cleanup()

    def test_overzichtsstuk_en_optout(self):
        stukken = index.per_bron(self.cfg.db_pad)
        self.assertEqual(stukken["taken"], 3)          # TK-1, TK-2 en het overzicht; TK-3 niet
        r = self.zoeker.zoek("welke taken zijn er", k=10, bron="taken")
        overzicht = [x for x in r if x["sectie"] == "overzicht"]
        self.assertEqual(len(overzicht), 1)
        self.assertIn("TK-1 - Kwartaaltest herstel (back-up, open)", overzicht[0]["tekst"])
        self.assertNotIn("Geheim", overzicht[0]["tekst"])

    def test_alle_velden(self):
        r = self.zoeker.zoek("prijs", k=3, bron="taken")
        self.assertIn("prijs: 12", r[0]["tekst"])

    def test_bronfilter(self):
        for x in self.zoeker.zoek("back-up herstel", k=10, bron="docs"):
            self.assertEqual(x["bron"], "docs")
        self.assertEqual(self.zoeker.zoek("back-up", k=10, bron="bestaat-niet"), [])

    def test_vervallen_zakt_en_omschrijving_telt_mee(self):
        r = self.zoeker.zoek("nachtelijke back-up", k=5, bron="docs")
        self.assertEqual(r[0]["pad"], "backup.md")
        self.assertTrue(r[0]["tekst"].startswith("Back-upprocedure van de server"))
        oud = [x for x in r if x["status"] == "vervallen"]
        self.assertTrue(oud and oud[0]["score"] < r[0]["score"])

    def test_fts_syntax_gaat_ongewijzigd_door(self):
        r = self.zoeker.zoek('"PoE-budget"', k=5)
        self.assertTrue(r)
        self.assertTrue(all("PoE-budget" in x["tekst"] for x in r))

    def test_herbouw_gebruikt_cache_en_lock(self):
        class Teller(NepEmbedder):
            aanroepen = 0

            def passages(self, teksten):
                Teller.aanroepen += 1
                return NepEmbedder.passages(self, teksten)
        index.bouw(self.cfg, verbose=False, embedder=Teller())
        self.assertEqual(Teller.aanroepen, 0)         # niets gewijzigd: alles uit de cache
        with index._Slot(self.cfg.db_pad):
            with self.assertRaises(index.BouwBezig):
                index.bouw(self.cfg, verbose=False, embedder=self.emb)

    def test_oude_indexstructuur_wordt_geweigerd(self):
        import sqlite3
        con = sqlite3.connect(self.cfg.db_pad)
        con.execute("UPDATE meta SET waarde='1' WHERE sleutel='schema'")
        con.commit(); con.close()
        with self.assertRaisesRegex(ValueError, "opnieuw"):
            index.Zoeker(self.cfg, embedder=self.emb).zoek("x")

    def test_extra_stukken_van_aanroeper(self):
        extra = [{"bron": "git", "pad": "commit abc123", "titel": "Firewall-regels aangescherpt",
                  "tekst": "Poort 22 alleen nog vanaf het beheernetwerk; wachtwoord: Geheim123!"}]
        index.bouw(self.cfg, verbose=False, embedder=self.emb, extra_stukken=extra)
        r = index.Zoeker(self.cfg, embedder=self.emb).zoek("firewall beheernetwerk", k=3, bron="git")
        self.assertEqual(r[0]["titel"], "Firewall-regels aangescherpt")
        self.assertNotIn("Geheim123", r[0]["tekst"])
        with self.assertRaises(ValueError):
            index.bouw(self.cfg, verbose=False, embedder=self.emb, extra_stukken=[{"bron": "x"}])

    def test_snippets_markeren_gevonden_woord(self):
        cfg = _config(self.tmp.name, extra_algemeen='snippets = true\nsnippet_markering = ["<", ">"]\n',
                      bronnen='[[bron]]\nnaam = "docs"\npad = "docs"\n')
        index.bouw(cfg, verbose=False, embedder=self.emb)
        r = index.Zoeker(cfg, embedder=self.emb).zoek("herstelpunt", k=3)
        self.assertIn("<herstelpunt>", r[0]["fragment"])
        self.assertTrue(all("fragment" in x for x in r))
        self.assertNotIn("fragment", self.zoeker.zoek("herstelpunt", k=1)[0])

    @unittest.skipUnless(HEEFT_SNOWBALL, "snowballstemmer niet geïnstalleerd")
    def test_stemmer_nl_vindt_verbuiging_en_eist_herbouw(self):
        cfg = _config(self.tmp.name, extra_algemeen='stemmer = "nl"\n',
                      bronnen='[[bron]]\nnaam = "docs"\npad = "docs"\n')
        with self.assertRaisesRegex(ValueError, "stemmer"):
            index.Zoeker(cfg, embedder=self.emb).zoek("x")       # index is nog 'grof'
        index.bouw(cfg, verbose=False, embedder=self.emb)
        r = index.Zoeker(cfg, embedder=self.emb).zoek("herstelde storingen", k=3, bron="docs")
        self.assertEqual(r[0]["pad"], "backup.md")               # "Herstel na een storing"

    def test_eval_mrr(self):
        vragen = [{"vraag": "nachtelijke back-up", "verwacht": "backup"},
                  {"vraag": "PoE-budget access points", "verwacht": "netwerk", "bron": "docs"},
                  {"vraag": "PoE-budget", "verwacht": "bestaat-niet"}]
        uit = evalmod.evalueer(self.cfg, vragen, k=5, zoeker=self.zoeker)
        self.assertEqual(uit["gevonden"], 2)
        self.assertEqual([u["plek"] for u in uit["per_vraag"]][:2], [1, 1])
        self.assertAlmostEqual(uit["mrr"], round(2 / 3, 3))
        self.assertIn("2/3", evalmod.rapport(uit))
        with self.assertRaises(ValueError):
            pad = os.path.join(self.tmp.name, "v.json")
            with open(pad, "w") as f:
                json.dump([{"vraag": "x", "verwacht": "("}], f)
            evalmod.laad_vragen(pad)

    def test_dubbele_id_van_aanroeper_strandt_niet(self):
        extra = [{"id": "zelfde", "bron": "a", "pad": "x", "tekst": "Eerste tekst over printers en toner."},
                 {"id": "zelfde", "bron": "b", "pad": "y", "tekst": "Tweede tekst over de koffiemachine."}]
        index.bouw(self.cfg, verbose=False, embedder=self.emb, extra_stukken=extra)
        r = index.Zoeker(self.cfg, embedder=self.emb).zoek("koffiemachine", k=2)
        self.assertEqual(r[0]["bron"], "b")
        self.assertNotEqual(r[0]["id"], "zelfde")
        self.assertEqual(index.per_bron(self.cfg.db_pad)["a"], 1)

    def test_matrix_wordt_ververst_na_herbouw(self):
        self.zoeker.zoek("back-up")
        stand = self.zoeker._stand
        _schrijf(self.tmp.name, "docs/nieuw.md", "## Printer\nDe printer op de eerste verdieping "
                 "heeft een nieuwe toner nodig, bestel via de leverancier. " * 5)
        index.bouw(self.cfg, verbose=False, embedder=self.emb)
        r = self.zoeker.zoek("printer toner", k=3)
        self.assertEqual(r[0]["pad"], "nieuw.md")
        self.assertNotEqual(self.zoeker._stand, stand)


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
            self.assertEqual(c.actualiteit_bonus, 0.25)
            self.assertEqual(c.threads, 0)
            self.assertEqual(c.stemmer, "grof")
            self.assertFalse(c.snippets)

    def test_ongeldige_stemmer_is_fout(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                _config(d, extra_algemeen='stemmer = "engels"\n')

    def test_zonder_bronnen_en_zonder_stukken_is_de_bouw_fout(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(d, bronnen="")
            self.assertEqual(cfg.bronnen, [])
            with self.assertRaises(ValueError):
                index.bouw(cfg, verbose=False, embedder=NepEmbedder())
            n = index.bouw(cfg, verbose=False, embedder=NepEmbedder(),
                           extra_stukken=[{"bron": "x", "pad": "y", "tekst": "alleen aangeleverd"}])
            self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
