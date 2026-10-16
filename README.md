# 🤖 Bot de paper trading sur marchés de prédiction

[![Bot trading](https://github.com/Mel21-tech/Bot/actions/workflows/bot.yml/badge.svg)](https://github.com/Mel21-tech/Bot/actions/workflows/bot.yml)

👉 **[Voir le rapport en direct](RAPPORT.md)**

---

## 🎯 L'objectif

Répondre à une seule question : **une IA gratuite prédit-elle mieux que les parieurs ?**

Le bot part de **1 000 $**, analyse des dizaines de marchés chaque heure, parie là où il pense voir une opportunité, et compare ses prédictions à celles du marché avec le **score de Brier** (plus il est bas, plus on est précis).

---

## ⚙️ Comment ça marche

```
Polymarket ──► Sélection des marchés ──► IA (à l'aveugle) ──► Décision ──► Paris ──► RAPPORT.md
```

1. **📥 Récupération** — les 500 marchés les plus actifs de Polymarket (API publique, lecture seule).
2. **🔎 Filtrage** — marchés Oui/Non, assez liquides (≥ 5 000 $), qui se terminent entre 12 h et 30 jours. Moitié de marchés courts (≤ 3 jours), moitié de longs.
3. **🧠 Estimation à l'aveugle** — l'IA reçoit la question et les règles **sans voir le prix du marché**, fait une recherche web, et donne sa probabilité.
4. **⚖️ Décision** — si l'écart entre l'IA et le marché est suffisant, le bot parie fictivement.
5. **✅ Résolution** — quand un marché se termine, le bot encaisse ou perd la mise.
6. **📊 Rapport** — mise à jour de `RAPPORT.md` à chaque passage.

### Les IA utilisées

| Ordre | IA | Rôle |
|---|---|---|
| 1 | **Gemini** (Google) | IA principale, avec recherche Google |
| 2 | **Groq** (GPT-OSS 120B / 20B) | Prend le relais si Gemini ne répond pas, avec recherche web intégrée |
| + | **GNews** | Actus récentes en complément |

---

## 🎲 Les 3 catégories de paris

Chaque pari est classé selon le prix du côté acheté :

| Catégorie | Prix acheté | Écart mini exigé | Mise max par pari | Profil |
|---|---|---|---|---|
| 🛡️ **Sûr** | ≥ 0,70 | 3 points | 5 % du capital | Gagne souvent, rapporte peu |
| ⚖️ **Équilibré** | 0,35 à 0,70 | 5 points | 3 % du capital | Entre les deux |
| 🎲 **Outsider** | < 0,35 | 8 points | 2 % du capital | Gagne rarement, rapporte gros |

La mise exacte est calculée avec un **quart de Kelly** : plus l'avantage estimé est grand, plus la mise est élevée, dans la limite du plafond.

Le rapport affiche la **rentabilité de chaque catégorie** séparément, pour savoir où l'IA est vraiment bonne.

---

## 🛡️ Garde-fous

- **Pas de pari sans recherche web** : une IA qui ne connaît pas l'actu du jour se trompe (ex. un match déjà joué).
- **Pas de pari si l'écart dépasse 30 points** : c'est presque toujours l'IA qui rate une info.
- **Pas de pari si l'IA a une confiance faible.**
- **Un seul camp par événement** : si le bot a pris l'outsider, il ne prend pas le favori, et inversement.
- **Max 75 % du capital engagé** en même temps, dont **20 % max sur les outsiders**.
- **1 centime de slippage** ajouté à chaque achat pour rester réaliste.
- **Arrêt à 15 minutes** d'analyse par run pour toujours sauvegarder les résultats.

---

## 📁 Structure du dépôt

```
├── bot.py                     # le bot
├── RAPPORT.md                 # le rapport, mis à jour automatiquement
├── data/state.json            # capital, positions, historique, prédictions
└── .github/workflows/bot.yml  # lancement automatique toutes les heures
```

---

## 🚀 Installation

Tout tourne sur **GitHub Actions**, aucun ordinateur nécessaire.

1. **Clés API** (gratuites) à ajouter dans *Settings → Secrets and variables → Actions* :

   | Secret | Où l'obtenir |
   |---|---|
   | `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) |
   | `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
   | `GNEWS_API_KEY` | [gnews.io](https://gnews.io) |

2. **Lancer** : onglet *Actions* → *Bot trading* → **Run workflow**.

3. **Lancement automatique** : le workflow est programmé toutes les heures. GitHub sautant parfois des créneaux, un service comme [cron-job.org](https://cron-job.org) peut déclencher le workflow via l'API GitHub pour plus de régularité.

> 🔒 Les clés ne doivent **jamais** être écrites dans le code, uniquement dans les Secrets.

---

## 🔧 Réglages

Tout se règle en haut de `bot.py` :

| Réglage | Valeur | Rôle |
|---|---|---|
| `CAPITAL_DEPART` | 1000 | Capital fictif de départ |
| `MARCHES_PAR_RUN` | 25 | Marchés analysés par passage |
| `EDGE_MIN` | 3 / 5 / 8 pts | Écart mini par catégorie |
| `MISE_MAX_CAT` | 5 / 3 / 2 % | Mise max par catégorie |
| `KELLY_FRACTION` | 0,25 | Prudence des mises |
| `EXPO_MAX` | 75 % | Capital max engagé |
| `JOURS_MIN` / `JOURS_MAX` | 0,5 / 30 | Durée des marchés acceptés |
| `DUREE_MAX_MIN` | 15 | Durée max d'analyse par run |

---

## 📏 Comment lire les résultats

- **Score de Brier IA < marché** → l'IA prédit mieux que les parieurs.
- **Score de Brier IA > marché** → le marché a raison plus souvent : pas d'avantage.
- Répondre « 50 % » à tout donne **0,25** : une IA au-dessus ne sert à rien.
- Il faut **au moins 30 prédictions résolues**, idéalement **100+**, avant de conclure quoi que ce soit.

---

## ⚖️ Avertissement

Projet **éducatif et expérimental**. Aucun argent réel n'est engagé, et rien ici n'est un conseil financier. Les paris sur Polymarket sont **interdits en France** : ce bot se contente de lire des données publiques pour une simulation.
