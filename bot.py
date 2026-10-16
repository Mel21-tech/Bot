"""
BOT PAPER TRADING - marchés de prédiction (ARGENT FICTIF)
1. Récupère les marchés publics Polymarket (lecture seule, aucun pari réel)
2. Demande à l'IA (Gemini, Groq en secours) une proba "à l'aveugle" (l'IA ne voit PAS le prix du marché)
3. Mise fictivement là où il y a un vrai écart, sur 3 catégories :
   🛡️ Sûr (favoris)  ·  ⚖️ Équilibré  ·  🎲 Outsider
4. Clôture les positions quand les marchés sont résolus
5. Mesure si l'IA bat le marché (score de Brier) + rentabilité par catégorie -> RAPPORT.md
"""
import os
import re
import json
import time
from datetime import datetime, timezone

import requests

# ================= RÉGLAGES =================
CAPITAL_DEPART = 1000.0     # capital fictif en $
MARCHES_PAR_RUN = 25        # marchés analysés à chaque passage (Groq permet plus de requêtes)
NB_MARCHES_API = 500        # marchés Polymarket récupérés (les plus actifs)
DUREE_MAX_MIN = 15          # le bot arrête d'analyser après 15 min (pour toujours sauvegarder)
PAUSE_IA = 2                # secondes entre 2 analyses
GNEWS_MAX_PAR_RUN = 4       # GNews gratuit = ~100 requêtes/jour

# Catégories selon le prix du côté acheté
SEUIL_SUR = 0.70            # côté acheté à 0.70 ou plus  -> 🛡️ Sûr
SEUIL_OUTSIDER = 0.35       # côté acheté à moins de 0.35 -> 🎲 Outsider (entre les deux : ⚖️ Équilibré)
EDGE_MIN = {"Sûr": 0.03, "Équilibré": 0.05, "Outsider": 0.08}   # avantage mini (proba IA - prix payé)
EV_MIN = 0.03               # gain espéré mini : +3 % par $ misé
EDGE_MAX = 0.30             # au-delà de 30 points d'écart, c'est sûrement l'IA qui se trompe

KELLY_FRACTION = 0.25       # quart de Kelly (prudent)
MISE_MAX_CAT = {"Sûr": 0.05, "Équilibré": 0.03, "Outsider": 0.02}   # mise max par pari, en % du capital
MISE_MIN = 1.0              # mise mini en $
EXPO_MAX = 0.75             # max 75 % du capital engagé en même temps
EXPO_CAT = {"Sûr": 0.75, "Équilibré": 0.75, "Outsider": 0.20}   # max engagé par catégorie (outsiders limités)
SLIPPAGE = 0.01             # on paie 1 centime de plus que le prix affiché

JOURS_MIN, JOURS_MAX = 0.5, 30   # fenêtre des marchés acceptés
JOURS_COURT = 3             # "court" = se termine dans 3 jours max
PART_COURTS = 0.5           # moitié des analyses sur des marchés courts
LIQUIDITE_MIN = 5000        # ignore les marchés trop petits
PRIX_MIN, PRIX_MAX = 0.03, 0.97
MODELES = []   # rempli automatiquement au démarrage (voir decouvrir_modeles)

