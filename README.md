# 📰 Veille Crypto Hebdomadaire

Pipeline automatisé de veille sur les crypto-actifs, filtré selon un programme de référence sur les crypto-actifs.

Chaque vendredi soir, le pipeline :
1. Collecte les articles des 27 flux RSS surveillés
2. Filtre et résume en français via l'API Mistral
3. Génère une note Markdown structurée dans `notes/`
4. Regroupe les articles retenus par sujet réel, rédige un article de blog explicatif par sujet majeur, le fait vérifier par recherche web live (Perplexity Agent API) avant publication sur `docs/` (GitHub Pages) — voir [Blog public](#blog-public)
5. Commite et push automatiquement dans ce repo

## Installation & Configuration

### 1. Forker / cloner ce repo sur GitHub

### 2. Ajouter les secrets
Dans ton repo GitHub :
`Settings → Secrets and variables → Actions → New repository secret`

- `MISTRAL_API_KEY` : ta clé API Mistral (https://console.mistral.ai)
- `PERPLEXITY_API_KEY` : ta clé API Perplexity (https://www.perplexity.ai/settings/api)
- `PERPLEXITY_PRESET` : le preset Agent API à utiliser pour la vérification (`fast`, `low`, `medium`, `high`, `xhigh` — équivalents approximatifs des anciens niveaux Sonar, du plus économique au plus poussé). `low` est un point de départ raisonnable pour vérifier des faits d'actualité. L'Agent API est très récente : vérifier que ce mapping tient toujours sur [docs.perplexity.ai/docs/agent-api/migrate-from-sonar](https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview) avant de fixer la valeur.
- `NTFY_TOPIC` : le topic [ntfy](https://ntfy.sh) sur lequel recevoir une alerte si un article est rejeté à la vérification

⚠️ Sonar Chat Completions (l'ancienne API Perplexity) est retiré le 27/09/2026 — ce pipeline utilise directement la nouvelle Agent API.

### 3. Activer GitHub Actions
`Actions → Enable Actions` (si désactivé)

### 4. Activer GitHub Pages (pour le blog public)
`Settings → Pages → Source: Deploy from a branch → main → /docs`

Nécessite un repo public (ou un forfait payant pour Pages sur repo privé) — voir la discussion sur la visibilité du repo.

### 5. Récupérer les notes dans Obsidian

**Option A — Pull manuel (recommandé pour commencer)**
```bash
# Dans le dossier de ton coffre Obsidian (synchronisé PCloud)
git clone https://github.com/TON_USERNAME/crypto-veille.git
# Chaque samedi matin :
cd crypto-veille && git pull
```

**Option B — Script automatique (macOS)**
Ajouter dans crontab (`crontab -e`) :
```
0 8 * * 6 cd /chemin/vers/coffre/crypto-veille && git pull
```

Cela pull automatiquement chaque samedi à 8h00.

### 6. Lier au coffre Obsidian
Dans Obsidian, crée un lien symbolique ou place le dossier `notes/` directement dans ton coffre.
Les notes sont taggées `#veille #crypto #master #cours`.

## Déclenchement manuel
Dans GitHub : `Actions → Veille Crypto Hebdomadaire → Run workflow`

## Structure des notes
```
notes/
└── Veille-Crypto-2025-01-20.md   ← une note par semaine
```

Chaque note contient :
- Un sommaire par catégorie (Sécurité/Hack, Régulation, DeFi, etc.)
- Pour chaque article : résumé en français, lien source, chapitre du cours concerné, intérêt pédagogique

## Blog public

Depuis les mêmes articles filtrés, le pipeline génère un blog public statique, sans aucune intervention manuelle :

```
docs/                      ← servi par GitHub Pages
├── index.html              ← liste des articles, régénérée à chaque run
├── articles.json           ← manifest (titre, date, extrait) utilisé pour régénérer index.html
├── style.css
└── articles/
    └── 2026-08-21-nom-du-sujet.html
```

Pipeline par sujet (`scripts/blog.py`) :
1. **Regroupement** — les articles filtrés qui parlent du même événement (ex. un hack couvert par 3 médias) sont fusionnés en un seul sujet ; seuls les sujets jugés "majeurs" (impact généraliste) donnent lieu à un article.
2. **Rédaction** — Mistral rédige un article explicatif de 500-700 mots à partir des sources du sujet.
3. **Vérification** — l'article est vérifié par recherche web live via l'Agent API Perplexity (pas seulement contre le texte fourni : capable de détecter une source RSS elle-même erronée).
4. **Publication ou rejet** — si approuvé, l'article est publié dans `docs/`. Sinon, il est sauvegardé dans `rejected/AAAA-MM-JJ-slug.md` avec le verdict, et une notification ntfy est envoyée (rien n'est publié sans vérification).

Pour ajuster le ton ou la longueur des articles : modifier le prompt dans `write_article()` (`scripts/blog.py`). Pour ajuster le seuil de sélection des sujets : modifier le prompt dans `group_by_story()`.

## Sources surveillées (27 flux RSS)
CoinDesk, CoinTelegraph, Decrypt, The Block, Blockworks, Cryptoast, Journal du Coin,
Ethereum Blog, Bitcoin Optech, Vitalik Blog, Trail of Bits, Rekt News, Immunefi,
Uniswap Blog, Aave Blog, DeFi Llama, The Defiant, AMF France, ESMA,
Bitcoin Magazine, a16z Crypto, Gauntlet, Dragonfly Research...

## Modifier les sources ou le filtrage
- **Ajouter une source RSS** : éditer `RSS_FEEDS` dans `scripts/veille.py`
- **Affiner le filtrage** : modifier le prompt dans la fonction `filter_and_summarize()`
- **Changer le programme** : modifier `PROGRAMME_COURS` dans `scripts/veille.py`
