# -*- coding: utf-8 -*-
"""polaris.index - de hybride zoekindex zelf: chunking, embeddings, FTS5, RRF-fusie.

De bronnen komen uit een Config-object (polaris/config.py), niet uit hardcoded paden -
dezelfde motor, andere kennisbank per installatie. Zie docs/techniek.md voor de
onderbouwing van elke keuze hieronder.

Pijplijn, kort:
1. Verzamelen: markdown op koppen geknipt (met de frontmatter-omschrijving als context
   vóór elk stuk), JSON-lijsten één stuk per item plus één overzichtsstuk per lijst.
   Wat niet mag (uitgesloten pad, opt-out, uitsluit_veld) komt er niet in; geheimen
   worden gemaskeerd (polaris/beveiliging.py). Een aanroepend programma kan eigen
   stukken meegeven (`extra_stukken`) voor bronnen die Polaris zelf niet kent.
2. Opslag: één SQLite-bestand met een FTS5-tabel (woorden) en een vectortabel
   (betekenis), bij elke bouw atomisch vervangen; een lock voorkomt twee bouwen tegelijk.
   `ververs` werkt dezelfde index in één transactie bij: alleen wat veranderd is.
3. Embeddings: een e5-model, int8-ONNX, lokaal op de CPU. Titel en kopjespad gaan mee
   in de invoer, anders weet een losse sectie niet waar hij over gaat.
4. Zoeken: woordlijst (BM25), betekenislijst (cosinus) en een woordlijst per naam uit de
   vraag, samengevoegd met Reciprocal Rank Fusion, daarna herrangschikt op status en
   actualiteit. Een raak document levert zijn andere relevante stukken mee. De `Zoeker`
   houdt model en vectormatrix warm, zodat een langlopend proces per vraag alleen het
   zoeken zelf betaalt.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime

import numpy as np

from . import beveiliging

STOPWOORDEN = set("""
de het een en of maar want dus als dan dat die deze dit is was zijn ben bent wordt
worden er wij jij hij zij ik je u we ze op in aan van voor met bij naar om over uit
niet geen nog wel ook al maar toch al te tot per zo hoe wie wat welke welk
""".split())

# Woorden die met een hoofdletter midden in een vraag kunnen staan zonder een naam te zijn.
VRAAGWOORDEN = set("""
wie wat waar wanneer waarom hoe hoeveel welke welk waarmee waarover wiens
who what where when why how which whose
""".split())

VERVALLEN_STATUSSEN = {"klaar", "afgerond", "gesloten", "vervallen", "vervangen",
                       "done", "closed", "deprecated"}

# Chunking
MIN_SECTIE = 300
MAX_STUK = 1800

# Zoeken
RRF_K = 60
KANDIDATEN = 40
# BM25-gewichten per FTS-kolom: id (niet geïndexeerd), titel, sectie, tekst, stammen.
BM25_GEWICHTEN = (0.0, 6.0, 3.0, 1.0, 1.0)

# Versie van de indexstructuur. Hoog dit op als de tabellen of de embed-invoer
# veranderen: een oude index wordt dan met een duidelijke melding geweigerd.
SCHEMA = "4"

# Een bouw-lock ouder dan dit is van een afgebroken proces en mag weg.
LOCK_VEROUDERD_S = 3600

_KOPREGEX = re.compile(r"^(#{2,4})\s+(.*)$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)
_FTS_SYNTAX = re.compile(r'[*"^:()]|\b(AND|OR|NOT|NEAR)\b')
_ZONDER_SYNTAX = re.compile(r'[*"^:()]|\b(?:AND|OR|NOT|NEAR)\b')


class BouwBezig(Exception):
    """Er loopt al een bouw voor deze index."""


# ---------------------------------------------------------------- stammen

_SNOWBALL = {}


def _snowball():
    """De Nederlandse Snowball-stemmer, lui geladen. Alleen nodig bij stemmer = "nl"."""
    if "nl" not in _SNOWBALL:
        try:
            import snowballstemmer
        except ImportError:
            raise ImportError("stemmer = \"nl\" vraagt het pakket snowballstemmer: "
                              "pip install snowballstemmer")
        _SNOWBALL["nl"] = snowballstemmer.stemmer("dutch")
    return _SNOWBALL["nl"]


def woorden(tekst):
    """Zelfde woordgrens als de FTS-tokenizer (unicode61): reeksen letters en cijfers."""
    return re.findall(r"\w+", tekst.lower())


def stammen(tekst):
    """De tekst als reeks Nederlandse stammen (Snowball), voor de stam-kolom in de index
    en voor de vraag. Stam tegen stam, exact: "betalingen" en "betaald" worden allebei
    "betaal", wat als voorvoegsel-wildcard nooit was gelukt."""
    st = _snowball()
    return " ".join(st.stemWords(woorden(tekst)))


_TOKEN = re.compile(r"\w+(?:[-'’]\w+)*")


def namen(vraag):
    """De eigennamen en afkortingen uit een vraag, als lijsten woorden (zoals de index ze
    ziet). Een naam is een woord met een hoofdletter dat niet vooraan een zin staat, of een
    woord helemaal in hoofdletters; aaneengesloten namenwoorden vormen één naam
    ("Bakkerij Vermeulen"). Vraagwoorden en stopwoorden tellen niet mee.

    Bewust eenvoudig: een naam vooraan een zin wordt gemist, en een vraag in kleine
    letters heeft geen namen. Dan verandert er niets aan de zoektocht."""
    uit, huidige = [], []
    zinsbegin, vorige_eind = True, 0
    for m in _TOKEN.finditer(vraag):
        tussen = vraag[vorige_eind:m.start()]
        if re.search(r"[.!?:;]", tussen):
            zinsbegin = True
        t = m.group(0)
        letters = [c for c in t if c.isalpha()]
        afkorting = len(letters) >= 2 and all(c.isupper() for c in letters)
        naam = (len(t) >= 2 and t.lower() not in STOPWOORDEN and t.lower() not in VRAAGWOORDEN
                and (afkorting or (t[0].isupper() and not zinsbegin)))
        # "Noordwind-contract": alleen "Noordwind" is de naam. Een samenstelling houdt de
        # delen met een hoofdletter of een cijfer ("TK-1" blijft heel).
        delen = [d for d in re.split(r"[-'’]", t) if d and (d[0].isupper() or d.isdigit())]
        naamwoorden = woorden(" ".join(delen))
        if naam and huidige and not tussen.strip():
            huidige.extend(naamwoorden)
        else:
            if huidige:
                uit.append(huidige)
            huidige = naamwoorden if naam else []
        zinsbegin, vorige_eind = False, m.end()
    if huidige:
        uit.append(huidige)
    gezien, uniek = set(), []
    for n in uit:
        if tuple(n) not in gezien:
            gezien.add(tuple(n))
            uniek.append(n)
    return uniek


def _frase(woordenlijst):
    """Een naam als FTS5-frase, alleen op de leesbare kolommen (niet de stam-kolom)."""
    return '{titel sectie tekst} : "%s"' % " ".join(woordenlijst)


def _verwijzingen(tekst, patronen, eigen_id=""):
    gevonden = set()
    for p in patronen:
        gevonden.update(p.findall(tekst))
    gevonden.discard(eigen_id)
    return ",".join(sorted(g for g in gevonden if g))


# ---------------------------------------------------------------- frontmatter

def frontmatter(tekst):
    """Leest een eenvoudige YAML-achtige frontmatter (`sleutel: waarde` per regel).

    Geeft (velden, romp) terug. Geen YAML-bibliotheek: geneste structuren zijn hier niet
    nodig, en een extra afhankelijkheid voor een paar losse velden is het niet waard.
    Waarden mogen tussen aanhalingstekens staan; die worden verwijderd.
    """
    m = _FRONTMATTER.match(tekst)
    if not m:
        return {}, tekst
    velden = {}
    for regel in m.group(1).splitlines():
        if ":" not in regel or regel.startswith((" ", "\t")):
            continue
        sleutel, _, waarde = regel.partition(":")
        waarde = waarde.strip().strip('"').strip("'")
        velden[sleutel.strip().lower()] = waarde
    return velden, tekst[m.end():]


def _eerste(velden, *namen):
    for n in namen:
        if velden.get(n):
            return velden[n]
    return ""


# ---------------------------------------------------------------- chunking

def chunk_markdown(tekst, titel_bestand):
    """Knip op ##..#### koppen, met het kopjespad als sectienaam.

    Koppen binnen een codeblok tellen niet mee. Secties korter dan MIN_SECTIE tekens gaan
    op in hun buur; secties langer dan MAX_STUK worden op alinea's geknipt.
    """
    regels = tekst.split("\n")
    in_code = False
    secties = []
    pad_stapel = []
    huidige = []

    for r in regels:
        if r.strip().startswith("```"):
            in_code = not in_code
            huidige.append(r)
            continue
        m = None if in_code else _KOPREGEX.match(r)
        if m:
            if huidige:
                secties.append((list(pad_stapel), huidige))
            huidige = []
            niveau = len(m.group(1)) - 2
            pad_stapel = pad_stapel[:niveau] + [m.group(2).strip()]
        else:
            huidige.append(r)
    if huidige:
        secties.append((list(pad_stapel), huidige))

    stukken = []
    for pad, inhoud in secties:
        t = "\n".join(inhoud).strip()
        if t:
            stukken.append([" › ".join(pad) if pad else titel_bestand, t])

    samengevoegd = []
    for sectie, t in stukken:
        if samengevoegd and len(t) < MIN_SECTIE:
            samengevoegd[-1][1] += "\n\n" + t
        else:
            samengevoegd.append([sectie, t])
    if len(samengevoegd) > 1 and len(samengevoegd[0][1]) < MIN_SECTIE:
        samengevoegd[1][1] = samengevoegd[0][1] + "\n\n" + samengevoegd[1][1]
        samengevoegd.pop(0)

    uit = []
    for sectie, t in samengevoegd:
        uit.extend((sectie, deel) for deel in _knip(t, MAX_STUK))
    return uit


def _knip(t, maxlen):
    """Knip tekst op alinea's in delen van hoogstens maxlen tekens."""
    if len(t) <= maxlen:
        return [t]
    uit = []
    buf = ""
    for alinea in t.split("\n\n"):
        if buf and len(buf) + len(alinea) > maxlen:
            uit.append(buf.strip())
            buf = alinea
        else:
            buf = (buf + "\n\n" + alinea) if buf else alinea
    if buf.strip():
        uit.append(buf.strip())
    return uit


