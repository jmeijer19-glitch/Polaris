# -*- coding: utf-8 -*-
"""Polaris - hybride zoekindex (woorden + betekenis) over je eigen kennisbank.

Zie README.md voor installatie en gebruik. Versie staat in ../VERSION, niet hier - dat
bestand is de ene plek waar het versienummer vandaan komt, ook voor het pakketscript.
"""
import os

_VERSION_PAD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "VERSION")


def versie():
    try:
        with open(_VERSION_PAD, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "onbekend"


__version__ = versie()
