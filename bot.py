"""
BOT PAPER TRADING - marchés de prédiction (ARGENT FICTIF)
1. Récupère les marchés publics Polymarket (lecture seule, aucun pari réel)
2. Demande à Gemini une proba "à l'aveugle" (l'IA ne voit PAS le prix du marché)
3. Mise fictivement si l'écart IA / marché est gros (Kelly fractionné)
4. Clôture les positions quand les marchés sont résolus
5. Mesure si l'IA bat vraiment le marché (score de Brier) -> RAPPORT.md
"""
import os
import re
import json
import time
from datetime import datetime, timezone

import requests

# ================= RÉGLAGES =================
CAPITAL_DEPART = 1000.0     # capital fictif en $
MARCHES_PAR_RUN = 4         # marchés analysés à chaque passage (quota gratuit)
EDGE_MIN = 0.10             # écart mini IA vs marché pour miser (10 points)
EDGE_MAX = 0.30             # au-delà de 30 points, c'est sûrement l'IA qui se trompe -> pas de pari
KELLY_FRACTION = 0.25       # quart de Kelly (prudent)
MISE_MAX_PCT = 0.05         # jamais plus de 5 % du capital sur un trade
MISE_MIN = 1.0              # mise mini en $
SLIPPAGE = 0.01             # on paie 1 centime de plus que le prix affiché
JOURS_MIN, JOURS_MAX = 0.5, 21   # fenêtre globale des marchés acceptés
JOURS_COURT = 3             # "court" = se termine dans 3 jours max
PART_COURTS = 0.5           # moitié des analyses sur des marchés courts
LIQUIDITE_MIN = 5000        # ignore les marchés trop petits
PRIX_MIN, PRIX_MAX = 0.05, 0.95
PAUSE_IA = 13               # secondes entre 2 appels IA (limites gratuites)
MODELES = [m for m in [os.getenv("GEMINI_MODEL"), "gemini-flash-latest",
                       "gemini-2.5-flash", "gemini-2.5-flash-lite"] if m]
USE_SEARCH = os.getenv("USE_GOOGLE_SEARCH", "1") == "1"   # l'IA cherche les actus elle-même

GAMMA = "https://gamma-api.polymarket.com"
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RAPPORT = "RAPPORT.md"


def now():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def parse_liste(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def parse_date(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ================= ÉTAT =================
def charger_etat():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"capital": CAPITAL_DEPART, "positions": [], "historique": [], "predictions": []}


def sauver_etat(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ================= MARCHÉS =================
def recuperer_marches():
    r = requests.get(f"{GAMMA}/markets", timeout=30, params={
        "active": "true", "closed": "false", "limit": 200,
        "order": "volume24hr", "ascending": "false"})
    r.raise_for_status()
    return r.json()


def candidat(m):
    try:
        outcomes = [o.lower() for o in parse_liste(m.get("outcomes"))]
        prix = [float(p) for p in parse_liste(m.get("outcomePrices"))]
    except (ValueError, TypeError):
        return None
    if outcomes != ["yes", "no"] or len(prix) != 2:
        return None
    p_yes = prix[0]
    if not PRIX_MIN <= p_yes <= PRIX_MAX:
        return None
    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
    fin = parse_date(m.get("endDate"))
    if liq < LIQUIDITE_MIN or not fin:
        return None
    jours = (fin - now()).total_seconds() / 86400
    if not JOURS_MIN <= jours <= JOURS_MAX:
        return None
    return {"id": str(m["id"]), "question": m.get("question", ""),
            "regles": (m.get("description") or "")[:1500],
            "p_yes": p_yes, "fin": fin.isoformat(), "slug": m.get("slug", "")}


_cache = {}


def resultat_marche(mid):
    """None = pas encore résolu, sinon valeur d'une part YES (1, 0 ou 0.5)."""
    if mid in _cache:
        return _cache[mid]
    res = None
    try:
        r = requests.get(f"{GAMMA}/markets/{mid}", timeout=30)
        if r.status_code == 200:
            m = r.json()
            if m.get("closed"):
                p = float(parse_liste(m.get("outcomePrices"))[0])
                for v in (0.0, 0.5, 1.0):
                    if abs(p - v) < 0.01:
                        res = v
    except (requests.RequestException, ValueError, IndexError) as e:
        log(f"Erreur résolution {mid}: {e}")
    _cache[mid] = res
    return res


# ================= NEWS (optionnel) =================
def actus(question):
    key = os.getenv("GNEWS_API_KEY")
    if not key:
        return ""
    mots = [w for w in re.findall(r"[A-Za-z0-9']+", question) if len(w) > 3][:6]
    try:
        r = requests.get("https://gnews.io/api/v4/search", timeout=20, params={
            "q": " ".join(mots), "lang": "en", "max": 5, "apikey": key})
        arts = r.json().get("articles", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return ""
    return "\n".join(f"- {a.get('publishedAt', '')[:10]} {a.get('title', '')}: "
                     f"{(a.get('description') or '')[:200]}" for a in arts)


# ================= IA =================
def appel_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    for modele in MODELES:
        for recherche in ([True, False] if USE_SEARCH else [False]):
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2}}
            if recherche:
                body["tools"] = [{"google_search": {}}]
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent",
                    headers={"x-goog-api-key": key}, json=body, timeout=120)
            except requests.RequestException as e:
                log(f"{modele}: erreur réseau {e}")
                continue
            if r.status_code == 200:
                try:
                    cand = r.json()["candidates"][0]
                    parts = cand["content"]["parts"]
                    gm = cand.get("groundingMetadata") or {}
                    a_cherche = bool(gm.get("webSearchQueries") or gm.get("groundingChunks"))
                    return "".join(p.get("text", "") for p in parts), modele, a_cherche
                except (KeyError, IndexError):
                    log(f"{modele}: réponse vide")
                    continue
            log(f"{modele} (recherche={recherche}): HTTP {r.status_code} {r.text[:120]}")
            if r.status_code == 429:
                time.sleep(20)
    return None, None, False


