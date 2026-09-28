# -*- coding: utf-8 -*-
"""polaris.eval - meet of de index vindt wat hij moet vinden.

Zonder meetlat is elke wijziging aan chunking, gewichten of model een gok. Een
evaluatieset is een JSON-lijst van vragen met wat er (minstens) in de top-k moet staan:

    [
      {"vraag": "access point krijgt geen stroom", "verwacht": "taken\\.json#TK-1"},
      {"vraag": "hoe zet ik een back-up terug",   "verwacht": "back-up-procedure", "bron": "documenten"},
      {"vraag": "wie neemt Bakkerij Vermeulen over", "verwacht": "verlof-karin", "k": 3,
       "verwacht_alle": ["Samenvatting", "Waarneming per klant"]}
    ]

`verwacht` is een reguliere expressie die op het pad óf de titel van een treffer moet
passen. Per vraag telt de plek van de eerste passende treffer; de maat over de hele set
is de Mean Reciprocal Rank (MRR): gemiddelde van 1/plek, met 0 voor niet gevonden.
MRR 1,0 = alles bovenaan; 0,5 = gemiddeld op plek 2. `k` per vraag overschrijft de k
van de hele run.

`verwacht_alle` is een tweede soort controle: staan álle genoemde stukken in de uitkomst?
Elke expressie moet passen op het pad, de titel of de sectie van een treffer, of van een
stuk dat via `ook_in_dit_document` meekomt. Zo is te meten of een document dat raak is
ook zijn andere relevante stukken levert, bijvoorbeeld een samenvatting én de tabel die
haar tegenspreekt.

De code hoort in de repo; de vragen horen bij de kennisbank waar ze over gaan.
"""
import io
import json
import re
import time

from . import index as indexmod


def laad_vragen(pad):
    with io.open(pad, encoding="utf-8") as f:
        vragen = json.load(f)
    if not isinstance(vragen, list):
        raise ValueError("evaluatieset moet een JSON-lijst zijn")
    for i, v in enumerate(vragen):
        if not isinstance(v, dict) or not v.get("vraag") or not v.get("verwacht"):
            raise ValueError("vraag %d mist 'vraag' of 'verwacht'" % (i + 1))
        alle = v.get("verwacht_alle", [])
        if not isinstance(alle, list):
            raise ValueError("vraag %d: 'verwacht_alle' moet een lijst zijn" % (i + 1))
        for expressie in [v["verwacht"]] + alle:
            try:
                re.compile(expressie)
            except re.error as e:
                raise ValueError("vraag %d: ongeldige expressie %r (%s)" % (i + 1, expressie, e))
        if "k" in v and not (isinstance(v["k"], int) and v["k"] > 0):
            raise ValueError("vraag %d: 'k' moet een positief geheel getal zijn" % (i + 1))
    return vragen


def _compleet(patronen, treffers):
    """Welke van de patronen niet terug te vinden zijn in de treffers of hun aanvulling."""
    velden = []
    for t in treffers:
        velden.append((t.get("pad") or "", t.get("titel") or "", t.get("sectie") or ""))
        for extra in t.get("ook_in_dit_document", []):
            velden.append((t.get("pad") or "", t.get("titel") or "", extra.get("sectie") or ""))
    return [p for p in patronen
            if not any(re.search(p, v, re.I) for rij in velden for v in rij)]


def evalueer(config, vragen, k=8, zoeker=None):
    """Geeft een dict met per vraag de plek (0 = niet in de top-k) en de totalen."""
    zoeker = zoeker or indexmod.Zoeker(config)
    uitkomsten = []
    start = time.time()
    for v in vragen:
        patroon = re.compile(v["verwacht"], re.I)
        k_vraag = v.get("k", k)
        treffers = zoeker.zoek(v["vraag"], k=k_vraag, bron=v.get("bron"))
        plek = 0
        for i, t in enumerate(treffers, 1):
            if patroon.search(t["pad"] or "") or patroon.search(t["titel"] or ""):
                plek = i
                break
        u = {"vraag": v["vraag"], "verwacht": v["verwacht"], "k": k_vraag, "plek": plek,
             "bovenaan": (treffers[0]["pad"] if treffers else "")}
        if v.get("verwacht_alle"):
            u["ontbreekt"] = _compleet(v["verwacht_alle"], treffers)
            u["compleet"] = not u["ontbreekt"]
        uitkomsten.append(u)
    n = len(uitkomsten)
    gevonden = sum(1 for u in uitkomsten if u["plek"])
    mrr = (sum(1.0 / u["plek"] for u in uitkomsten if u["plek"]) / n) if n else 0.0
    compleet = [u["compleet"] for u in uitkomsten if "compleet" in u]
    return {"k": k, "vragen": n, "gevonden": gevonden, "mrr": round(mrr, 3),
            "compleet": sum(compleet), "compleet_van": len(compleet),
            "seconden": round(time.time() - start, 1), "per_vraag": uitkomsten}


def rapport(uit):
    regels = []
    for u in uit["per_vraag"]:
        merk = "%2d" % u["plek"] if u["plek"] else " -"
        regels.append("%s  %s" % (merk, u["vraag"]))
        if not u["plek"]:
            regels.append("     verwacht %s in de top-%d, bovenaan stond: %s"
                          % (u["verwacht"], u["k"], u["bovenaan"] or "niets"))
        if u.get("ontbreekt"):
            regels.append("     niet compleet, ontbreekt: %s" % ", ".join(u["ontbreekt"]))
    regels.append("")
    regel = ("gevonden: %d/%d   MRR %.3f" % (uit["gevonden"], uit["vragen"], uit["mrr"]))
    if uit["compleet_van"]:
        regel += "   compleet: %d/%d" % (uit["compleet"], uit["compleet_van"])
    regels.append(regel + "   (top-%d, %.1f s)" % (uit["k"], uit["seconden"]))
    return "\n".join(regels)
