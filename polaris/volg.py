# -*- coding: utf-8 -*-
"""polaris.volg - de index bijhouden terwijl de bronnen veranderen.

Kijkt elke paar seconden naar de bronnen uit de config (alleen wijzigingstijd en grootte,
geen inhoud) en draait `ververs` zodra er iets veranderd is en het daarna een tijdje
stil blijft. Dat wachten voorkomt dat een editor die een bestand in drie stappen opslaat,
of een synchronisatie die honderd bestanden neerzet, honderd keer een ververs geeft.

Wanneer er gebouwd moet worden, weet een aanroepend programma meestal zelf het best; dat
roept dan gewoon `index.ververs()` aan. Deze modus is voor een kennisbank die alleen uit
bestanden bestaat. Stukken die een programma aanlevert (`extra_stukken`) ziet hij niet.
"""
import os
import time

from . import index as indexmod


def stand(config):
    """Een vingerafdruk van de bronnen: per bestand pad, wijzigingstijd en grootte."""
    uit = []
    for bron in config.bronnen:
        if bron.type == "markdown" and os.path.isdir(bron.pad):
            for dirpad, dirs, files in os.walk(bron.pad):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if f.endswith(".md") and not f.startswith("."):
                        vol = os.path.join(dirpad, f)
                        try:
                            st = os.stat(vol)
                        except OSError:
                            continue
                        uit.append((vol, st.st_mtime_ns, st.st_size))
        elif os.path.exists(bron.pad):
            st = os.stat(bron.pad)
            uit.append((bron.pad, st.st_mtime_ns, st.st_size))
    return tuple(uit)


def volg(config, interval=5.0, rust=20.0, embedder=None, eenmalig=False, verbose=True):
    """Ververs nu, en daarna telkens als de bronnen `rust` seconden stil zijn na een
    wijziging. `eenmalig` is voor tests: stop na de eerste ververs die op een wijziging
    volgde."""
    indexmod.ververs(config, verbose=verbose, embedder=embedder)
    verwerkt = stand(config)
    laatst_gezien, sinds = verwerkt, None
    if verbose:
        print("volgt %d bestand(en); ververst %g s nadat een wijziging stil is gevallen "
              "(stoppen met Ctrl+C)" % (len(verwerkt), rust), flush=True)
    while True:
        time.sleep(interval)
        nu = stand(config)
        if nu != laatst_gezien:
            laatst_gezien, sinds = nu, time.time()
            continue
        if nu != verwerkt and sinds is not None and time.time() - sinds >= rust:
            try:
                indexmod.ververs(config, verbose=verbose, embedder=embedder)
                verwerkt = nu
            except indexmod.BouwBezig as e:
                print("%s - volgende ronde opnieuw" % e, flush=True)
            except ValueError as e:
                print("ververs mislukt: %s" % e, flush=True)
                verwerkt = nu
            if eenmalig:
                return
