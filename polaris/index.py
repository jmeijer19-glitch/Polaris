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
3. Embeddings: een e5-model, int8-ONNX, lokaal op de CPU. Titel en kopjespad gaan mee
   in de invoer, anders weet een losse sectie niet waar hij over gaat.
4. Zoeken: woordlijst (BM25) en betekenislijst (cosinus) apart, samengevoegd met
   Reciprocal Rank Fusion, daarna herrangschikt op status en actualiteit. De `Zoeker`
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
SCHEMA = "3"

# Een bouw-lock ouder dan dit is van een afgebroken proces en mag weg.
LOCK_VEROUDERD_S = 3600

_KOPREGEX = re.compile(r"^(#{2,4})\s+(.*)$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)
_FTS_SYNTAX = re.compile(r'[*"^:()]|\b(AND|OR|NOT|NEAR)\b')


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

    def __init__(self):
        self.uitgesloten = 0
        self.optout = 0
        self.gemaskeerd = 0

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
                with io.open(vol, encoding="utf-8") as fh:
                    tekst = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if not tekst.strip():
                continue
            if beveiliging.heeft_optout(tekst):
                telling.optout += 1
                continue
            tekst = telling.maskeer(tekst, config)
            uit.extend(stukken_uit_markdown(bron.naam, rel, tekst, config,
                                            datum=_datum(vol, tekst)))
    return uit


def stukken_uit_markdown(bron_naam, rel, tekst, config, datum=""):
    """Eén markdown-document naar stukken. Ook bruikbaar voor tekst die niet uit een
    bestand komt (een aanroepend programma dat zelf bronnen aanlevert).

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
        })
    if not uit and omschrijving:
        uit.append({
            "id": _stuk_id(bron_naam, rel, "", 0), "bron": bron_naam, "pad": rel,
            "titel": titel, "sectie": "", "datum": datum, "status": status,
            "tekst": omschrijving,
            "verwijzingen": _verwijzingen(omschrijving, config.verwijzingspatronen),
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
    for s in extra_stukken or []:
        stukken.append(_normaliseer_stuk(s, config, telling))
    return _ontdubbel(stukken)


_VELDEN = ("id", "bron", "pad", "titel", "sectie", "datum", "status", "tekst", "verwijzingen")


def _normaliseer_stuk(s, config, telling):
    """Een stuk van een aanroepend programma: `bron`, `pad` en `tekst` zijn verplicht,
    de rest is optioneel. Krijgt dezelfde maskering als eigen bronnen."""
    for veld in ("bron", "pad", "tekst"):
        if not s.get(veld):
            raise ValueError("extra stuk mist het veld %r" % veld)
    uit = {v: str(s.get(v, "") or "") for v in _VELDEN}
    uit["tekst"] = telling.maskeer(uit["tekst"], config)
    if not uit["id"]:
        uit["id"] = _stuk_id(uit["bron"], uit["pad"], uit["sectie"], s.get("volgnr", 0))
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


def bouw(config, verbose=True, embedder=None, extra_stukken=None):
    """(Her)bouw de index. `embedder` is optioneel (standaard het e5-model uit de config);
    `extra_stukken` zijn stukken van het aanroepende programma, zie _normaliseer_stuk."""
    os.makedirs(os.path.dirname(config.db_pad) or ".", exist_ok=True)
    with _Slot(config.db_pad):
        return _bouw(config, verbose, embedder, extra_stukken)


def _bouw(config, verbose, embedder, extra_stukken):
    telling = Telling()
    stukken = verzamel_stukken(config, telling, extra_stukken)
    if verbose:
        print("verzameld: %d stukken" % len(stukken))
        print("beveiliging: %d bestand(en) uitgesloten, %d opt-out, %d geheim(en) gemaskeerd"
              % (telling.uitgesloten, telling.optout, telling.gemaskeerd))

    db_tmp = config.db_pad + ".tmp"
    if os.path.exists(db_tmp):
        os.remove(db_tmp)
    con = sqlite3.connect(db_tmp)
    cur = con.cursor()
    cur.execute("""CREATE TABLE stuk(
        id TEXT PRIMARY KEY, bron TEXT, pad TEXT, titel TEXT, sectie TEXT,
        datum TEXT, status TEXT, tekst TEXT, verwijzingen TEXT)""")
    # De vijfde kolom bevat bij stemmer = "nl" de Snowball-stammen van titel, sectie en
    # tekst; anders is hij leeg. Zo is de tabelstructuur onafhankelijk van de instelling.
    cur.execute("""CREATE VIRTUAL TABLE stuk_fts USING fts5(
        id UNINDEXED, titel, sectie, tekst, stammen,
        tokenize="unicode61 remove_diacritics 2", prefix='3 5')""")
    cur.execute("CREATE TABLE vector(id TEXT PRIMARY KEY, vec BLOB)")
    cur.execute("CREATE TABLE embed_cache(sleutel TEXT PRIMARY KEY, vec BLOB)")
    cur.execute("CREATE TABLE meta(sleutel TEXT PRIMARY KEY, waarde TEXT)")

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
        cur.execute("INSERT INTO stuk VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["bron"], s["pad"], s["titel"], s["sectie"],
                     s["datum"], s["status"], s["tekst"], s["verwijzingen"]))
        stam = (stammen("%s %s %s" % (s["titel"], s["sectie"], s["tekst"]))
                if config.stemmer == "nl" else "")
        cur.execute("INSERT INTO stuk_fts VALUES (?,?,?,?,?)",
                    (s["id"], s["titel"], s["sectie"], s["tekst"], stam))
    con.commit()

    sleutels = {s["id"]: _cache_sleutel(config, embed_tekst(s)) for s in stukken}
    nieuw = [s for s in stukken if sleutels[s["id"]] not in cache]
    if verbose:
        print("nieuw te embedden: %d (cache treft %d)" % (len(nieuw), len(stukken) - len(nieuw)))

    if nieuw:
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

    for s in stukken:
        sleutel = sleutels[s["id"]]
        cur.execute("INSERT INTO vector VALUES (?,?)", (s["id"], cache[sleutel]))
        cur.execute("INSERT OR IGNORE INTO embed_cache VALUES (?,?)", (sleutel, cache[sleutel]))

    from . import versie
    for k, v in (("model", config.model_repo), ("dim", str(config.dim)),
                 ("gebouwd", time.strftime("%Y-%m-%d %H:%M")), ("versie", versie()),
                 ("schema", SCHEMA), ("stemmer", config.stemmer),
                 ("stukken", str(len(stukken)))):
        cur.execute("INSERT INTO meta VALUES (?,?)", (k, v))

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
        stand = (st.st_mtime, st.st_size)
        if self._stand == stand:
            return
        rijen = con.execute("SELECT v.id, v.vec, s.bron FROM vector v JOIN stuk s ON s.id = v.id"
                            ).fetchall()
        self._ids = [r["id"] for r in rijen]
        self._bronnen = np.array([r["bron"] for r in rijen])
        self._matrix = (np.frombuffer(b"".join(r["vec"] for r in rijen), dtype=np.float32)
                        .reshape(len(rijen), self.config.dim) if rijen else None)
        self._stand = stand

    def _woordlijst(self, con, vraag, bron):
        """Geeft ({id: (gewicht, rang)}, {id: snippet}): exact eerst, dan aangevuld met
        stammen. Snippets alleen als de config erom vraagt."""
        uit, snippets = {}, {}
        rangorde = "bm25(stuk_fts, %s)" % ", ".join(str(g) for g in BM25_GEWICHTEN)
        filter_sql, params_extra = "", ()
        if bron:
            filter_sql = " AND id IN (SELECT id FROM stuk WHERE bron = ?)"
            params_extra = (bron,)
        kolommen, params_voor = "id", ()
        if self.config.snippets:
            # Kolom 3 = tekst; de stam-kolom is voor mensen onleesbaar.
            kolommen = "id, snippet(stuk_fts, 3, ?, ?, '…', 24) AS fragment"
            params_voor = self.config.snippet_markering
        pogingen = ([(vraag, 1.0)] if is_fts_syntax(vraag)
                    else _fts_pogingen(vraag, self.config.stemmer))
        for query, gewicht in pogingen:
            if len(uit) >= KANDIDATEN:
                break
            try:
                rijen = con.execute(
                    "SELECT %s FROM stuk_fts WHERE stuk_fts MATCH ?%s ORDER BY %s LIMIT %d"
                    % (kolommen, filter_sql, rangorde, KANDIDATEN),
                    params_voor + (query,) + params_extra).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rijen:
                if r["id"] not in uit:
                    uit[r["id"]] = (gewicht, len(uit) + 1)
                    if self.config.snippets:
                        snippets[r["id"]] = r["fragment"]
        return uit, snippets

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

    def zoek(self, vraag, k=8, bron=None):
        self._controleer()
        con = sqlite3.connect(self.config.db_pad)
        con.row_factory = sqlite3.Row
        try:
            woord, snippets = self._woordlijst(con, vraag, bron)
            betekenis = {} if is_fts_syntax(vraag) else self._betekenislijst(con, vraag, bron)

            scores = {}
            for id_, (gewicht, rang) in woord.items():
                scores[id_] = scores.get(id_, 0.0) + gewicht / (RRF_K + rang)
            for id_, rang in betekenis.items():
                scores[id_] = scores.get(id_, 0.0) + 1.0 / (RRF_K + rang)

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
                rij["score"] = round(score, 5)
                if self.config.snippets:
                    # Een treffer die alleen op betekenis is gevonden heeft geen gemarkeerde
                    # woorden; dan het begin van de tekst, zodat het veld er altijd is.
                    rij["fragment"] = " ".join((snippets.get(id_) or r["tekst"][:200]).split())
                uit.append(rij)
            uit.sort(key=lambda x: -x["score"])
            return uit[:k]
        finally:
            con.close()


_ZOEKERS = {}


def zoek(config, vraag, k=8, bron=None, embedder=None):
    """Gemak-functie: één Zoeker per indexbestand, warm gehouden binnen dit proces."""
    z = _ZOEKERS.get(config.db_pad)
    if z is None or z.config is not config:
        z = _ZOEKERS[config.db_pad] = Zoeker(config, embedder)
    return z.zoek(vraag, k=k, bron=bron)
