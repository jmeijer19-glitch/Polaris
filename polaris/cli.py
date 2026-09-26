# -*- coding: utf-8 -*-
"""Opdrachtregel voor Polaris.

    polaris bouw      --config polaris.toml         index (her)bouwen
    polaris zoek      --config polaris.toml "vraag"  hybride zoekopdracht
    polaris info      --config polaris.toml         wat zit er in de index
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
from . import index as indexmod
from . import update as updatemod
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
                         ("info", "toon wat er in de index zit"),
                         ("model", "haal het embeddingmodel vooraf op")):
        sp = sub.add_parser(naam, help=uitleg)
        sp.add_argument("--config", default="polaris.toml")

    p_zoek = sub.add_parser("zoek", help="hybride zoekopdracht")
    p_zoek.add_argument("--config", default="polaris.toml")
    p_zoek.add_argument("-k", type=int, default=8, help="aantal resultaten (standaard 8)")
    p_zoek.add_argument("--json", action="store_true",
                        help="resultaten als JSON, voor gebruik door een ander programma")
    p_zoek.add_argument("vraag", nargs="+")

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
        indexmod.bouw(cfg)

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

    elif args.cmd == "zoek":
        if not os.path.exists(cfg.db_pad):
            print("geen index op %s - draai eerst: polaris bouw --config %s"
                  % (cfg.db_pad, args.config))
            sys.exit(1)
        vraag = " ".join(args.vraag)
        try:
            resultaten = indexmod.zoek(cfg, vraag, k=args.k)
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
            print("   %s" % r["tekst"][:220].replace("\n", " "))
            if r.get("verwijzingen"):
                print("   live-referentie(s): %s - overweeg dit vers op te halen "
                      "in plaats van de index te vertrouwen" % r["verwijzingen"])
            print()


if __name__ == "__main__":
    main()