def _stuk_id(*delen):
    return hashlib.sha1("|".join(str(d) for d in delen).encode("utf-8")).hexdigest()[:12]


def _datum(pad, tekst):
    m = (re.search(r"(20\d{2})-(\d{2})-(\d{2})", pad)
         or re.search(r"(20\d{2})-(\d{2})-(\d{2})", tekst[:400]))
    if m:
        return "%s-%s-%s" % m.groups()
    try:
        return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(pad)))
    except OSError:
        return ""


def leesbaar(waarde, inspring=""):
    """Een JSON-item als `sleutel: waarde`-regels in plaats van ruwe JSON.

    Accolades en aanhalingstekens zijn ruis voor zowel BM25 als het embeddingmodel;
    een lijst van leesbare regels is voor beide beter.
    """
    if isinstance(waarde, dict):
        regels = []
        for k, v in waarde.items():
            plat = isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)
            if isinstance(v, (dict, list)) and v and not plat:
                regels.append("%s%s:" % (inspring, k))
                regels.append(leesbaar(v, inspring + "  "))
            elif v not in (None, "", [], {}):
                regels.append("%s%s: %s" % (inspring, k, leesbaar(v)))
        return "\n".join(regels)
    if isinstance(waarde, list):
        if all(not isinstance(x, (dict, list)) for x in waarde):
            return inspring + ", ".join(str(x) for x in waarde)
        return "\n".join("%s- %s" % (inspring, leesbaar(x, inspring + "  ").strip())
                         for x in waarde)
    if isinstance(waarde, bool):
        return "ja" if waarde else "nee"
    return str(waarde)


# ---------------------------------------------------------------- bronverzamelaars

class Telling(object):
    """Houdt bij wat er is overgeslagen en gemaskeerd, zodat de bouw dat kan melden."""

    def __init__(self, hergebruik=None):
        self.uitgesloten = 0
        self.optout = 0
        self.gemaskeerd = 0
        # Per markdown-bestand (bron, pad): [mtime_ns, grootte, stuk-ID's die het opleverde].
        # `ververs` vergelijkt dit met de schijf; wat gelijk is, hoeft niet opnieuw gelezen.
        self.bestanden = {}
        # Van `ververs`: {(bron, pad): (mtime_ns, grootte, [opgeslagen stukken])}.
        self.hergebruik = hergebruik or {}
        # Van `ververs`: {stuk-ID: (vingerafdruk van het ruwe stuk, opgeslagen stuk)} voor
        # aangeleverde stukken; gelijk ruw stuk = niet opnieuw maskeren.
        self.hergebruik_extra = {}
        self.gelezen = 0
        self.hergebruikt = 0

    def maskeer(self, tekst, config):
        if not config.maskeer_geheimen:
            return tekst
        tekst, n = beveiliging.maskeer(tekst)
        self.gemaskeerd += n
        return tekst


def _stukken_markdown(bron, config, telling):
    uit = []
    if not os.path.isdir(bron.pad):
        print("waarschuwing: bron %r - map bestaat niet: %s" % (bron.naam, bron.pad))
        return uit
    for dirpad, dirs, files in os.walk(bron.pad):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("."):
                continue
            vol = os.path.join(dirpad, f)
            rel = os.path.relpath(vol, bron.pad).replace("\\", "/")
            if not beveiliging.mag_indexeren(rel, bron.alleen, bron.uitsluiten):
                telling.uitgesloten += 1
                continue
            try:
                st = os.stat(vol)
            except OSError:
                continue
            sleutel = (bron.naam, rel)
            oud = telling.hergebruik.get(sleutel)
            if oud is not None and oud[:2] == (st.st_mtime_ns, st.st_size):
                telling.hergebruikt += 1
                telling.bestanden[sleutel] = [st.st_mtime_ns, st.st_size, [s["id"] for s in oud[2]]]
                uit.extend(oud[2])
                continue
            try:
                with io.open(vol, encoding="utf-8") as fh:
                    tekst = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            telling.gelezen += 1
            nieuw = []
            if tekst.strip() and beveiliging.heeft_optout(tekst):
                telling.optout += 1
            elif tekst.strip():
                tekst = telling.maskeer(tekst, config)
                nieuw = stukken_uit_markdown(bron.naam, rel, tekst, config,
                                             datum=_datum(vol, tekst), bestand=rel)
            telling.bestanden[sleutel] = [st.st_mtime_ns, st.st_size, [s["id"] for s in nieuw]]
            uit.extend(nieuw)
    return uit


