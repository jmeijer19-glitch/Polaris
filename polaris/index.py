# -*- coding: utf-8 -*-
"""polaris.index - de hybride zoekindex zelf: chunking, embeddings, FTS5, RRF-fusie.

De bronnen komen uit een Config-object (polaris/config.py), niet uit hardcoded paden -
dezelfde motor, andere kennisbank per installatie. Zie docs/techniek.md voor de
onderbouwing van elke keuze hieronder.

Pijplijn, kort:
1. Verzamelen: markdown op koppen geknipt, JSON-lijsten één stuk per item. Wat niet mag
   (uitgesloten pad, opt-out, uitsluit_veld) komt er niet in; geheimen worden gemaskeerd
   (polaris/beveiliging.py).
2. Opslag: één SQLite-bestand met een FTS5-tabel (woorden) en een vectortabel
   (betekenis), bij elke bouw atomisch vervangen.
3. Embeddings: een e5-model, int8-ONNX, lokaal op de CPU. Er gaat geen tekst naar buiten.
4. Zoeken: woordlijst (BM25) en betekenislijst (cosinus) apart, samengevoegd met
   Reciprocal Rank Fusion, daarna herrangschikt op status.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import time

import numpy as np

from . import beveiliging

STOPWOORDEN = set("""
de het een en of maar want dus als dan dat die deze dit is was zijn ben bent wordt
worden er wij jij hij zij ik je u we ze op in aan van voor met bij naar om over uit
niet geen nog wel ook al maar toch al te tot per zo hoe wie wat welke welk
""".split())

VERVALLEN_STATUSSEN = {"klaar", "afgerond", "gesloten", "vervallen", "done", "closed"}

# Chunking
MIN_SECTIE = 300
MAX_STUK = 1800

# Zoeken
RRF_K = 60
KANDIDATEN = 40
# BM25-gewichten per FTS-kolom: id (niet geïndexeerd), titel, sectie, tekst.
BM25_GEWICHTEN = (0.0, 6.0, 3.0, 1.0)


def _verwijzingen(tekst, patronen, eigen_id=""):
    gevonden = set()
    for p in patronen:
        gevonden.update(p.findall(tekst))
    gevonden.discard(eigen_id)
    return ",".join(sorted(g for g in gevonden if g))


# ---------------------------------------------------------------- chunking

_KOPREGEX = re.compile(r"^(#{2,4})\s+(.*)$")


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
        if len(t) <= MAX_STUK:
            uit.append((sectie, t))
            continue
        buf = ""
        for alinea in t.split("\n\n"):
            if buf and len(buf) + len(alinea) > MAX_STUK:
                uit.append((sectie, buf.strip()))
                buf = alinea
            else:
                buf = (buf + "\n\n" + alinea) if buf else alinea
        if buf.strip():
            uit.append((sectie, buf.strip()))
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
            titel = os.path.splitext(f)[0]
            datum = _datum(vol, tekst)
            for volgnr, (sectie, chunk) in enumerate(chunk_markdown(tekst, titel)):
                uit.append({
                    "id": _stuk_id(bron.naam, rel, sectie, volgnr),
                    "bron": bron.naam,
                    "pad": rel,
                    "titel": titel,
                    "sectie": sectie,
                    "datum": datum,
                    "status": "",
                    "tekst": chunk,
                    "verwijzingen": _verwijzingen(chunk, config.verwijzingspatronen),
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
    for volgnr, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if bron.uitsluit_veld and beveiliging.is_waar(item.get(bron.uitsluit_veld)):
            telling.optout += 1
            continue
        item_id = str(item.get(bron.id_veld, "") or "")
        titel = str(item.get(bron.titel_veld, "") or "")
        lichaam = str(item.get(bron.tekst_veld, "") or "")
        tekst = telling.maskeer("%s\n\n%s" % (titel, lichaam), config)
        uit.append({
            "id": _stuk_id(bron.naam, item_id or volgnr),
            "bron": bron.naam,
            "pad": "%s#%s" % (os.path.basename(bron.pad), item_id or volgnr),
            "titel": "%s - %s" % (item_id, titel) if item_id else titel,
            "sectie": str(item.get(bron.sectie_veld, "") or "") if bron.sectie_veld else "",
            "datum": str(item.get(bron.datum_veld, "") or "") if bron.datum_veld else "",
            "status": str(item.get(bron.status_veld, "") or ""),
            "tekst": tekst,
            "verwijzingen": _verwijzingen(tekst, config.verwijzingspatronen, item_id),
        })
    return uit


def verzamel_stukken(config, telling=None):
    telling = telling or Telling()
    stukken = []
    for bron in config.bronnen:
        if bron.type == "markdown":
            stukken += _stukken_markdown(bron, config, telling)
        elif bron.type == "json_lijst":
            stukken += _stukken_json_lijst(bron, config, telling)
        else:
            raise ValueError("onbekend brontype %r bij bron %r" % (bron.type, bron.naam))
    return _ontdubbel(stukken)


def _ontdubbel(stukken):
    gezien = set()
    uit = []
    for s in stukken:
        h = hashlib.sha1(re.sub(r"\s+", " ", s["tekst"].strip().lower()).encode("utf-8")).hexdigest()
        if h not in gezien:
            gezien.add(h)
            uit.append(s)
    return uit


# ---------------------------------------------------------------- embedder

MODELBESTANDEN_EXTRA = ("onnx/tokenizer.json", "onnx/tokenizer_config.json",
                        "onnx/special_tokens_map.json", "onnx/config.json")


class Embedder(object):
    """Lokaal embeddingmodel. Laadt tokenizer en ONNX-sessie lui en houdt ze daarna warm
    in het proces. Downloadt het model alleen als er geen lokale kopie is."""

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
        self._sess = ort.InferenceSession(os.path.join(snap, self.config.model_bestand),
                                          providers=["CPUExecutionProvider"])
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


def bouw(config, verbose=True):
    telling = Telling()
    stukken = verzamel_stukken(config, telling)
    if verbose:
        print("verzameld: %d stukken" % len(stukken))
        print("beveiliging: %d bestand(en) uitgesloten, %d opt-out, %d geheim(en) gemaskeerd"
              % (telling.uitgesloten, telling.optout, telling.gemaskeerd))

    db_tmp = config.db_pad + ".tmp"
    os.makedirs(os.path.dirname(config.db_pad) or ".", exist_ok=True)
    if os.path.exists(db_tmp):
        os.remove(db_tmp)
    con = sqlite3.connect(db_tmp)
    cur = con.cursor()
    cur.execute("""CREATE TABLE stuk(
        id TEXT PRIMARY KEY, bron TEXT, pad TEXT, titel TEXT, sectie TEXT,
        datum TEXT, status TEXT, tekst TEXT, verwijzingen TEXT)""")
    cur.execute("""CREATE VIRTUAL TABLE stuk_fts USING fts5(
        id UNINDEXED, titel, sectie, tekst,
        tokenize="unicode61 remove_diacritics 2")""")
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

    nieuw = [s for s in stukken if _cache_sleutel(config, s["tekst"]) not in cache]
    if verbose:
        print("nieuw te embedden: %d (cache treft %d)" % (len(nieuw), len(stukken) - len(nieuw)))

    if nieuw:
        embedder = Embedder(config)
        nieuw.sort(key=lambda s: len(s["tekst"]))
        for i in range(0, len(nieuw), 16):
            batch = nieuw[i:i + 16]
            for s, v in zip(batch, embedder.passages([b["tekst"] for b in batch])):
                cache[_cache_sleutel(config, s["tekst"])] = v.astype(np.float32).tobytes()

    for s in stukken:
        sleutel = _cache_sleutel(config, s["tekst"])
        cur.execute("INSERT INTO stuk VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["bron"], s["pad"], s["titel"], s["sectie"],
                     s["datum"], s["status"], s["tekst"], s["verwijzingen"]))
        cur.execute("INSERT INTO stuk_fts VALUES (?,?,?,?)",
                    (s["id"], s["titel"], s["sectie"], s["tekst"]))
        cur.execute("INSERT INTO vector VALUES (?,?)", (s["id"], cache[sleutel]))
        cur.execute("INSERT OR IGNORE INTO embed_cache VALUES (?,?)", (sleutel, cache[sleutel]))

    from . import versie
    for k, v in (("model", config.model_repo), ("dim", str(config.dim)),
                 ("gebouwd", time.strftime("%Y-%m-%d %H:%M")), ("versie", versie()),
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


def _fts_pogingen(vraag):
    """Eerst exact, dan AND over woordstammen, dan OR. De eerste die iets vindt telt."""
    woorden = re.findall(r"\w+", vraag.lower())
    inhoud = [w for w in woorden if w not in STOPWOORDEN and len(w) > 2]
    if not inhoud:
        return []

    def _stam(w):
        return w[:-3] + "*" if len(w) >= 7 else w

    stammen = [_stam(w) for w in inhoud]
    return [(" ".join('"%s"' % w for w in inhoud), 1.0),
            (" AND ".join(stammen), 0.7),
            (" OR ".join(stammen), 0.5)]


def zoek(config, vraag, k=8):
    m = meta(config.db_pad)
    if m.get("model") != config.model_repo or m.get("dim") != str(config.dim):
        raise ValueError(
            "de index is gebouwd met model %s (dim %s), de config zegt %s (dim %s) - "
            "draai 'polaris bouw' opnieuw" % (m.get("model"), m.get("dim"),
                                            config.model_repo, config.dim))

    con = sqlite3.connect(config.db_pad)
    con.row_factory = sqlite3.Row
    try:
        woordscore = {}
        rangorde = "bm25(stuk_fts, %s)" % ", ".join(str(g) for g in BM25_GEWICHTEN)
        for query, gewicht in _fts_pogingen(vraag):
            try:
                rijen = con.execute(
                    "SELECT id FROM stuk_fts WHERE stuk_fts MATCH ? ORDER BY %s LIMIT %d"
                    % (rangorde, KANDIDATEN), (query,)).fetchall()
            except sqlite3.OperationalError:
                continue
            if rijen:
                for rang, r in enumerate(rijen, 1):
                    woordscore[r["id"]] = (gewicht, rang)
                break

        vectorscore = {}
        rijen = con.execute("SELECT id, vec FROM vector").fetchall()
        if rijen:
            ids = [r["id"] for r in rijen]
            mat = np.frombuffer(b"".join(r["vec"] for r in rijen),
                                dtype=np.float32).reshape(len(ids), config.dim)
            sims = mat @ Embedder(config).query(vraag)
            for rang, i in enumerate(np.argsort(-sims)[:KANDIDATEN], 1):
                vectorscore[ids[i]] = rang

        scores = {}
        for id_, (gewicht, rang) in woordscore.items():
            scores[id_] = scores.get(id_, 0.0) + gewicht / (RRF_K + rang)
        for id_, rang in vectorscore.items():
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (RRF_K + rang)

        uit = []
        for id_, score in scores.items():
            r = con.execute("SELECT * FROM stuk WHERE id=?", (id_,)).fetchone()
            if r is None:
                continue
            if (r["status"] or "").lower() in VERVALLEN_STATUSSEN:
                score *= 0.5
            rij = dict(r)
            rij["score"] = round(score, 5)
            uit.append(rij)
        uit.sort(key=lambda x: -x["score"])
        return uit[:k]
    finally:
        con.close()
