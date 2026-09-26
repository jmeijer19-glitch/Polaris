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
import tomllib

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

        patronen = alg.get("verwijzingspatronen", STANDAARD_VERWIJZINGSPATRONEN)
        try:
            self.verwijzingspatronen = [re.compile(p) for p in _lijst(patronen)]
        except re.error as e:
            raise ValueError("ongeldig verwijzingspatroon in de config: %s" % e)

        self.bronnen = [BronConfig(b, basis) for b in ruw.get("bron", [])]
        if not self.bronnen:
            raise ValueError("geen [[bron]] secties in de config - er is niets om te indexeren")


def laad(pad):
    with open(pad, "rb") as f:
        ruw = tomllib.load(f)
    return Config(ruw, os.path.dirname(os.path.abspath(pad)))