def estimer(c):
    contexte = actus(c["question"]) or "none provided (use search if available)"
    prompt = f"""You are an elite superforecaster. Today (UTC): {now():%Y-%m-%d %H:%M}.
Question: {c['question']}
Resolution rules: {c['regles']}
Deadline: {c['fin']}
Recent news: {contexte}

Estimate the probability this resolves YES. Consider base rates, time remaining,
the status quo (most things don't change quickly), and the exact resolution wording.
Be calibrated: avoid extremes unless evidence is overwhelming.
Reply with ONLY this JSON, nothing else:
{{"proba": <0-1>, "confiance": "faible|moyenne|forte", "raison": "<1 phrase en français>"}}"""
    texte, modele, a_cherche = appel_gemini(prompt)
    if not texte:
        return None
    m = re.search(r"\{.*\}", texte, re.S)
    try:
        d = json.loads(m.group(0))
        d["proba"] = min(max(float(d["proba"]), 0.01), 0.99)
        d["modele"] = modele
        d["recherche"] = a_cherche
        return d
    except (AttributeError, ValueError, KeyError, TypeError):
        log(f"JSON illisible: {texte[:150]}")
        return None


# ================= DÉCISION =================
def decider(p_ia, p_yes, capital):
    if p_ia - p_yes >= EDGE_MIN:
        cote, prix, p_win = "YES", p_yes + SLIPPAGE, p_ia
    elif p_yes - p_ia >= EDGE_MIN:
        cote, prix, p_win = "NO", (1 - p_yes) + SLIPPAGE, 1 - p_ia
    else:
        return None
    prix = min(prix, 0.99)
    kelly = (p_win - prix) / (1 - prix)
    if kelly <= 0:
        return None
    mise = min(capital * kelly * KELLY_FRACTION, capital * MISE_MAX_PCT)
    return (cote, round(prix, 3), round(mise, 2)) if mise >= MISE_MIN else None


# ================= RÉSOLUTION =================
def resoudre(st):
    restantes = []
    for p in st["positions"]:
        res = resultat_marche(p["id"])
        if res is None:
            restantes.append(p)
            continue
        valeur_part = res if p["cote"] == "YES" else 1 - res
        retour = p["mise"] / p["prix"] * valeur_part
        st["capital"] += retour
        p.update(resultat=res, pnl=round(retour - p["mise"], 2), cloture=now().isoformat())
        st["historique"].append(p)
        log(f"Clôturé: {p['question'][:60]} -> PnL {p['pnl']:+.2f} $")
    st["positions"] = restantes

    for pr in st["predictions"]:
        if "resultat" in pr or parse_date(pr["fin"]) > now():
            continue
        res = resultat_marche(pr["id"])
        if res in (0.0, 1.0):
            pr["resultat"] = res
        elif res == 0.5:
            pr["resultat"] = None   # annulé, exclu des stats


