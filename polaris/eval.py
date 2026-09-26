# -*- coding: utf-8 -*-
"""polaris.eval - meet of de index vindt wat hij moet vinden.

Zonder meetlat is elke wijziging aan chunking, gewichten of model een gok. Een
evaluatieset is een JSON-lijst van vragen met wat er (minstens) in de top-k moet staan:

    [
      {"vraag": "access point krijgt geen stroom", "verwacht": "taken\\.json#TK-1"},
      {"vraag": "hoe zet ik een back-up terug",   "verwacht": "back-up-procedure", "bron": "documenten"}
    ]

`verwacht` is een reguliere expressie die op het pad óf de titel van een treffer moet
passen. Per vraag telt de plek van de eerste passende treffer; de maat over de hele set
is de Mean Reciprocal Rank (MRR): gemiddelde van 1/plek, met 0 voor niet gevonden.
MRR 1,0 = alles bovenaan; 0,5 = gemiddeld op plek 2.

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
        try:
            re.compile(v["verwacht"])
        except re.error as e:
            raise ValueError("vraag %d: ongeldige expressie %r (%s)" % (i + 1, v["verwacht"], e))
    return vragen


def evalueer(config, vragen, k=8, zoeker=None):
    """Geeft een dict met per vraag de plek (0 = niet in de top-k) en de totalen."""
    zoeker = zoeker or indexmod.Zoeker(config)
    uitkomsten = []
    start = time.time()
    for v in vragen:
        patroon = re.compile(v["verwacht"], re.I)
        treffers = zoeker.zoek(v["vraag"], k=k, bron=v.get("bron"))
        plek = 0
        for i, t in enumerate(treffers, 1):
            if patroon.search(t["pad"] or "") or patroon.search(t["titel"] or ""):
                plek = i
                break
        uitkomsten.append({"vraag": v["vraag"], "verwacht": v["verwacht"], "plek": plek,
                           "bovenaan": (treffers[0]["pad"] if treffers else "")})
    n = len(uitkomsten)
    gevonden = sum(1 for u in uitkomsten if u["plek"])
    mrr = (sum(1.0 / u["plek"] for u in uitkomsten if u["plek"]) / n) if n else 0.0
    return {"k": k, "vragen": n, "gevonden": gevonden, "mrr": round(mrr, 3),
            "seconden": round(time.time() - start, 1), "per_vraag": uitkomsten}


def rapport(uit):
    regels = []
    for u in uit["per_vraag"]:
        merk = "%2d" % u["plek"] if u["plek"] else " -"
        regels.append("%s  %s" % (merk, u["vraag"]))
        if not u["plek"]:
            regels.append("     verwacht %s, bovenaan stond: %s" % (u["verwacht"], u["bovenaan"] or "niets"))
    regels.append("")
    regels.append("gevonden in top-%d: %d/%d   MRR %.3f   (%.1f s)"
                  % (uit["k"], uit["gevonden"], uit["vragen"], uit["mrr"], uit["seconden"]))
    return "\n".join(regels)