def stukken_uit_markdown(bron_naam, rel, tekst, config, datum="", bestand=""):
    """Eén markdown-document naar stukken. Ook bruikbaar voor tekst die niet uit een
    bestand komt (een aanroepend programma dat zelf bronnen aanlevert). `bestand` is het
    bronbestand waar de stukken uit komen; `ververs` gebruikt het om een ongewijzigd
    bestand niet opnieuw te lezen.

    Frontmatter: `description`/`omschrijving` gaat vóór elk stuk (een losse sectie
    houdt zo zijn context), `status` wordt de status van alle stukken, `titel`/`title`/
    `name` overschrijft de bestandsnaam als titel, `datum`/`date` de datum.
    """
    velden, romp = frontmatter(tekst)
    titel = _eerste(velden, "titel", "title", "name", "naam") or os.path.splitext(
        os.path.basename(rel))[0]
    status = _eerste(velden, "status")
    datum = _eerste(velden, "datum", "date") or datum
    omschrijving = _eerste(velden, "description", "omschrijving", "beschrijving")
    uit = []
    for volgnr, (sectie, chunk) in enumerate(chunk_markdown(romp, titel)):
        if omschrijving and sectie != titel:
            chunk = omschrijving + "\n\n" + chunk
        uit.append({
            "id": _stuk_id(bron_naam, rel, sectie, volgnr),
            "bron": bron_naam,
            "pad": rel,
            "titel": titel,
            "sectie": sectie,
            "datum": datum,
            "status": status,
            "tekst": chunk,
            "verwijzingen": _verwijzingen(chunk, config.verwijzingspatronen),
            "volgnr": volgnr,
            "bestand": bestand,
        })
    if not uit and omschrijving:
        uit.append({
            "id": _stuk_id(bron_naam, rel, "", 0), "bron": bron_naam, "pad": rel,
            "titel": titel, "sectie": "", "datum": datum, "status": status,
            "tekst": omschrijving,
            "verwijzingen": _verwijzingen(omschrijving, config.verwijzingspatronen),
            "volgnr": 0, "bestand": bestand,
        })
    return uit


def _stukken_json_lijst(bron, config, telling):
    if not os.path.exists(bron.pad):
        print("waarschuwing: bron %r - bestand bestaat niet: %s" % (bron.naam, bron.pad))
        return []
    with io.open(bron.pad, encoding="utf-8") as f:
        d = json.load(f)
    items = d.get(bron.lijst_veld) if bron.lijst_veld else d
    if not isinstance(items, list):
        raise ValueError("bron %r: verwacht een lijst op lijst_veld=%r, kreeg %s"
                         % (bron.naam, bron.lijst_veld, type(items).__name__))

    uit = []
    overzicht = []
    bestand = os.path.basename(bron.pad)
    for volgnr, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if bron.uitsluit_veld and beveiliging.is_waar(item.get(bron.uitsluit_veld)):
            telling.optout += 1
            continue
        item_id = str(item.get(bron.id_veld, "") or "")
        titel = str(item.get(bron.titel_veld, "") or "")
        status = str(item.get(bron.status_veld, "") or "")
        sectie = str(item.get(bron.sectie_veld, "") or "") if bron.sectie_veld else ""
        if bron.alle_velden:
            rest = {k: v for k, v in item.items()
                    if k not in (bron.id_veld, bron.titel_veld, bron.uitsluit_veld)}
            lichaam = leesbaar(rest)
        else:
            lichaam = str(item.get(bron.tekst_veld, "") or "")
        tekst = telling.maskeer("%s\n\n%s" % (titel, lichaam), config)
        uit.append({
            "id": _stuk_id(bron.naam, item_id or volgnr),
            "bron": bron.naam,
            "pad": "%s#%s" % (bestand, item_id or volgnr),
            "titel": "%s - %s" % (item_id, titel) if item_id else titel,
            "sectie": sectie,
            "datum": str(item.get(bron.datum_veld, "") or "") if bron.datum_veld else "",
            "status": status,
            "tekst": tekst,
            "verwijzingen": _verwijzingen(tekst, config.verwijzingspatronen, item_id),
            "volgnr": 0,
            "bestand": bestand,
        })
        regel = " - ".join(x for x in (item_id, titel) if x)
        extra = ", ".join(x for x in (sectie, status) if x)
        overzicht.append(regel + (" (%s)" % extra if extra else ""))

    # Overzichtsstuk: alle namen van de lijst bij elkaar. Zonder dit is een vraag als
    # "welke taken zijn er over back-ups" kansloos: het antwoord ligt verspreid over
    # tientallen stukken die elk maar één item kennen.
    if bron.overzicht and len(overzicht) > 1:
        kop = "%s - overzicht (%d items)" % (bron.naam, len(overzicht))
        for volgnr, deel in enumerate(_knip("\n".join(overzicht), MAX_STUK * 2)):
            uit.append({
                "id": _stuk_id(bron.naam, "overzicht", volgnr),
                "bron": bron.naam,
                "pad": "%s#overzicht" % bestand,
                "titel": kop,
                "sectie": "overzicht",
                "datum": "",
                "status": "",
                "tekst": telling.maskeer(deel, config),
                "verwijzingen": "",
                "volgnr": volgnr,
                "bestand": bestand,
            })
    return uit


def verzamel_stukken(config, telling=None, extra_stukken=None):
    telling = telling or Telling()
    stukken = []
    for bron in config.bronnen:
        if bron.type == "markdown":
            stukken += _stukken_markdown(bron, config, telling)
        elif bron.type == "json_lijst":
            stukken += _stukken_json_lijst(bron, config, telling)
        else:
            raise ValueError("onbekend brontype %r bij bron %r" % (bron.type, bron.naam))
    per_document = {}
    for s in extra_stukken or []:
        stuk = _normaliseer_stuk(s, config, telling, per_document)
        stukken.append(stuk)
    return _ontdubbel(stukken)


_VELDEN = ("id", "bron", "pad", "titel", "sectie", "datum", "status", "tekst", "verwijzingen")