# ================= RAPPORT =================
def rapport(st):
    engage = sum(p["mise"] for p in st["positions"])
    total = st["capital"] + engage
    roi = (total / CAPITAL_DEPART - 1) * 100
    h = st["historique"]
    gagnes = sum(1 for t in h if t["pnl"] > 0)
    res = [p for p in st["predictions"] if p.get("resultat") in (0.0, 1.0)]
    L = ["# 📊 Rapport du bot (argent FICTIF)",
         f"_Mis à jour : {now():%d/%m/%Y %H:%M} UTC_", "",
         f"**Capital total : {total:.2f} $** (départ {CAPITAL_DEPART:.0f} $) → **{roi:+.1f} %**",
         "",
         f"Dispo : {st['capital']:.2f} $ · Engagé : {engage:.2f} $ sur {len(st['positions'])} positions",
         "",
         f"Trades clôturés : {len(h)} · Gagnés : {gagnes}"
         + (f" ({gagnes / len(h) * 100:.0f} %)" if h else ""), "",
         "## 🧠 L'IA bat-elle le marché ?"]
    if res:
        b_ia = sum((p["p_ia"] - p["resultat"]) ** 2 for p in res) / len(res)
        b_m = sum((p["p_yes"] - p["resultat"]) ** 2 for p in res) / len(res)
        L += [f"Prédictions résolues : {len(res)}",
              f"Score Brier IA : **{b_ia:.4f}** · Marché : **{b_m:.4f}** (plus bas = meilleur)"]
        if len(res) < 30:
            L.append("⏳ Pas encore assez de données (il en faut au moins 30, idéalement 100+).")
        elif b_ia < b_m:
            L.append("✅ L'IA fait mieux que le marché sur cet échantillon. À confirmer dans la durée.")
        else:
            L.append("❌ Le marché est plus précis que l'IA. Pas d'edge pour l'instant.")
    else:
        L.append("Aucune prédiction résolue pour l'instant.")
    L += ["", "## Positions ouvertes", "| Marché | Fin | Côté | Prix | Mise | IA | Marché |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {p['question'][:55]} | {parse_date(p['fin']):%d/%m} | {p['cote']} | {p['prix']:.2f} | {p['mise']:.2f} $ "
          f"| {p['p_ia']:.0%} | {p['p_yes']:.0%} |" for p in sorted(st["positions"], key=lambda p: p["fin"])]
    L += ["", "## 10 derniers trades clôturés", "| Marché | Côté | Mise | PnL |", "|---|---|---|---|"]
    L += [f"| {t['question'][:55]} | {t['cote']} | {t['mise']:.2f} $ | {t['pnl']:+.2f} $ |"
          for t in h[-10:][::-1]]
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ================= MAIN =================
def main():
    st = charger_etat()
    resoudre(st)

    if not os.getenv("GEMINI_API_KEY"):
        log("Pas de GEMINI_API_KEY : je mets juste à jour le rapport.")
    else:
        try:
            marches = recuperer_marches()
        except requests.RequestException as e:
            log(f"Impossible de récupérer les marchés : {e}")
            marches = []
        deja = {p["id"] for p in st["positions"]} | {p["id"] for p in st["predictions"]}
        cands = [c for c in map(candidat, marches) if c and c["id"] not in deja]
        log(f"{len(marches)} marchés récupérés, {len(cands)} nouveaux candidats")

        def jours(c):
            return (parse_date(c["fin"]) - now()).total_seconds() / 86400
        courts = [c for c in cands if jours(c) <= JOURS_COURT]
        longs = [c for c in cands if jours(c) > JOURS_COURT]
        n_courts = round(MARCHES_PAR_RUN * PART_COURTS)
        selection = courts[:n_courts] + longs[:MARCHES_PAR_RUN - n_courts]
        selection += [c for c in cands if c not in selection][:MARCHES_PAR_RUN - len(selection)]
        log(f"{len(courts)} courts / {len(longs)} longs dispo -> {len(selection)} analysés")

        for c in selection:
            log(f"Analyse : {c['question'][:80]}")
            est = estimer(c)
            time.sleep(PAUSE_IA)
            if not est:
                continue
            p_ia = est["proba"]
            st["predictions"].append({"id": c["id"], "question": c["question"], "p_ia": p_ia,
                                      "p_yes": c["p_yes"], "fin": c["fin"], "date": now().isoformat(),
                                      "recherche": est["recherche"]})
            log(f"  IA {p_ia:.0%} vs marché {c['p_yes']:.0%} ({est.get('confiance')}, "
                f"recherche={'oui' if est['recherche'] else 'NON'}) - {est.get('raison', '')}")
            if est.get("confiance") == "faible":
                continue
            if not est["recherche"]:
                log("  ⛔ Pas de pari : l'IA n'a pas cherché d'actus récentes")
                continue
            if abs(p_ia - c["p_yes"]) > EDGE_MAX:
                log("  ⛔ Pas de pari : écart trop gros, l'IA rate sûrement une info")
                continue
            d = decider(p_ia, c["p_yes"], st["capital"])
            if d:
                cote, prix, mise = d
                st["capital"] -= mise
                st["positions"].append({**c, "cote": cote, "prix": prix, "mise": mise, "p_ia": p_ia,
                                        "raison": est.get("raison", ""), "ouverture": now().isoformat()})
                log(f"  💸 PARI FICTIF {cote} à {prix:.2f} pour {mise:.2f} $")

    sauver_etat(st)
    rapport(st)
    log("Terminé.")


if __name__ == "__main__":
    main()
