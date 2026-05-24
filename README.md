# 📰 Veille Crypto Hebdomadaire

Pipeline automatisé de veille sur les crypto-actifs, filtré selon le programme du cours Master Crypto-actifs (Université Paris 1 Panthéon-Sorbonne).

Chaque vendredi soir, le pipeline :
1. Collecte les articles des 25 flux RSS surveillés
2. Filtre et résume en français via l'API Mistral
3. Génère une note Markdown structurée dans `notes/`
4. Commite et push automatiquement dans ce repo

## Installation & Configuration

### 1. Forker / cloner ce repo sur GitHub

### 2. Ajouter le secret Mistral
Dans ton repo GitHub :
`Settings → Secrets and variables → Actions → New repository secret`

- Nom : `MISTRAL_API_KEY`
- Valeur : ta clé API Mistral (https://console.mistral.ai)

### 3. Activer GitHub Actions
`Actions → Enable Actions` (si désactivé)

### 4. Récupérer les notes dans Obsidian

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

### 5. Lier au coffre Obsidian
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

## Sources surveillées (25 flux RSS)
CoinDesk, CoinTelegraph, Decrypt, The Block, Blockworks, Ethereum Blog,
Bitcoin Optech, Vitalik Blog, Trail of Bits, Rekt News, Immunefi,
Uniswap Blog, Aave Blog, DeFi Llama, The Defiant, AMF France, ESMA,
Bitcoin Magazine, a16z Crypto, Gauntlet, Dragonfly Research...

## Modifier les sources ou le filtrage
- **Ajouter une source RSS** : éditer `RSS_FEEDS` dans `scripts/veille.py`
- **Affiner le filtrage** : modifier le prompt dans la fonction `filter_and_summarize()`
- **Changer le programme** : modifier `PROGRAMME_COURS` dans `scripts/veille.py`
