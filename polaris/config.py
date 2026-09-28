# -*- coding: utf-8 -*-
"""Configuratie voor een Polaris-installatie: welke bronnen, welk model, waar de index staat.

Eén config-bestand (TOML) per installatie houdt de kern generiek: dezelfde `polaris`-code
draait op elke kennisbank - alleen `polaris.toml` verschilt.

Relatieve paden (bronnen, database, model) worden opgelost ten opzichte van de map waar
het configbestand staat, niet ten opzichte van de map waarin je het commando uitvoert.
Zo blijft een installatie werken als je hem verplaatst of vanuit een andere map aanroept.
"""
import os
import re

try:
    import tomllib
except ImportError:                       # Python 3.10: tomli is dezelfde API
    import tomli as tomllib

STANDAARD_MODEL = "intfloat/multilingual-e5-base"
STANDAARD_MODELBESTAND = "onnx/model_qint8_avx512_vnni.onnx"
STANDAARD_DIM = 768

# Standaard herkent Polaris ID's in de vorm AB-123 als verwijzing naar een extern
# systeem. Eigen formaten (bijvoorbeeld datumgebaseerde ticketnummers) voeg je toe via
# `verwijzingspatronen` in de config.
STANDAARD_VERWIJZINGSPATRONEN = [r"\b[A-Z]{2,10}-\d{1,6}\b"]


def _pad(pad, basis):
    pad = os.path.expanduser(pad)
    return pad if os.path.isabs(pad) else os.path.normpath(os.path.join(basis, pad))


def _lijst(waarde):
    if waarde is None:
        return []
    if isinstance(waarde, str):
        return [waarde]
    return list(waarde)


class BronConfig(object):
    """Eén bron: een map met markdown, of een JSON-lijst met een generiek veldschema."""

    def __init__(self, ruw, basis):
        self.naam = ruw.get("naam") or ruw.get("type", "bron")
        self.type = ruw.get("type", "markdown")
        if "pad" not in ruw:
            raise ValueError("bron %r heeft geen 'pad'" % self.naam)
        self.pad = _pad(ruw["pad"], basis)

        # Beveiliging: wat wel en niet in de index mag. Patronen zijn glob-patronen op
        # het relatieve pad binnen de bron (bijvoorbeeld "hr/**" of "*contract*").
        # `alleen` leeg = alles mag, tenzij uitgesloten. Uitsluiten wint altijd.
        self.alleen = _lijst(ruw.get("alleen"))
        self.uitsluiten = _lijst(ruw.get("uitsluiten"))

        # Voor type "json_lijst": welk veld in het JSON-bestand de lijst items bevat
        # ("" betekent: het hele bestand is al een lijst), en hoe de velden van elk
        # item heten - dat verschilt per systeem ("titel"/"detail" bij het ene,
        # "name"/"description" bij het andere).
        self.lijst_veld = ruw.get("lijst_veld", "")
        self.id_veld = ruw.get("id_veld", "id")
        self.titel_veld = ruw.get("titel_veld", "titel")
        self.tekst_veld = ruw.get("tekst_veld", "detail")
        self.status_veld = ruw.get("status_veld", "status")
        self.datum_veld = ruw.get("datum_veld", "")
        self.sectie_veld = ruw.get("sectie_veld", "")
        # Items waarvan dit veld een ware waarde heeft (true, "ja", 1) worden overgeslagen,
        # bijvoorbeeld een veld "vertrouwelijk".
        self.uitsluit_veld = ruw.get("uitsluit_veld", "")
        # `alle_velden = true`: niet alleen `tekst_veld`, maar elk veld van het item als
        # `sleutel: waarde`-regel. Voor lijsten waar de kennis over meer velden verspreid
        # zit (prijs, leverancier, opzegdatum, ...).
        self.alle_velden = bool(ruw.get("alle_velden", False))
        # Per lijst één extra overzichtsstuk met alle namen, voor "welke ... zijn er"-vragen.
        self.overzicht = bool(ruw.get("overzicht", True))


