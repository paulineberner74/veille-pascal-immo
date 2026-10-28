"""Veille des annonces de pascal-immo.fr (prix affiché + honoraires affichés).

Lancé automatiquement 2 fois par jour par GitHub (voir .github/workflows/veille.yml).
Pour chaque annonce de TRANSACTION en vente sur le site (le neuf est ignoré), on note :
  - la référence (= n° de mandat Immofacile),
  - le prix affiché (honoraires inclus),
  - le prix hors honoraires et les honoraires affichés (différence des deux),
  - le % d'honoraires et à la charge de qui.

Fichiers produits dans data/ :
  - mandats.json   : dernière valeur connue de CHAQUE mandat, même retiré du site
                     (c'est ce fichier que lit le bat de saisie des ventes) ;
  - historique.csv : un journal des événements (nouveau, baisse/hausse de prix,
                     retiré du site, remis en ligne) ;
  - annonces_en_ligne.csv : la photo des annonces au dernier passage.

Uniquement la bibliothèque standard de Python : rien à installer.
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://www.pascal-immo.fr/"
PAGE_1 = BASE + "annonces/transaction/Vente.html"
PAGE_N = BASE + "annonces/transaction_____{n}/vente.html"
DOSSIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
F_MANDATS = os.path.join(DOSSIER, "mandats.json")
F_HISTO = os.path.join(DOSSIER, "historique.csv")
F_ENLIGNE = os.path.join(DOSSIER, "annonces_en_ligne.csv")
PAUSE = 1.0          # secondes entre deux pages : on reste poli avec le site
MAX_PAGES = 50       # sécurité


def maintenant():
    # heure de Paris (UTC+1 / UTC+2 selon la saison, approximation suffisante ici)
    utc = datetime.now(timezone.utc)
    ete = 3 < utc.month < 11 or (utc.month == 3 and utc.day >= 25) or (utc.month == 10 and utc.day < 25)
    return (utc + timedelta(hours=2 if ete else 1)).strftime("%Y-%m-%d %H:%M")


def lire(url, essais=3):
    derniere = None
    for k in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "VeillePascalImmo/1.0 (usage interne agence)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                brut = r.read()
                m = re.search(rb'charset="?([\w-]+)', brut[:3000])
                enc = (m.group(1).decode() if m else "iso-8859-1").lower()
                if enc in ("iso-8859-1", "latin-1"):
                    enc = "windows-1252"
                return brut.decode(enc, errors="replace")
        except Exception as e:          # coupure réseau, site lent...
            derniere = e
            time.sleep(5 * (k + 1))
    raise RuntimeError(f"page illisible : {url} ({derniere})")


def montant(txt):
    if not txt:
        return None
    t = html.unescape(txt)
    t = re.sub(r"[^\d,.]", "", t.replace(" ", "").replace("\xa0", "").replace(" ", ""))
    t = t.replace(",", ".")
    try:
        return round(float(t), 2) if t else None
    except ValueError:
        return None


def texte(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def lister_fiches():
    p1 = lire(PAGE_1)
    nb = max([1] + [int(n) for n in re.findall(r"transaction_____(\d+)/vente\.html", p1)])
    fiches = []
    for n in range(1, min(nb, MAX_PAGES) + 1):
        h = p1 if n == 1 else lire(PAGE_N.format(n=n))
        for f in re.findall(r'href="(?:\.\./|https://www\.pascal-immo\.fr/)?(fiches/[^"#?]+\.html)"', h):
            if f not in fiches:
                fiches.append(f)
        time.sleep(PAUSE)
    return fiches


def lire_fiche(chemin):
    h = lire(BASE + chemin)
    ref = re.search(r'class="product-model">\s*R(?:&eacute;|é)f\s*:\s*([^<]+?)\s*<', h)
    if not ref:
        ref = re.search(r'R(?:&eacute;|é)f(?:&eacute;|é)rence\s+([\w-]+)', h)
    ttc = re.search(r'class="hono_inclus_price">(.*?)</span>', h, re.S)
    net = re.search(r'class="hono_net_price">(.*?)</span>', h, re.S)
    pct = re.search(r'class="hono_percent">(.*?)</span>', h, re.S)
    dp = re.search(r'data-price="(\d+)"', h)
    titre = re.search(r"<title>(.*?)</title>", h, re.S)
    prix = montant(ttc.group(1)) if ttc else (float(dp.group(1)) if dp else None)
    prix_net = montant(net.group(1)) if net else None
    pct_txt = texte(pct.group(1)) if pct else ""
    m_pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", pct_txt)
    charge = "vendeur" if "vendeur" in pct_txt.lower() else ("acquéreur" if "acqu" in pct_txt.lower() else "")
    hono = round(prix - prix_net, 2) if prix and prix_net else None
    return {
        "ref": texte(ref.group(1)) if ref else "",
        "prix_affiche": prix,
        "prix_net_vendeur": prix_net,
        "hono_affiches": hono,
        "hono_pct": float(m_pct.group(1).replace(",", ".")) if m_pct else None,
        "hono_charge": charge,
        "titre": texte(titre.group(1))[:120] if titre else "",
        "url": BASE + chemin,
    }


def charger_json(p, defaut):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def main():
    os.makedirs(DOSSIER, exist_ok=True)
    quand = maintenant()
    mandats = charger_json(F_MANDATS, {})
    fiches = lister_fiches()
    if not fiches:
        print("Aucune annonce trouvée : présentation du site changée ? Rien n'est modifié.")
        sys.exit(1)

    en_ligne, erreurs = {}, []
    for chemin in fiches:
        try:
            a = lire_fiche(chemin)
        except Exception as e:
            erreurs.append(f"{chemin} : {e}")
            continue
        if re.fullmatch(r"\d+", a["ref"] or ""):
            en_ligne[a["ref"]] = a
        elif a["ref"]:
            pass    # lot NEUF (réf. du type 38216-ELO-A405 ou 38498-3) : hors stats, ignoré
        else:
            erreurs.append(f"{chemin} : référence introuvable")
        time.sleep(PAUSE)

    # garde-fou : si on lit moins de la moitié des annonces habituelles, on
    # n'enregistre RIEN (sinon tout serait marqué "retiré du site" à tort)
    nb_avant = sum(1 for m in mandats.values() if m.get("en_ligne"))
    if nb_avant and len(en_ligne) < nb_avant / 2:
        print(f"Seulement {len(en_ligne)} annonces lues (contre {nb_avant} la dernière fois) : "
              f"passage ignoré par prudence.")
        sys.exit(1)

    evenements = []
    for ref, a in en_ligne.items():
        m = mandats.get(ref)
        if m is None:
            evenements.append((quand, ref, "nouveau", "", a["prix_affiche"], a["hono_affiches"], a["titre"]))
            m = mandats[ref] = {"premiere_vue": quand, "historique_prix": []}
        else:
            if not m.get("en_ligne"):
                evenements.append((quand, ref, "remis en ligne", m.get("prix_affiche"), a["prix_affiche"],
                                   a["hono_affiches"], a["titre"]))
            if a["prix_affiche"] and m.get("prix_affiche") and a["prix_affiche"] != m["prix_affiche"]:
                sens = "baisse de prix" if a["prix_affiche"] < m["prix_affiche"] else "hausse de prix"
                evenements.append((quand, ref, sens, m["prix_affiche"], a["prix_affiche"],
                                   a["hono_affiches"], a["titre"]))
        if not m["historique_prix"] or m["historique_prix"][-1]["prix_affiche"] != a["prix_affiche"] \
                or m["historique_prix"][-1].get("hono_affiches") != a["hono_affiches"]:
            m["historique_prix"].append({"date": quand, "prix_affiche": a["prix_affiche"],
                                         "hono_affiches": a["hono_affiches"]})
        m.update({k: v for k, v in a.items() if k != "ref"})
        m["derniere_vue"] = quand
        m["en_ligne"] = True
        m.pop("retire_le", None)

    for ref, m in mandats.items():
        if m.get("en_ligne") and ref not in en_ligne:
            m["en_ligne"] = False
            m["retire_le"] = quand
            evenements.append((quand, ref, "retiré du site", m.get("prix_affiche"), "",
                               m.get("hono_affiches"), m.get("titre", "")))

    with open(F_MANDATS, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(mandats.items())), f, ensure_ascii=False, indent=1)

    nouveau = not os.path.exists(F_HISTO)
    with open(F_HISTO, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if nouveau:
            w.writerow(["date", "mandat", "événement", "ancien prix", "nouveau prix", "honoraires affichés", "annonce"])
        w.writerows(evenements)

    with open(F_ENLIGNE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["mandat", "prix affiché", "prix hors honoraires", "honoraires affichés",
                    "% honoraires", "à la charge de", "annonce", "lien", "en ligne depuis"])
        for ref in sorted(en_ligne):
            a, m = en_ligne[ref], mandats[ref]
            w.writerow([ref, a["prix_affiche"], a["prix_net_vendeur"], a["hono_affiches"], a["hono_pct"],
                        a["hono_charge"], a["titre"], a["url"], m.get("premiere_vue", "")])

    print(f"{quand} : {len(en_ligne)} annonces lues, {len(evenements)} événement(s), {len(erreurs)} souci(s)")
    for e in evenements:
        print("  ", e[2], e[1], e[3], "->", e[4])
    for e in erreurs:
        print("  ! ", e)


if __name__ == "__main__":
    main()
