# -*- coding: utf-8 -*-
"""Opdrachtregel voor Polaris.

    polaris bouw      --config polaris.toml         index (her)bouwen
    polaris ververs   --config polaris.toml         alleen bijwerken wat veranderd is
    polaris volg      --config polaris.toml         blijven verversen bij elke wijziging
    polaris zoek      --config polaris.toml "vraag"  hybride zoekopdracht (-k, --bron, --json)
    polaris info      --config polaris.toml         wat zit er in de index
    polaris eval      --config polaris.toml vragen.json   meetlat: staat het verwachte in de top-k?
    polaris model     --config polaris.toml         model vooraf ophalen (offline gebruik)
    polaris versie                                  versienummer
    polaris bijwerken                               nieuwste versie ophalen via git
"""
import argparse
import io
import json
import os
import sys

from . import config as configmod
from . import eval as evalmod
from . import index as indexmod
from . import update as updatemod
from . import volg as volgmod
from . import versie as versie_fn


def _laad_config(pad):
    if not os.path.exists(pad):
        print("configbestand niet gevonden: %s" % pad)
        print("Kopieer voorbeelden/polaris.toml en pas de bronnen aan.")
        sys.exit(1)
    try:
        return configmod.laad(pad)
    except ValueError as e:
        print("fout in de config: %s" % e)
        sys.exit(1)


def main(argv=None):
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    p = argparse.ArgumentParser(prog="polaris", description="Hybride zoekindex, lokaal.")
    sub = p.add_subparsers(dest="cmd", required=True)

    for naam, uitleg in (("bouw", "(her)bouw de index"),
                         ("ververs", "werk alleen bij wat veranderd is (snel)"),
                         ("info", "toon wat er in de index zit"),
                         ("model", "haal het embeddingmodel vooraf op")):
        sp = sub.add_parser(naam, help=uitleg)
        sp.add_argument("--config", default="polaris.toml")

    p_zoek = sub.add_parser("zoek", help="hybride zoekopdracht")
    p_zoek.add_argument("--config", default="polaris.toml")
    p_zoek.add_argument("-k", type=int, default=8, help="aantal resultaten (standaard 8)")
    p_zoek.add_argument("--json", action="store_true",
                        help="resultaten als JSON, voor gebruik door een ander programma")
    p_zoek.add_argument("--bron", default=None, help="alleen in deze bron zoeken")
    p_zoek.add_argument("vraag", nargs="+")

    p_volg = sub.add_parser("volg", help="blijf de bronnen volgen en ververs bij elke wijziging")
    p_volg.add_argument("--config", default="polaris.toml")
    p_volg.add_argument("--interval", type=float, default=5.0,
                        help="elke hoeveel seconden kijken (standaard 5)")
    p_volg.add_argument("--rust", type=float, default=20.0,
                        help="zoveel seconden stil na een wijziging voor er ververst wordt "
                             "(standaard 20)")

    p_eval = sub.add_parser("eval", help="evaluatieset draaien: vragen met verwacht resultaat")
    p_eval.add_argument("--config", default="polaris.toml")
    p_eval.add_argument("-k", type=int, default=8, help="binnen hoeveel treffers (standaard 8)")
    p_eval.add_argument("--json", action="store_true")
    p_eval.add_argument("vragen", help="JSON-bestand met [{vraag, verwacht, bron?}, ...]")

    sub.add_parser("versie", help="toon het versienummer")
    sub.add_parser("bijwerken", help="haal de nieuwste versie op via git pull")

    args = p.parse_args(argv)

    if args.cmd == "versie":
        print("Polaris %s" % versie_fn())
        return
    if args.cmd == "bijwerken":
        updatemod.bijwerken()
        return

    cfg = _laad_config(args.config)

    if args.cmd == "bouw":
        try:
            indexmod.bouw(cfg)
        except indexmod.BouwBezig as e:
            print(str(e))
            sys.exit(2)

    elif args.cmd == "ververs":
        try:
            indexmod.ververs(cfg)
        except indexmod.BouwBezig as e:
            print(str(e))
            sys.exit(2)

    elif args.cmd == "volg":
        try:
            volgmod.volg(cfg, interval=args.interval, rust=args.rust)
        except KeyboardInterrupt:
            print()

    elif args.cmd == "model":
        emb = indexmod.Embedder(cfg)
        map_ = emb.modelmap(downloaden=True)
        print("model beschikbaar: %s" % map_)
        print("Voor een machine zonder internet: kopieer deze map en zet in de config "
              "model_pad = \"<pad naar de kopie>\".")

    elif args.cmd == "info":
        if not os.path.exists(cfg.db_pad):
            print("geen index op %s - draai eerst: polaris bouw" % cfg.db_pad)
            sys.exit(1)
        for k, v in sorted(indexmod.meta(cfg.db_pad).items()):
            print("%-10s %s" % (k, v))
        print("%-10s %s" % ("bestand", cfg.db_pad))
        for bron, n in indexmod.per_bron(cfg.db_pad).items():
            print("%-10s %s: %d stukken" % ("bron", bron, n))

    elif args.cmd == "eval":
        if not os.path.exists(cfg.db_pad):
            print("geen index op %s - draai eerst: polaris bouw" % cfg.db_pad)
            sys.exit(1)
        try:
            vragen = evalmod.laad_vragen(args.vragen)
            uit = evalmod.evalueer(cfg, vragen, k=args.k)
        except (ValueError, OSError) as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(uit, ensure_ascii=False, indent=1) if args.json else evalmod.rapport(uit))

    elif args.cmd == "zoek":
        if not os.path.exists(cfg.db_pad):
            print("geen index op %s - draai eerst: polaris bouw --config %s"
                  % (cfg.db_pad, args.config))
            sys.exit(1)
        vraag = " ".join(args.vraag)
        try:
            resultaten = indexmod.zoek(cfg, vraag, k=args.k, bron=args.bron)
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        if args.json:
            print(json.dumps(resultaten, ensure_ascii=False, indent=1))
            return
        if not resultaten:
            print("niets gevonden")
        for i, r in enumerate(resultaten, 1):
            print("%d. [%s] %s › %s" % (i, r["bron"], r["titel"], r["sectie"]))
            print("   %s" % (r.get("fragment") or r["tekst"][:220].replace("\n", " ")))
            for extra in r.get("ook_in_dit_document", []):
                print("   ook in dit document › %s: %s"
                      % (extra["sectie"], (extra.get("fragment") or extra["tekst"])[:160]
                         .replace("\n", " ")))
            if r.get("verwijzingen"):
                print("   live-referentie(s): %s - overweeg dit vers op te halen "
                      "in plaats van de index te vertrouwen" % r["verwijzingen"])
            print()


if __name__ == "__main__":
    main()