class Config(object):
    def __init__(self, ruw, basis):
        alg = ruw.get("algemeen", {})
        self.model_repo = alg.get("model", STANDAARD_MODEL)
        self.model_bestand = alg.get("model_bestand", STANDAARD_MODELBESTAND)
        self.dim = int(alg.get("dim", STANDAARD_DIM))
        self.db_pad = _pad(alg.get("db_pad", "polaris_index.db"), basis)
        self.model_cache = _pad(alg.get("model_cache", os.path.join(
            os.path.expanduser("~"), ".cache", "polaris-modellen")), basis)
        # Optioneel: een map met een lokaal gekopieerd model (met daarin onnx/...). Dan
        # wordt er nooit iets gedownload - bedoeld voor machines zonder internet.
        model_pad = alg.get("model_pad", "")
        self.model_pad = _pad(model_pad, basis) if model_pad else ""

        # Beveiliging: wachtwoorden, tokens en sleutels maskeren vóór ze in de index
        # komen. Staat standaard aan; uitzetten moet een bewuste keuze zijn.
        self.maskeer_geheimen = bool(alg.get("maskeer_geheimen", True))

        # Actualiteit: tot `actualiteit_bonus` × (de score van een eerste plek) extra voor
        # een stuk van vandaag, lineair aflopend naar 0 na `actualiteit_dagen`. 0 = uit.
        self.actualiteit_bonus = float(alg.get("actualiteit_bonus", 0.25))
        self.actualiteit_dagen = int(alg.get("actualiteit_dagen", 730))

        # Aantal CPU-threads voor het model; 0 = alle kernen.
        self.threads = int(alg.get("threads", 0))

        # Woordstammen bij het zoeken: "grof" (woorden van 7+ tekens verliezen 3 letters en
        # krijgen een wildcard; geen afhankelijkheid), "nl" (Snowball-stemmer voor Nederlands,
        # vraagt `pip install snowballstemmer` en een herbouw van de index) of "uit".
        self.stemmer = str(alg.get("stemmer", "grof")).lower()
        if self.stemmer not in ("grof", "nl", "uit"):
            raise ValueError("stemmer moet 'grof', 'nl' of 'uit' zijn, niet %r" % self.stemmer)

        # Snippets: per treffer een fragment rond de gevonden woorden, met de markering
        # eromheen (standaard [ en ]). Kost niets extra's; SQLite maakt ze.
        self.snippets = bool(alg.get("snippets", False))
        markering = alg.get("snippet_markering", ["[", "]"])
        if not (isinstance(markering, list) and len(markering) == 2):
            raise ValueError("snippet_markering moet een lijst van twee tekens zijn")
        self.snippet_markering = (str(markering[0]), str(markering[1]))

        # Namen: eigennamen en afkortingen uit de vraag krijgen elk een eigen woordzoektocht
        # in de fusie, met dit gewicht; een stuk met álle namen krijgt er nog een lijst bij.
        # Het betekenisdeel ziet "over hetzelfde onderwerp" als raak, ook als het de klant
        # uit de vraag niet noemt. 0 = uit.
        self.namen_gewicht = float(alg.get("namen_gewicht", 0.7))

        # Aanvullen: is een document raak, dan komen er tot zoveel andere stukken van dat
        # document mee die de namen of woorden uit de vraag bevatten (veld
        # `ook_in_dit_document`). Een samenvatting en een tabel verderop die elkaar
        # tegenspreken, staan zo naast elkaar. 0 = uit.
        self.aanvullen = int(alg.get("aanvullen", 2))
        if self.aanvullen < 0:
            raise ValueError("aanvullen moet 0 of meer zijn, niet %r" % self.aanvullen)

        patronen = alg.get("verwijzingspatronen", STANDAARD_VERWIJZINGSPATRONEN)
        try:
            self.verwijzingspatronen = [re.compile(p) for p in _lijst(patronen)]
        except re.error as e:
            raise ValueError("ongeldig verwijzingspatroon in de config: %s" % e)

        # Geen [[bron]] mag: een aanroepend programma kan alle stukken zelf aanleveren
        # (extra_stukken). De bouw weigert pas als er dan óók niets aangeleverd is.
        self.bronnen = [BronConfig(b, basis) for b in ruw.get("bron", [])]

    def vingerafdruk(self):
        """Alles wat bepaalt wat er in de index staat. Verandert dit, dan kan `ververs` niet
        op de bestaande index voortbouwen en wordt het een volledige bouw."""
        import hashlib
        import json
        from . import versie
        # De versie hoort erbij: een nieuwe versie kan anders knippen of maskeren.
        deel = {"versie": versie(), "model": self.model_repo, "dim": self.dim, "stemmer": self.stemmer,
                "maskeer": self.maskeer_geheimen,
                "patronen": [p.pattern for p in self.verwijzingspatronen],
                "bronnen": [sorted(vars(b).items()) for b in self.bronnen]}
        return hashlib.sha1(json.dumps(deel, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def laad(pad):
    with open(pad, "rb") as f:
        ruw = tomllib.load(f)
    return Config(ruw, os.path.dirname(os.path.abspath(pad)))