def _normaliseer_stuk(s, config, telling, per_document=None):
    """Een stuk van een aanroepend programma: `bron`, `pad` en `tekst` zijn verplicht,
    de rest is optioneel. Krijgt dezelfde maskering als eigen bronnen. `volgnr` (de plek
    in het document) is standaard de volgorde waarin stukken met dezelfde bron en hetzelfde
    pad worden aangeleverd."""
    for veld in ("bron", "pad", "tekst"):
        if not s.get(veld):
            raise ValueError("extra stuk mist het veld %r" % veld)
    uit = {v: str(s.get(v, "") or "") for v in _VELDEN}
    per_document = per_document if per_document is not None else {}
    doc = (uit["bron"], uit["pad"])
    try:
        uit["volgnr"] = int(s["volgnr"]) if s.get("volgnr") is not None else per_document.get(doc, 0)
    except (TypeError, ValueError):
        uit["volgnr"] = per_document.get(doc, 0)
    per_document[doc] = per_document.get(doc, 0) + 1
    uit["bestand"] = ""
    if not uit["id"]:
        uit["id"] = _stuk_id(uit["bron"], uit["pad"], uit["sectie"], s.get("volgnr", 0))
    uit["ruw"] = _inhoud(uit)
    oud = telling.hergebruik_extra.get(uit["id"])
    if oud is not None and oud[0] == uit["ruw"]:
        telling.hergebruikt += 1
        return dict(oud[1], ruw=uit["ruw"])
    uit["tekst"] = telling.maskeer(uit["tekst"], config)
    if not uit["titel"]:
        uit["titel"] = os.path.splitext(os.path.basename(uit["pad"]))[0]
    if not uit["verwijzingen"]:
        uit["verwijzingen"] = _verwijzingen(uit["tekst"], config.verwijzingspatronen)
    return uit


def _ontdubbel(stukken):
    """Dezelfde tekst gaat er één keer in; een dubbele ID (twee bronnen die toevallig
    hetzelfde stuk-ID afleiden, of een aanroeper die ze zelf toekent) krijgt een afgeleide
    ID in plaats van de bouw te laten stranden."""
    gezien = set()
    ids = set()
    uit = []
    for s in stukken:
        h = hashlib.sha1(re.sub(r"\s+", " ", s["tekst"].strip().lower()).encode("utf-8")).hexdigest()
        if h in gezien:
            continue
        gezien.add(h)
        if s["id"] in ids:
            s = dict(s, id=_stuk_id(s["id"], s["bron"], s["pad"], len(ids)))
        ids.add(s["id"])
        uit.append(s)
    return uit


# ---------------------------------------------------------------- embedder

MODELBESTANDEN_EXTRA = ("onnx/tokenizer.json", "onnx/tokenizer_config.json",
                        "onnx/special_tokens_map.json", "onnx/config.json")


def embed_tekst(stuk):
    """Wat het model ziet: titel en kopjespad vóór de tekst. Een sectie 'Herstel' zonder
    de titel 'Back-upprocedure' erbij weet niet waar hij over gaat."""
    kop = stuk["titel"]
    if stuk.get("sectie") and stuk["sectie"] != stuk["titel"]:
        kop += " › " + stuk["sectie"]
    return "%s\n%s" % (kop, stuk["tekst"])


class Embedder(object):
    """Lokaal embeddingmodel. Laadt tokenizer en ONNX-sessie lui en houdt ze daarna warm
    in het proces. Downloadt het model alleen als er geen lokale kopie is.

    Elk object met `passages(teksten) -> matrix` en `query(tekst) -> vector` (genormaliseerd)
    kan deze klasse vervangen, bijvoorbeeld in tests."""

    def __init__(self, config, max_tokens=384):
        self.config = config
        self.max_tokens = max_tokens
        self._tok = None
        self._sess = None
        self._input_names = None

    def modelmap(self, downloaden=True):
        c = self.config
        if c.model_pad:
            if not os.path.exists(os.path.join(c.model_pad, c.model_bestand)):
                raise FileNotFoundError("model_pad %s bevat geen %s"
                                        % (c.model_pad, c.model_bestand))
            return c.model_pad
        snap_root = os.path.join(c.model_cache, "models--" + c.model_repo.replace("/", "--"),
                                 "snapshots")
        if not os.path.isdir(snap_root) or not os.listdir(snap_root):
            if not downloaden:
                return None
            self.download()
        return os.path.join(snap_root, sorted(os.listdir(snap_root))[-1])

    def download(self):
        from huggingface_hub import hf_hub_download
        c = self.config
        for bestand in (c.model_bestand,) + MODELBESTANDEN_EXTRA:
            hf_hub_download(repo_id=c.model_repo, filename=bestand, cache_dir=c.model_cache)

    def _laad(self):
        if self._sess is not None:
            return
        from tokenizers import Tokenizer
        import onnxruntime as ort

        snap = self.modelmap()
        self._tok = Tokenizer.from_file(os.path.join(snap, "onnx", "tokenizer.json"))
        self._tok.enable_padding(pad_id=1, pad_token="<pad>")
        self._tok.enable_truncation(max_length=self.max_tokens)
        opties = ort.SessionOptions()
        # Expliciet aantal threads: zonder probeert ONNX Runtime threads aan kernen te
        # pinnen, wat in containers zonder cpu-affinity een foutmelding per sessie geeft.
        opties.intra_op_num_threads = self.config.threads or os.cpu_count() or 1
        self._sess = ort.InferenceSession(os.path.join(snap, self.config.model_bestand),
                                          opties, providers=["CPUExecutionProvider"])
        self._input_names = [i.name for i in self._sess.get_inputs()]

    def _embed(self, teksten):
        self._laad()
        encs = self._tok.encode_batch(teksten)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        inputs = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = np.zeros_like(ids)
        out = self._sess.run(None, inputs)[0]
        m = mask[..., None].astype(np.float32)
        pooled = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        return pooled / np.linalg.norm(pooled, axis=1, keepdims=True)

    def passages(self, teksten):
        return self._embed(["passage: " + t[:1500] for t in teksten])

    def query(self, tekst):
        return self._embed(["query: " + tekst])[0]


# ---------------------------------------------------------------- bouwen

def _cache_sleutel(config, tekst):
    return hashlib.sha1((config.model_repo + "|" + tekst).encode("utf-8")).hexdigest()