GAMMA = "https://gamma-api.polymarket.com"
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RAPPORT = "RAPPORT.md"
CATS = [("Sûr", "🛡️"), ("Équilibré", "⚖️"), ("Outsider", "🎲")]

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
    tous = []
    for offset in range(0, NB_MARCHES_API, 250):
        r = requests.get(f"{GAMMA}/markets", timeout=30, params={
            "active": "true", "closed": "false", "limit": 250, "offset": offset,
            "order": "volume24hr", "ascending": "false"})
        r.raise_for_status()
        page = r.json()
        tous += page
        if len(page) < 250:
            break
    return tous


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
    evts = m.get("events") or [{}]
    event = str(evts[0].get("id") or m.get("slug") or m["id"])
    return {"id": str(m["id"]), "event": event, "question": m.get("question", ""),
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
_gnews = {"n": 0}


def actus(question):
    key = os.getenv("GNEWS_API_KEY")
    if not key or _gnews["n"] >= GNEWS_MAX_PAR_RUN:
        return ""
    _gnews["n"] += 1
    mots = [w for w in re.findall(r"[A-Za-z0-9']+", question) if len(w) > 3][:6]
    try:
        r = requests.get("https://gnews.io/api/v4/search", timeout=20, params={
            "q": " ".join(mots), "lang": "en", "max": 5, "apikey": key})
        arts = r.json().get("articles", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return ""
    return "\n".join(f"- {a.get('publishedAt', '')[:10]} {a.get('title', '')}: "
                     f"{(a.get('description') or '')[:200]}" for a in arts)


# ================= IA (Gemini d'abord, Groq en secours) =================
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MODELES_GEMINI, MODELES_GROQ = [], []
EPUISES = set()          # modèles inutilisables pour ce run (quota vide / indispo)
SANS_RECHERCHE = set()   # modèles Gemini dont la recherche Google est épuisée


def decouvrir_modeles():
    """Gemini : liste des modèles 'flash' dispo pour TA clé, du plus récent au plus ancien.
    Groq : GPT-OSS avec recherche web intégrée (browser_search)."""
    if os.getenv("GEMINI_API_KEY"):
        noms = []
        try:
            r = requests.get(GEMINI_URL, params={"pageSize": 200}, timeout=30,
                             headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            for m in r.json().get("models", []):
                nom = m.get("name", "").replace("models/", "")
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                if "flash" not in nom or any(x in nom for x in
                                             ("image", "tts", "audio", "live", "thinking", "exp", "embed")):
                    continue
                noms.append(nom)
        except (requests.RequestException, ValueError) as e:
            log(f"Liste des modèles Gemini impossible : {e}")

        def cle(n):
            v = [int(x) for x in re.findall(r"\d+", n.split("flash")[0])] or [0]
            return (-v[0], -(v[1] if len(v) > 1 else 0), "lite" in n, "preview" in n, "latest" in n, n)
        MODELES_GEMINI.extend(sorted(set(noms), key=cle)[:4] or ["gemini-flash-latest"])
    if os.getenv("GROQ_API_KEY"):
        MODELES_GROQ.extend(["openai/gpt-oss-120b", "openai/gpt-oss-20b"])
    MODELES.extend(MODELES_GEMINI + MODELES_GROQ)


def appel_gemini(prompt, a_des_actus):
    key = os.environ["GEMINI_API_KEY"]
    surcharges = 0
    for modele in MODELES_GEMINI:
        if modele in EPUISES or surcharges >= 2:
            continue
        modes = ([True] if modele not in SANS_RECHERCHE else []) + ([False] if a_des_actus else [])
        for recherche in modes:
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
            if recherche:
                body["tools"] = [{"google_search": {}}]
            try:
                r = requests.post(f"{GEMINI_URL}/{modele}:generateContent", json=body, timeout=120,
                                  headers={"x-goog-api-key": key})
            except requests.RequestException as e:
                log(f"{modele}: erreur réseau {e}")
                break
            if r.status_code == 200:
                try:
                    cand = r.json()["candidates"][0]
                    gm = cand.get("groundingMetadata") or {}
                    a_cherche = bool(gm.get("webSearchQueries") or gm.get("groundingChunks"))
                    return "".join(p.get("text", "") for p in cand["content"]["parts"]), modele, a_cherche
                except (KeyError, IndexError, ValueError):
                    log(f"{modele}: réponse vide")
                    break
            log(f"{modele} (recherche={'oui' if recherche else 'non'}): HTTP {r.status_code}")
            if r.status_code in (500, 503):
                surcharges += 1      # Google surchargé : on passe vite à la suite
                break
            if r.status_code == 429 and recherche:
                SANS_RECHERCHE.add(modele)
                continue
            if r.status_code in (429, 404, 403, 400):
                EPUISES.add(modele)
            break
    return None, None, False


def appel_groq(prompt, a_des_actus):
    """GPT-OSS avec l'outil browser_search de Groq (remplace compound, fermé le 21/09/2026).
    Sans recherche, seulement si on a des actus GNews à donner."""
    key = os.environ["GROQ_API_KEY"]
    for modele in MODELES_GROQ:
        if modele in EPUISES:
            continue
        modes = ([True] if modele not in SANS_RECHERCHE else []) + ([False] if a_des_actus else [])
        for recherche in modes:
            body = {"model": modele, "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}]}
            if recherche:
                body["tools"] = [{"type": "browser_search"}]
            try:
                r = requests.post(GROQ_URL, json=body, timeout=180,
                                  headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 429:   # limite par minute : on attend un peu et on réessaie 1 fois
                    attente = float(r.headers.get("retry-after", "0") or 0)
                    if 0 < attente <= 60:
                        log(f"  ⏳ {modele} : limite/minute, pause {attente:.0f}s")
                        time.sleep(attente + 1)
                        r = requests.post(GROQ_URL, json=body, timeout=180,
                                          headers={"Authorization": f"Bearer {key}"})
            except (requests.RequestException, ValueError) as e:
                log(f"{modele}: erreur réseau {e}")
                break
            if r.status_code == 200:
                try:
                    msg = r.json()["choices"][0]["message"]
                except (KeyError, IndexError, ValueError):
                    log(f"{modele}: réponse vide")
                    break
                outils = msg.get("executed_tools") or []
                a_cherche = recherche and any(
                    "search" in str(t.get("type", "")).lower() or t.get("search_results") for t in outils)
                return msg.get("content") or "", modele, a_cherche
            log(f"{modele} (recherche={'oui' if recherche else 'non'}): HTTP {r.status_code} "
                f"{r.text[:150].replace(chr(10), ' ')}")
            if recherche and r.status_code in (400, 429):
                SANS_RECHERCHE.add(modele)   # outil de recherche refusé/épuisé : essai sans
                continue
            if r.status_code in (429, 404, 403, 401):
                EPUISES.add(modele)
            break
    return None, None, False


def appel_ia(prompt, a_des_actus):
    if MODELES_GEMINI:
        res = appel_gemini(prompt, a_des_actus)
        if res[0]:
            return res
        if MODELES_GROQ:
            log("  ↪️ Gemini ne répond pas, je passe à Groq")
    if MODELES_GROQ:
        return appel_groq(prompt, a_des_actus)
    return None, None, False


def estimer(c):
    news = actus(c["question"])
    contexte = news or "none provided (search the web)"
    prompt = f"""You are an elite superforecaster. Today (UTC): {now():%Y-%m-%d %H:%M}.
Question: {c['question']}
Resolution rules: {c['regles']}
Deadline: {c['fin']}
Recent news: {contexte}

Search the web for the latest facts (scores, polls, official statements, live counts, schedules)
before answering. Estimate the probability this resolves YES. Consider base rates, time remaining,
the status quo (most things don't change quickly), and the exact resolution wording.
Be calibrated AND decisive: if the outcome is very likely or very unlikely, say so clearly
(e.g. 0.03 or 0.96). Do NOT drift toward 0.5 just to be safe: hedging is as costly as overconfidence.
Reply with ONLY this JSON, nothing else:
{{"proba": <0-1>, "confiance": "faible|moyenne|forte", "raison": "<1 phrase en français>"}}"""
    texte, modele, a_cherche = appel_ia(prompt, bool(news))
    if not texte:
        return None
    m = re.search(r"\{.*\}", texte, re.S)
    try:
        d = json.loads(m.group(0))
        d["proba"] = min(max(float(d["proba"]), 0.01), 0.99)
        d["modele"] = modele
        d["recherche"] = a_cherche or bool(news)   # recherche web OU actus GNews = infos récentes
        return d
    except (AttributeError, ValueError, KeyError, TypeError):
        log(f"JSON illisible: {texte[:150]}")
        return None


# ================= DÉCISION =================
def categorie(prix_cote):
    """prix_cote = prix du côté acheté (sans slippage)."""
    if prix_cote >= SEUIL_SUR:
        return "Sûr"
    if prix_cote < SEUIL_OUTSIDER:
        return "Outsider"
    return "Équilibré"


def cat_de(p):
    return p.get("categorie") or categorie(p["prix"] - SLIPPAGE)


def decider(p_ia, p_yes, st):
    """Teste les 2 côtés et garde le seul qui a un vrai avantage. Un seul côté par marché :
    soit le favori, soit l'outsider, jamais les deux."""
    choix = None
    for cote, prix_aff, p_win in (("YES", p_yes, p_ia), ("NO", 1 - p_yes, 1 - p_ia)):
        prix = min(prix_aff + SLIPPAGE, 0.99)
        cat = categorie(prix_aff)
        edge = p_win - prix
        ev = p_win / prix - 1
        if edge >= EDGE_MIN[cat] and ev >= EV_MIN:
            choix = (cote, prix, p_win, cat)
    if not choix:
        return None
    cote, prix, p_win, cat = choix

    total = st["capital"] + sum(p["mise"] for p in st["positions"])
    engage = total - st["capital"]
    engage_cat = sum(p["mise"] for p in st["positions"] if cat_de(p) == cat)
    kelly = (p_win - prix) / (1 - prix)
    mise = min(total * kelly * KELLY_FRACTION, total * MISE_MAX_CAT[cat],
               total * EXPO_MAX - engage, total * EXPO_CAT[cat] - engage_cat, st["capital"])
    if mise < MISE_MIN:
        return "plafond"
    return cote, round(prix, 3), round(mise, 2), cat


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
        p.update(resultat=res, pnl=round(retour - p["mise"], 2), cloture=now().isoformat(),
                 categorie=cat_de(p))
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
    pos, h = st["positions"], st["historique"]
    engage = sum(p["mise"] for p in pos)
    total = st["capital"] + engage
    pnl_tot = sum(t["pnl"] for t in h)
    gagnes = sum(1 for t in h if t["pnl"] > 0)
    L = ["# 📊 Rapport du bot (argent FICTIF)",
         f"_Mis à jour : {now():%d/%m/%Y %H:%M} UTC_", "",
         "## 💰 Résumé", "",
         "| | |", "|---|---|",
         f"| **Capital total** | **{total:.2f} $** ({(total / CAPITAL_DEPART - 1) * 100:+.1f} %) |",
         f"| Disponible | {st['capital']:.2f} $ |",
         f"| Engagé | {engage:.2f} $ sur {len(pos)} positions |",
         f"| Trades clôturés | {len(h)} · gagnés {gagnes}"
         + (f" ({gagnes / len(h) * 100:.0f} %)" if h else "") + " |",
         f"| Gains/pertes réalisés | {pnl_tot:+.2f} $ |", ""]

    # --- Rentabilité par catégorie
    L += ["## 📈 Rentabilité par catégorie", "",
          "| Catégorie | Clôturés | Gagnés | Misé | Gain/perte | ROI | En cours |",
          "|---|---|---|---|---|---|---|"]
    for cat, emo in CATS:
        hc = [t for t in h if cat_de(t) == cat]
        pc = [p for p in pos if cat_de(p) == cat]
        mise = sum(t["mise"] for t in hc)
        pnl = sum(t["pnl"] for t in hc)
        g = sum(1 for t in hc if t["pnl"] > 0)
        L.append(f"| {emo} {cat} | {len(hc)} | {g}" + (f" ({g / len(hc) * 100:.0f} %)" if hc else "")
                 + f" | {mise:.2f} $ | {pnl:+.2f} $ | " + (f"{pnl / mise * 100:+.1f} %" if mise else "–")
                 + f" | {len(pc)} ({sum(p['mise'] for p in pc):.0f} $) |")
    L += ["", "_🛡️ Sûr = on achète le favori (prix ≥ 0,70), mise max 5 % du capital · "
          "⚖️ Équilibré = 0,35 à 0,70, mise max 3 % · "
          "🎲 Outsider = prix < 0,35, rapporte gros mais gagne rarement, mise max 2 % "
          "(et 20 % du capital au total)._", ""]

    # --- Brier
    res = [p for p in st["predictions"] if p.get("resultat") in (0.0, 1.0)]
    L += ["## 🧠 L'IA bat-elle le marché ?", ""]
    if res:
        b_ia = sum((p["p_ia"] - p["resultat"]) ** 2 for p in res) / len(res)
        b_m = sum((p["p_yes"] - p["resultat"]) ** 2 for p in res) / len(res)
        L += [f"Prédictions résolues : **{len(res)}** · Score Brier IA **{b_ia:.4f}** vs marché "
              f"**{b_m:.4f}** (plus bas = meilleur)", ""]
        if len(res) < 30:
            L.append("⏳ Pas encore assez de données (il en faut au moins 30, idéalement 100+).")
        elif b_ia < b_m:
            L.append("✅ L'IA fait mieux que le marché sur cet échantillon. À confirmer dans la durée.")
        else:
            L.append("❌ Le marché est plus précis que l'IA. Pas d'edge pour l'instant.")
    else:
        L.append("Aucune prédiction résolue pour l'instant.")
    L.append("")

    # --- Positions ouvertes par catégorie
    L += ["## 📂 Positions ouvertes", ""]
    if not pos:
        L += ["Aucune.", ""]
    for cat, emo in CATS:
        pc = sorted([p for p in pos if cat_de(p) == cat], key=lambda p: p["fin"])
        if not pc:
            continue
        L += [f"### {emo} {cat} ({len(pc)})", "",
              "| Marché | Fin | Pari | Prix | Mise | Gain si ✅ | IA (YES) | Marché (YES) |",
              "|---|---|---|---|---|---|---|---|"]
        L += [f"| {p['question'][:70]} | {parse_date(p['fin']):%d/%m} | {p['cote']} | {p['prix']:.2f} "
              f"| {p['mise']:.2f} $ | +{p['mise'] / p['prix'] - p['mise']:.2f} $ "
              f"| {p['p_ia']:.0%} | {p['p_yes']:.0%} |" for p in pc]
        L.append("")

    # --- Historique
    L += ["## 🧾 15 derniers trades clôturés", ""]
    if h:
        L += ["| | Marché | Cat. | Pari | Mise | Gain/perte |", "|---|---|---|---|---|---|"]
        emos = dict(CATS)
        L += [f"| {'✅' if t['pnl'] > 0 else '❌'} | {t['question'][:70]} | {emos[cat_de(t)]} | {t['cote']} "
              f"| {t['mise']:.2f} $ | {t['pnl']:+.2f} $ |" for t in h[-15:][::-1]]
    else:
        L.append("Aucun pour l'instant.")
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ================= MAIN =================
def main():
    debut = time.time()
    st = charger_etat()
    resoudre(st)

    decouvrir_modeles()
    if not MODELES:
        log("Ni GEMINI_API_KEY ni GROQ_API_KEY : je mets juste à jour le rapport.")
    else:
        log(f"Modèles dispo : {', '.join(MODELES)}")
        try:
            marches = recuperer_marches()
        except requests.RequestException as e:
            log(f"Impossible de récupérer les marchés : {e}")
            marches = []
        deja = {p["id"] for p in st["positions"]} | {p["id"] for p in st["predictions"]}
        events_pris = {p.get("event") or p["id"] for p in st["positions"]}
        cands = [c for c in map(candidat, marches) if c and c["id"] not in deja]
        log(f"{len(marches)} marchés récupérés, {len(cands)} nouveaux candidats")

        # 1 seul marché analysé par événement et par run (évite les doublons type tranches de tweets)
        vus, uniques = set(), []
        for c in cands:
            if c["event"] not in vus:
                vus.add(c["event"])
                uniques.append(c)

        def jours(c):
            return (parse_date(c["fin"]) - now()).total_seconds() / 86400
        courts = [c for c in uniques if jours(c) <= JOURS_COURT]
        longs = [c for c in uniques if jours(c) > JOURS_COURT]
        n_courts = round(MARCHES_PAR_RUN * PART_COURTS)
        selection = courts[:n_courts] + longs[:MARCHES_PAR_RUN - n_courts]
        selection += [c for c in uniques if c not in selection][:MARCHES_PAR_RUN - len(selection)]
        log(f"{len(courts)} courts / {len(longs)} longs dispo -> {len(selection)} analysés")

        for c in selection:
            if time.time() - debut > DUREE_MAX_MIN * 60:
                log(f"⏱️ {DUREE_MAX_MIN} min écoulées : je m'arrête là et je sauvegarde")
                break
            if len(EPUISES) >= len(MODELES):
                log("🛑 Quota IA épuisé (Gemini + Groq) : arrêt des analyses jusqu'au prochain run")
                break
            log(f"Analyse : {c['question'][:80]}")
            est = estimer(c)
            time.sleep(PAUSE_IA)
            if not est:
                continue
            p_ia = est["proba"]
            if not est["recherche"]:
                log("  ⏭️ Ignoré (pas de recherche web), il sera réanalysé plus tard")
                continue
            st["predictions"].append({"id": c["id"], "question": c["question"], "p_ia": p_ia,
                                      "p_yes": c["p_yes"], "fin": c["fin"], "date": now().isoformat(),
                                      "recherche": est["recherche"]})
            log(f"  IA {p_ia:.0%} vs marché {c['p_yes']:.0%} ({est.get('confiance')}) - {est.get('raison', '')}")
            if est.get("confiance") == "faible":
                log("  ⛔ Pas de pari : confiance faible")
                continue
            if abs(p_ia - c["p_yes"]) > EDGE_MAX:
                log("  ⛔ Pas de pari : écart trop gros, l'IA rate sûrement une info")
                continue
            if c["event"] in events_pris:
                log("  ⛔ Pas de pari : déjà une position sur cet événement (un seul camp par événement)")
                continue
            d = decider(p_ia, c["p_yes"], st)
            if d == "plafond":
                log("  ⛔ Pas de pari : plafond d'exposition atteint")
            elif d:
                cote, prix, mise, cat = d
                st["capital"] -= mise
                events_pris.add(c["event"])
                st["positions"].append({**c, "cote": cote, "prix": prix, "mise": mise, "p_ia": p_ia,
                                        "categorie": cat, "raison": est.get("raison", ""),
                                        "ouverture": now().isoformat()})
                log(f"  💸 PARI FICTIF {dict(CATS)[cat]} {cat} : {cote} à {prix:.2f} pour {mise:.2f} $")
            else:
                log("  · Pas d'avantage suffisant, pas de pari")

    sauver_etat(st)
    rapport(st)
    log("Terminé.")


if __name__ == "__main__":
    main()