class _Slot(object):
    """Bestandslock rond een bouw. Twee bouwen tegelijk (een cron en een handmatige)
    zouden elkaars tmp-bestand wissen; de tweede krijgt nu BouwBezig."""

    def __init__(self, db_pad):
        self.pad = db_pad + ".lock"
        self.fd = None

    def __enter__(self):
        try:
            if time.time() - os.path.getmtime(self.pad) > LOCK_VEROUDERD_S:
                os.remove(self.pad)
        except OSError:
            pass
        try:
            self.fd = os.open(self.pad, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BouwBezig("er loopt al een bouw voor %s (lock: %s)"
                            % (os.path.basename(self.pad)[:-5], self.pad))
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.remove(self.pad)
        except OSError:
            pass


def _inhoud(s):
    """Vingerafdruk van alles wat er van een stuk wordt opgeslagen; `ververs` vergelijkt hierop."""
    delen = [s.get(v, "") for v in _VELDEN] + [s.get("volgnr", 0), s.get("bestand", "")]
    return hashlib.sha1(json.dumps(delen, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _maak_tabellen(cur):
    cur.execute("""CREATE TABLE stuk(
        id TEXT PRIMARY KEY, bron TEXT, pad TEXT, titel TEXT, sectie TEXT,
        datum TEXT, status TEXT, tekst TEXT, verwijzingen TEXT,
        volgnr INTEGER, bestand TEXT, inhoud TEXT, ruw TEXT)""")
    # Voor het aanvullen: de andere stukken van hetzelfde document, in documentvolgorde.
    cur.execute("CREATE INDEX stuk_document ON stuk(bron, pad, volgnr)")
    # De vijfde kolom bevat bij stemmer = "nl" de Snowball-stammen van titel, sectie en
    # tekst; anders is hij leeg. Zo is de tabelstructuur onafhankelijk van de instelling.
    # De rowid van een FTS-rij is die van zijn stuk, zodat `ververs` gericht kan wissen.
    cur.execute("""CREATE VIRTUAL TABLE stuk_fts USING fts5(
        id UNINDEXED, titel, sectie, tekst, stammen,
        tokenize="unicode61 remove_diacritics 2", prefix='3 5')""")
    cur.execute("CREATE TABLE vector(id TEXT PRIMARY KEY, vec BLOB)")
    cur.execute("CREATE TABLE embed_cache(sleutel TEXT PRIMARY KEY, vec BLOB)")
    cur.execute("CREATE TABLE meta(sleutel TEXT PRIMARY KEY, waarde TEXT)")
    # Per markdown-bestand wat het opleverde, zodat `ververs` een ongewijzigd bestand niet
    # opnieuw leest. `ids` is leeg (NULL) als het bestand niet zonder meer te hergebruiken is.
    cur.execute("""CREATE TABLE bronbestand(bron TEXT, pad TEXT, mtime INTEGER,
        grootte INTEGER, ids TEXT, PRIMARY KEY (bron, pad))""")


def _schrijf_stuk(cur, s, config):
    cur.execute("INSERT INTO stuk VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], s["bron"], s["pad"], s["titel"], s["sectie"], s["datum"],
                 s["status"], s["tekst"], s["verwijzingen"], s.get("volgnr", 0),
                 s.get("bestand", ""), _inhoud(s), s.get("ruw", "")))
    stam = (stammen("%s %s %s" % (s["titel"], s["sectie"], s["tekst"]))
            if config.stemmer == "nl" else "")
    cur.execute("INSERT INTO stuk_fts(rowid, id, titel, sectie, tekst, stammen) "
                "VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, s["id"], s["titel"], s["sectie"], s["tekst"], stam))


def _embed(stukken, sleutels, cache, config, embedder, verbose):
    """Vul `cache` aan met vectoren voor de stukken waarvan de sleutel er nog niet in zit."""
    nieuw = [s for s in stukken if sleutels[s["id"]] not in cache]
    if verbose:
        print("nieuw te embedden: %d (cache treft %d)" % (len(nieuw), len(stukken) - len(nieuw)))
    if not nieuw:
        return
    embedder = embedder or Embedder(config)
    # Op lengte sorteren: een batch wordt opgevuld tot zijn langste tekst, dus korte
    # stukken naast één lange kosten evenveel als allemaal lange.
    nieuw.sort(key=lambda s: len(s["tekst"]))
    klaar = 0
    for i in range(0, len(nieuw), 16):
        batch = nieuw[i:i + 16]
        for s, v in zip(batch, embedder.passages([embed_tekst(b) for b in batch])):
            cache[sleutels[s["id"]]] = np.asarray(v, dtype=np.float32).tobytes()
        klaar += len(batch)
        if verbose and len(nieuw) >= 500 and klaar % 496 == 0:
            print("  embedden: %d/%d" % (klaar, len(nieuw)), flush=True)


def _bronbestanden(telling, stukken):
    """Rijen voor de tabel bronbestand. Een bestand is alleen te hergebruiken als al zijn
    stukken ongewijzigd in de index staan: ontdubbelen kan er een hebben weggelaten of een
    andere ID gegeven, en dan zou hergebruik iets anders opleveren dan opnieuw lezen."""
    herkomst = {s["id"]: (s["bron"], s.get("bestand", "")) for s in stukken}
    for (bron, pad), (mtime, grootte, ids) in sorted(telling.bestanden.items()):
        zuiver = all(herkomst.get(i) == (bron, pad) for i in ids)
        yield (bron, pad, mtime, grootte, json.dumps(ids) if zuiver else None)


def _schrijf_meta(cur, config, n):
    from . import versie
    for k, v in (("model", config.model_repo), ("dim", str(config.dim)),
                 ("gebouwd", time.strftime("%Y-%m-%d %H:%M")), ("versie", versie()),
                 ("schema", SCHEMA), ("stemmer", config.stemmer),
                 ("config", config.vingerafdruk()), ("stukken", str(n))):
        cur.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))


def _meld_verzameld(telling, stukken, verbose):
    if not stukken:
        raise ValueError("niets om te indexeren: geen [[bron]] in de config en geen extra_stukken")
    if verbose:
        print("verzameld: %d stukken" % len(stukken))
        print("beveiliging: %d bestand(en) uitgesloten, %d opt-out, %d geheim(en) gemaskeerd"
              % (telling.uitgesloten, telling.optout, telling.gemaskeerd))


def bouw(config, verbose=True, embedder=None, extra_stukken=None):
    """(Her)bouw de index. `embedder` is optioneel (standaard het e5-model uit de config);
    `extra_stukken` zijn stukken van het aanroepende programma, zie _normaliseer_stuk."""
    os.makedirs(os.path.dirname(config.db_pad) or ".", exist_ok=True)
    with _Slot(config.db_pad):
        return _bouw(config, verbose, embedder, extra_stukken)


def _bouw(config, verbose, embedder, extra_stukken):
    telling = Telling()
    stukken = verzamel_stukken(config, telling, extra_stukken)
    _meld_verzameld(telling, stukken, verbose)

    db_tmp = config.db_pad + ".tmp"
    if os.path.exists(db_tmp):
        os.remove(db_tmp)
    con = sqlite3.connect(db_tmp)
    cur = con.cursor()
    _maak_tabellen(cur)

    cache = {}
    if os.path.exists(config.db_pad):
        try:
            oud = sqlite3.connect(config.db_pad)
            cache = dict(oud.execute("SELECT sleutel, vec FROM embed_cache"))
            oud.close()
        except sqlite3.Error:
            cache = {}

    # Eerst de goedkope stap: tekst en woordindex. Een datafout (dubbele ID, verkeerd
    # type) komt zo binnen een seconde boven, niet pas na een kwartier embedden.
    for s in stukken:
        _schrijf_stuk(cur, s, config)
    con.commit()

    sleutels = {s["id"]: _cache_sleutel(config, embed_tekst(s)) for s in stukken}
    _embed(stukken, sleutels, cache, config, embedder, verbose)

    for s in stukken:
        sleutel = sleutels[s["id"]]
        cur.execute("INSERT INTO vector VALUES (?,?)", (s["id"], cache[sleutel]))
        cur.execute("INSERT OR IGNORE INTO embed_cache VALUES (?,?)", (sleutel, cache[sleutel]))
    cur.executemany("INSERT INTO bronbestand VALUES (?,?,?,?,?)", _bronbestanden(telling, stukken))
    _schrijf_meta(cur, config, len(stukken))

    con.commit()
    con.close()
    os.replace(db_tmp, config.db_pad)
    # Het indexbestand bevat de volledige tekst van alles wat erin zit: alleen de
    # eigenaar mag het lezen. (Op Windows regelt NTFS dit via de rechten van de map.)
    if os.name == "posix":
        os.chmod(config.db_pad, 0o600)
    if verbose:
        print("index gebouwd: %s" % config.db_pad)
    return len(stukken)


def _waarom_volledig(config):
    """Waarom `ververs` niet op de bestaande index kan voortbouwen, of "" als het wel kan."""
    if not os.path.exists(config.db_pad):
        return "er is nog geen index"
    try:
        m = meta(config.db_pad)
    except sqlite3.Error:
        return "de index is niet leesbaar"
    if m.get("schema") != SCHEMA:
        return "de index heeft een oudere indexstructuur"
    if m.get("config") != config.vingerafdruk():
        return "de config is veranderd sinds de vorige bouw"
    return ""


def ververs(config, verbose=True, embedder=None, extra_stukken=None):
    """Werk de index bij met alleen wat veranderd is: nieuwe stukken erbij, gewijzigde
    vervangen, verdwenen weg, in één transactie. Een markdown-bestand met dezelfde
    wijzigingstijd en grootte wordt niet opnieuw gelezen; een JSON-lijst wordt per item
    vergeleken, aangeleverde stukken per stuk. Het resultaat is dezelfde index als een
    volledige bouw zou geven. Kan dat niet (geen index, oudere structuur, andere config),
    dan wordt het een volledige bouw.

    Geeft een dict met `stukken`, `nieuw_of_gewijzigd`, `weg`, `gelezen`, `overgeslagen`
    en `volledig` (True als het een volledige bouw werd)."""
    os.makedirs(os.path.dirname(config.db_pad) or ".", exist_ok=True)
    with _Slot(config.db_pad):
        reden = _waarom_volledig(config)
        if reden:
            if verbose:
                print("volledige bouw: %s" % reden)
            n = _bouw(config, verbose, embedder, extra_stukken)
            return {"stukken": n, "nieuw_of_gewijzigd": n, "weg": 0, "gelezen": None,
                    "overgeslagen": 0, "volledig": True, "reden": reden}
        return _ververs(config, verbose, embedder, extra_stukken)


def _hergebruik(con):
    """Wat `ververs` niet opnieuw hoeft te maken. Eén: {(bron, pad): (mtime_ns, grootte,
    [stukken])} voor de markdown-bestanden waarvan de index precies de stukken bevat die
    het bestand de vorige keer opleverde. Twee: {stuk-ID: (ruw, stuk)} voor aangeleverde
    stukken, zodat een ongewijzigd stuk niet opnieuw gemaskeerd wordt."""
    opgeslagen, extra = {}, {}
    for r in con.execute("SELECT * FROM stuk ORDER BY bron, pad, volgnr"):
        s = dict(r)
        s.pop("inhoud", None)
        ruw = s.pop("ruw", "") or ""
        if s["bestand"]:
            opgeslagen.setdefault((s["bron"], s["bestand"]), []).append(s)
        elif ruw:
            extra[s["id"]] = (ruw, s)
    uit = {}
    for bron, pad, mtime, grootte, ids in con.execute(
            "SELECT bron, pad, mtime, grootte, ids FROM bronbestand WHERE ids IS NOT NULL"):
        stukken = opgeslagen.get((bron, pad), [])
        if sorted(s["id"] for s in stukken) == sorted(json.loads(ids)):
            uit[(bron, pad)] = (mtime, grootte, stukken)
    return uit, extra


def _ververs(config, verbose, embedder, extra_stukken):
    begin = time.time()
    con = sqlite3.connect(config.db_pad, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        bestanden, extra = _hergebruik(con)
        telling = Telling(bestanden)
        telling.hergebruik_extra = extra
        stukken = verzamel_stukken(config, telling, extra_stukken)
        _meld_verzameld(telling, stukken, verbose)

        bestaand = dict(con.execute("SELECT id, inhoud FROM stuk").fetchall())
        nieuw = {s["id"]: s for s in stukken}
        weg = [i for i, h in bestaand.items() if i not in nieuw or h != _inhoud(nieuw[i])]
        erbij = [s for s in stukken if bestaand.get(s["id"]) != _inhoud(s)]

        sleutels = {s["id"]: _cache_sleutel(config, embed_tekst(s)) for s in stukken}
        cache = {}
        for s in erbij:
            r = con.execute("SELECT vec FROM embed_cache WHERE sleutel=?",
                            (sleutels[s["id"]],)).fetchone()
            if r is not None:
                cache[sleutels[s["id"]]] = r[0]
        # Embedden vóór de transactie: dat kan even duren, en zoeken blijft intussen gewoon
        # werken op de index zoals hij was.
        _embed(erbij, sleutels, cache, config, embedder, verbose and bool(erbij))

        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for i in weg:
                cur.execute("DELETE FROM stuk_fts WHERE rowid = (SELECT rowid FROM stuk WHERE id=?)",
                            (i,))
                cur.execute("DELETE FROM stuk WHERE id=?", (i,))
                cur.execute("DELETE FROM vector WHERE id=?", (i,))
            for s in erbij:
                _schrijf_stuk(cur, s, config)
                sleutel = sleutels[s["id"]]
                cur.execute("INSERT INTO vector VALUES (?,?)", (s["id"], cache[sleutel]))
                cur.execute("INSERT OR IGNORE INTO embed_cache VALUES (?,?)",
                            (sleutel, cache[sleutel]))
            # Net als bij een bouw: cache-regels die geen enkel stuk meer gebruikt, gaan weg.
            gebruikt = set(sleutels.values())
            ongebruikt = [(k,) for (k,) in cur.execute("SELECT sleutel FROM embed_cache").fetchall()
                          if k not in gebruikt]
            cur.executemany("DELETE FROM embed_cache WHERE sleutel=?", ongebruikt)
            cur.execute("DELETE FROM bronbestand")
            cur.executemany("INSERT INTO bronbestand VALUES (?,?,?,?,?)",
                            _bronbestanden(telling, stukken))
            _schrijf_meta(cur, config, len(stukken))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
    finally:
        con.close()
    uit = {"stukken": len(stukken), "nieuw_of_gewijzigd": len(erbij),
           "weg": len(set(weg) - set(nieuw)), "gelezen": telling.gelezen,
           "overgeslagen": telling.hergebruikt, "volledig": False}
    if verbose:
        print("ververst in %.1f s: %d nieuw of gewijzigd, %d weg, %d ongewijzigd "
              "(%d bestand(en) gelezen, %d overgeslagen)"
              % (time.time() - begin, uit["nieuw_of_gewijzigd"], uit["weg"],
                 len(stukken) - len(erbij), telling.gelezen, telling.hergebruikt))
    return uit


# ---------------------------------------------------------------- zoeken

def meta(db_pad):
    con = sqlite3.connect(db_pad)
    try:
        return dict(con.execute("SELECT sleutel, waarde FROM meta"))
    finally:
        con.close()


def per_bron(db_pad):
    con = sqlite3.connect(db_pad)
    try:
        return dict(con.execute("SELECT bron, COUNT(*) FROM stuk GROUP BY bron ORDER BY bron"))
    finally:
        con.close()


def _fts_pogingen(vraag, stemmer="grof"):
    """Eerst exact, dan AND over woordstammen, dan OR - aanvullend, met aflopend gewicht.

    "grof": woorden van 7+ tekens verliezen 3 letters en krijgen een wildcard, op alle
    kolommen. "nl": Snowball-stammen, alleen op de stam-kolom (stam tegen stam). "uit":
    alleen de exacte poging.
    """
    inhoud = [w for w in woorden(vraag) if w not in STOPWOORDEN and len(w) > 2]
    if not inhoud:
        return []
    pogingen = [(" ".join('"%s"' % w for w in inhoud), 1.0)]
    if stemmer == "uit":
        return pogingen
    if stemmer == "nl":
        termen = ['stammen: "%s"' % t for t in _snowball().stemWords(inhoud)]
    else:
        termen = [w[:-3] + "*" if len(w) >= 7 else w for w in inhoud]
    pogingen.append((" AND ".join(termen), 0.7))
    if len(termen) > 1:
        pogingen.append((" OR ".join(termen), 0.5))
    return pogingen


def is_fts_syntax(vraag):
    """Gebruikt de vraagsteller zelf FTS5-syntax ("...", *, AND/OR/NOT/NEAR)? Dan weet
    hij precies wat hij zoekt: de vraag gaat ongewijzigd naar de woordindex en de
    betekenislijst blijft achterwege."""
    return bool(_FTS_SYNTAX.search(vraag))


def _actualiteit(datum, dagen_horizon):
    """1,0 voor vandaag, lineair naar 0 op de horizon; 0 zonder bruikbare datum."""
    try:
        dagen = (datetime.now() - datetime.fromisoformat(str(datum)[:10])).days
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, 1.0 - dagen / float(dagen_horizon))


def rrf(lijsten, k=RRF_K):
    """Reciprocal rank fusion over ranglijsten, voor wie zelf lijsten heeft (een eigen
    database, een versleuteld archief) en ze op dezelfde manier wil samenvoegen als de
    `Zoeker`. `lijsten` is een iterable van `(naam, ids, gewicht)`: `ids` in rangorde (plek 1
    eerst), dubbele ids tellen één keer. Geeft `{id: (score, [namen van de lijsten die het
    vonden])}`. Plek 1 in één lijst met gewicht 1 geeft 1/(k+1), precies als de `Zoeker`,
    zodat scores uit verschillende indexen naast elkaar te leggen zijn (`samenvoegen`)."""
    uit = {}
    for naam, ids, gewicht in lijsten:
        gezien = set()
        for id_ in ids:
            if id_ in gezien:
                continue
            gezien.add(id_)
            score, signalen = uit.get(id_, (0.0, []))
            uit[id_] = (score + gewicht / (k + len(gezien)), signalen + [naam])
    return uit


def samenvoegen(hoofd, extra, max_per_bron=2, min_signalen=2):
    """Treffers uit meerdere indexen tot één lijst, zonder dat een extra index de eigen
    treffers wegdrukt.

    `hoofd` is de lijst van je eigen index (treffers met `score`), `extra` een dict
    `{naam: treffers}` van andere indexen (uit een `Zoeker` of via `rrf`). Per extra index
    dringen hoogstens `max_per_bron` treffers op score tussen de hoofdlijst, en alleen als
    minstens `min_signalen` lijsten ze vonden (veld `signalen`): één signaal is in een grote,
    rommelige verzameling vaak toeval. De rest komt daarachter, per index op score.

    Knip je het resultaat af, reken dan `max_per_bron` plekken per extra index bij je gewone
    aantal: zo valt er door het voordringen geen eigen treffer weg. Zonder die ruimte en
    zonder drempel duwden een paar facturen die toevallig op één woord raakten de juiste
    stukken uit de top.
    """
    def score(t):
        return float(t.get("score") or 0)

    voor, achter = [], []
    for naam in extra:
        lijst = sorted(extra[naam], key=score, reverse=True)
        sterk = [t for t in lijst if len(t.get("signalen") or ()) >= min_signalen][:max_per_bron]
        gekozen = {id(t) for t in sterk}
        voor += sterk
        achter += [t for t in lijst if id(t) not in gekozen]
    return sorted(list(hoofd) + voor, key=score, reverse=True) + achter


class Zoeker(object):
    """Houdt model en vectormatrix warm. Maak er één per config in een langlopend
    proces; de matrix wordt opnieuw ingelezen zodra het indexbestand verandert."""

    def __init__(self, config, embedder=None):
        self.config = config
        self.embedder = embedder or Embedder(config)
        self._matrix = None
        self._ids = None
        self._bronnen = None
        self._stand = None

    def _controleer(self):
        m = meta(self.config.db_pad)
        if m.get("schema") != SCHEMA:
            raise ValueError("de index is gebouwd met een oudere indexstructuur - draai "
                             "'polaris bouw' opnieuw")
        if m.get("stemmer", "grof") != self.config.stemmer:
            raise ValueError("de index is gebouwd met stemmer %r, de config zegt %r - draai "
                             "'polaris bouw' opnieuw" % (m.get("stemmer", "grof"),
                                                        self.config.stemmer))
        if m.get("model") != self.config.model_repo or m.get("dim") != str(self.config.dim):
            raise ValueError(
                "de index is gebouwd met model %s (dim %s), de config zegt %s (dim %s) - "
                "draai 'polaris bouw' opnieuw" % (m.get("model"), m.get("dim"),
                                                self.config.model_repo, self.config.dim))

    def _laad_matrix(self, con):
        st = os.stat(self.config.db_pad)
        stand = (st.st_mtime_ns, st.st_size)
        if self._stand == stand:
            return
        rijen = con.execute("SELECT v.id, v.vec, s.bron FROM vector v JOIN stuk s ON s.id = v.id"
                            ).fetchall()
        self._ids = [r["id"] for r in rijen]
        self._bronnen = np.array([r["bron"] for r in rijen])
        self._matrix = (np.frombuffer(b"".join(r["vec"] for r in rijen), dtype=np.float32)
                        .reshape(len(rijen), self.config.dim) if rijen else None)
        self._stand = stand

    def _fts(self, con, query, bron=None, limiet=KANDIDATEN, extra_sql="", extra_params=(),
             streng=False):
        """Eén FTS5-zoekopdracht, gerangschikt op BM25. Geeft rijen met `id` en, als de
        config erom vraagt, `fragment`. Ongeldige syntax geeft een lege lijst, of met
        `streng` de OperationalError."""
        rangorde = "bm25(stuk_fts, %s)" % ", ".join(str(g) for g in BM25_GEWICHTEN)
        kolommen, params_voor = "id", ()
        if self.config.snippets:
            # Kolom 3 = tekst; de stam-kolom is voor mensen onleesbaar.
            kolommen = "id, snippet(stuk_fts, 3, ?, ?, '…', 24) AS fragment"
            params_voor = self.config.snippet_markering
        filter_sql, params_na = "", ()
        if bron:
            filter_sql = " AND id IN (SELECT id FROM stuk WHERE bron = ?)"
            params_na = (bron,)
        try:
            return con.execute(
                "SELECT %s FROM stuk_fts WHERE stuk_fts MATCH ?%s%s ORDER BY %s LIMIT %d"
                % (kolommen, filter_sql, extra_sql, rangorde, limiet),
                params_voor + (query,) + params_na + tuple(extra_params)).fetchall()
        except sqlite3.OperationalError:
            if streng:
                raise
            return []

    def _woordlijst(self, con, vraag, bron, snippets, streng=False):
        """{id: (gewicht, rang)}: exact eerst, dan aangevuld met stammen."""
        uit = {}
        pogingen = ([(vraag, 1.0)] if is_fts_syntax(vraag)
                    else _fts_pogingen(vraag, self.config.stemmer))
        for query, gewicht in pogingen:
            if len(uit) >= KANDIDATEN:
                break
            for r in self._fts(con, query, bron, streng=streng):
                if r["id"] not in uit:
                    uit[r["id"]] = (gewicht, len(uit) + 1)
                    if self.config.snippets:
                        snippets.setdefault(r["id"], r["fragment"])
        return uit

    def _namenlijsten(self, con, namenlijst, bron, snippets):
        """Per naam uit de vraag een eigen woordlijst, en bij twee of meer namen nog één voor
        de stukken die ze allemaal noemen. Zo zakt een stuk dat de klant uit de vraag noemt
        niet weg achter stukken die alleen over hetzelfde onderwerp gaan."""
        vragen = [_frase(n) for n in namenlijst]
        if len(vragen) > 1:
            vragen.append(" AND ".join(vragen))
        lijsten = []
        for query in vragen:
            lijst = {}
            for r in self._fts(con, query, bron):
                lijst[r["id"]] = len(lijst) + 1
                if self.config.snippets:
                    snippets.setdefault(r["id"], r["fragment"])
            lijsten.append(lijst)
        return lijsten

    def _betekenislijst(self, con, vraag, bron):
        self._laad_matrix(con)
        if self._matrix is None:
            return {}
        sims = self._matrix @ np.asarray(self.embedder.query(vraag), dtype=np.float32)
        if bron:
            sims = np.where(self._bronnen == bron, sims, -2.0)
        uit = {}
        for i in np.argsort(-sims)[:KANDIDATEN]:
            if sims[i] <= -2.0:
                break
            uit[self._ids[i]] = len(uit) + 1
        return uit

    def _vul_aan(self, con, treffers, vraag, namenlijst):
        """Is een document raak, dan komen de andere stukken ervan mee die de namen (of,
        zonder namen, de woorden) uit de vraag bevatten, ook als ze zelf buiten de top-k
        vielen: hoogstens `aanvullen` per document, in documentvolgorde, bij de hoogste
        treffer van dat document. Een samenvatting en een tabel verderop die elkaar
        tegenspreken, komen zo allebei boven."""
        # Alleen sectie en tekst: de titel delen alle stukken van een document, dus een naam
        # in de titel zou elk stuk raak maken.
        if namenlijst:
            termen = ['"%s"' % " ".join(n) for n in namenlijst]
        else:
            termen = [w[:-3] + "*" if len(w) >= 7 else '"%s"' % w
                      for w in woorden(vraag) if w not in STOPWOORDEN and len(w) > 2]
        if not termen:
            return
        query = "{sectie tekst} : (%s)" % " OR ".join(termen)
        al_getoond = {t["id"] for t in treffers}
        gedaan = set()
        for t in treffers:
            doc = (t["bron"], t["pad"])
            if doc in gedaan:
                continue
            gedaan.add(doc)
            rijen = self._fts(con, query, limiet=self.config.aanvullen + len(al_getoond),
                              extra_sql=" AND id IN (SELECT id FROM stuk WHERE bron = ? AND pad = ?)",
                              extra_params=doc)
            ids = [r["id"] for r in rijen if r["id"] not in al_getoond][:self.config.aanvullen]
            if not ids:
                continue
            fragmenten = {r["id"]: r["fragment"] for r in rijen} if self.config.snippets else {}
            extra = []
            for id_ in ids:
                r = con.execute("SELECT id, sectie, volgnr, tekst FROM stuk WHERE id=?",
                                (id_,)).fetchone()
                stuk = dict(r)
                if self.config.snippets:
                    stuk["fragment"] = " ".join((fragmenten.get(id_) or r["tekst"][:200]).split())
                extra.append(stuk)
            t["ook_in_dit_document"] = sorted(extra, key=lambda x: x["volgnr"])

    def zoek(self, vraag, k=8, bron=None):
        self._controleer()
        con = sqlite3.connect(self.config.db_pad)
        con.row_factory = sqlite3.Row
        try:
            snippets = {}
            try:
                woord = self._woordlijst(con, vraag, bron, snippets, streng=is_fts_syntax(vraag))
            except sqlite3.OperationalError:
                # Ongeldige eigen syntax is bijna altijd een gewone vraag met een haakje of
                # dubbele punt erin ("hoe doe ik dit (snel)?"). Geen lege lijst, maar zoeken
                # op de woorden. Geldige syntax zonder treffer blijft leeg: dat is een antwoord.
                vraag = _ZONDER_SYNTAX.sub(" ", vraag)
                woord = self._woordlijst(con, vraag, bron, snippets)
            vrij = not is_fts_syntax(vraag)
            betekenis = self._betekenislijst(con, vraag, bron) if vrij else {}
            namenlijst = namen(vraag) if vrij else []

            scores, signalen = {}, {}
            for id_, (gewicht, rang) in woord.items():
                scores[id_] = scores.get(id_, 0.0) + gewicht / (RRF_K + rang)
                signalen.setdefault(id_, []).append("woorden")
            for id_, rang in betekenis.items():
                scores[id_] = scores.get(id_, 0.0) + 1.0 / (RRF_K + rang)
                signalen.setdefault(id_, []).append("betekenis")
            if namenlijst and self.config.namen_gewicht:
                for lijst in self._namenlijsten(con, namenlijst, bron, snippets):
                    for id_, rang in lijst.items():
                        scores[id_] = scores.get(id_, 0.0) + self.config.namen_gewicht / (RRF_K + rang)
                        if "namen" not in signalen.setdefault(id_, []):
                            signalen[id_].append("namen")

            top = 1.0 / (RRF_K + 1)
            uit = []
            for id_, score in scores.items():
                r = con.execute("SELECT * FROM stuk WHERE id=?", (id_,)).fetchone()
                if r is None:
                    continue
                if (r["status"] or "").lower() in VERVALLEN_STATUSSEN:
                    score *= 0.5
                if self.config.actualiteit_bonus:
                    score += (self.config.actualiteit_bonus * top
                              * _actualiteit(r["datum"], self.config.actualiteit_dagen))
                rij = dict(r)
                for intern in ("inhoud", "bestand", "ruw"):
                    rij.pop(intern, None)
                rij["score"] = round(score, 5)
                rij["signalen"] = signalen.get(id_, [])
                if self.config.snippets:
                    # Een treffer die alleen op betekenis is gevonden heeft geen gemarkeerde
                    # woorden; dan het begin van de tekst, zodat het veld er altijd is.
                    rij["fragment"] = " ".join((snippets.get(id_) or r["tekst"][:200]).split())
                uit.append(rij)
            uit.sort(key=lambda x: -x["score"])
            uit = uit[:k]
            if vrij and self.config.aanvullen:
                self._vul_aan(con, uit, vraag, namenlijst)
            return uit
        finally:
            con.close()


_ZOEKERS = {}


def zoek(config, vraag, k=8, bron=None, embedder=None):
    """Gemak-functie: één Zoeker per indexbestand, warm gehouden binnen dit proces."""
    z = _ZOEKERS.get(config.db_pad)
    if z is None or z.config is not config:
        z = _ZOEKERS[config.db_pad] = Zoeker(config, embedder)
    return z.zoek(vraag, k=k, bron=bron)
