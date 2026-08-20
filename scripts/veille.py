"""
Pipeline de veille crypto-actifs
Génère une note Markdown hebdomadaire filtrée selon le programme du cours Master.
"""

import os
import json
import hashlib
import feedparser
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

import blog

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

MISTRAL_API_KEY = os.environ["MISTRAL_API_KEY"]
MISTRAL_MODEL   = "mistral-small-latest"   # suffisant pour du filtrage/résumé
OUTPUT_DIR      = Path(os.environ.get("OUTPUT_DIR", "./notes"))
MAX_AGE_DAYS    = 7                         # articles de la semaine écoulée
MAX_ARTICLES    = 80                        # plafond avant filtrage Mistral

# ─────────────────────────────────────────────
# SOURCES RSS (27 flux)
# ─────────────────────────────────────────────

RSS_FEEDS = [
    # Médias généralistes crypto
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",           "name": "CoinDesk"},
    {"url": "https://cointelegraph.com/rss",                             "name": "CoinTelegraph"},
    {"url": "https://decrypt.co/feed",                                   "name": "Decrypt"},
    {"url": "https://theblock.co/rss.xml",                               "name": "The Block"},
    {"url": "https://www.coinjournal.net/feed/",                         "name": "CoinJournal"},
    {"url": "https://cryptobriefing.com/feed/",                          "name": "Crypto Briefing"},
    {"url": "https://blockworks.co/feed",                                "name": "Blockworks"},
    {"url": "https://cryptoast.fr/feed/",                                 "name": "Cryptoast"},
    {"url": "https://journalducoin.com/feed/",                            "name": "Journal du Coin"},

    # Technique & protocoles
    {"url": "https://blog.ethereum.org/feed.xml",                        "name": "Ethereum Blog"},
    {"url": "https://bitcoinoptech.org/en/newsletters/feed.xml",         "name": "Bitcoin Optech"},
    {"url": "https://bitcoin.org/en/rss/releases.rss",                   "name": "Bitcoin Releases"},
    {"url": "https://vitalik.eth.limo/feed.xml",                         "name": "Vitalik Blog"},

    # Sécurité & vulnérabilités
    {"url": "https://blog.trailofbits.com/feed/",                        "name": "Trail of Bits"},
    {"url": "https://rekt.news/feed/",                                   "name": "Rekt News"},
    {"url": "https://consensys.io/blog/feed.rss",                        "name": "ConsenSys Blog"},
    {"url": "https://medium.com/feed/immunefi",                          "name": "Immunefi"},

    # DeFi
    {"url": "https://blog.uniswap.org/rss.xml",                         "name": "Uniswap Blog"},
    {"url": "https://medium.com/feed/aave",                              "name": "Aave Blog"},
    {"url": "https://defillama.com/blog/feed",                           "name": "DeFi Llama"},
    {"url": "https://thedefiant.io/feed",                                "name": "The Defiant"},

    # Régulation & institutionnel
    {"url": "https://www.amf-france.org/fr/rss/actualites",              "name": "AMF France"},
    {"url": "https://www.esma.europa.eu/sites/default/files/rss_feeds/esma_all_news.xml", "name": "ESMA"},
    {"url": "https://feeds.feedburner.com/bitcoinmagazine/full",         "name": "Bitcoin Magazine"},

    # Recherche & académique
    {"url": "https://a16zcrypto.com/feed/",                              "name": "a16z Crypto"},
    {"url": "https://medium.com/feed/gauntlet-networks",                 "name": "Gauntlet"},
    {"url": "https://medium.com/feed/dragonfly-research",                "name": "Dragonfly Research"},
]

# ─────────────────────────────────────────────
# PROGRAMME DU COURS (grille de filtrage)
# ─────────────────────────────────────────────

PROGRAMME_COURS = """
Programme de référence — Crypto-actifs

Chapitre 1 — Introduction & monnaie
Fonctions monétaires, limites des monnaies fiduciaires, genèse des crypto-actifs,
réseaux centralisés/décentralisés/distribués.

Chapitre 2 — Bitcoin : principes techniques
Blockchain, blocs, SHA-256, immutabilité, Proof of Work, attaque des 51%,
mempool, mining, halving, forks (soft/hard fork), exemples BCH et ETH/ETC.

Chapitre 3 — Wallets & sécurité
Clés publiques/privées, cryptographie asymétrique, seed phrases BIP-39,
types de wallets (hot/cold, hardware, custodial), attaques et vols.

Chapitre 4 — Ethereum
EVM, Solidity/Vyper, smart contracts, gas, dApps, Proof of Stake (The Merge),
slashing, protocoles alternatifs (DPoS, BFT, Algorand), DAOs, gouvernance,
Layer 2, sharding.

Chapitre 5 — Tokenisation
Taxonomy des tokens (utility, security, governance, stablecoins, NFT, SBT, RWA),
ICO/STO/airdrop, stablecoins (fiat, crypto-collatéralisés, algorithmiques),
MiCA (ART, EMT, utility tokens), PSAN/AMF, DLT Pilot Regime,
tokenisation des actifs financiers, CBDC, Institutional DeFi.

Chapitre 6 — DeFi
Money Legos, DEX (Uniswap, Curve), lending/borrowing (Aave, Compound),
liquid staking (Lido), yield aggregators, bridges, oracles,
risques (bugs smart contracts, hacks, manipulation d'oracle, risque systémique).
"""

# ─────────────────────────────────────────────
# ÉTAPE 1 : Collecte des articles RSS
# ─────────────────────────────────────────────

def collect_articles(max_age_days: int = MAX_AGE_DAYS) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    articles = []
    seen_hashes = set()

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries:
                # Extraction de la date
                published = None
                for date_field in ("published_parsed", "updated_parsed"):
                    if hasattr(entry, date_field) and getattr(entry, date_field):
                        import time
                        t = getattr(entry, date_field)
                        published = datetime(*t[:6], tzinfo=timezone.utc)
                        break

                if published and published < cutoff:
                    continue

                title   = getattr(entry, "title", "").strip()
                link    = getattr(entry, "link", "").strip()
                summary = getattr(entry, "summary", "")[:500].strip()

                if not title or not link:
                    continue

                # Déduplication par hash titre+source
                uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()
                if uid in seen_hashes:
                    continue
                seen_hashes.add(uid)

                articles.append({
                    "title":     title,
                    "link":      link,
                    "source":    feed_info["name"],
                    "published": published.strftime("%Y-%m-%d") if published else "N/A",
                    "summary":   summary,
                })

                if len(articles) >= MAX_ARTICLES:
                    break

        except Exception as e:
            print(f"[WARN] Erreur sur {feed_info['name']}: {e}")

    print(f"[INFO] {len(articles)} articles collectés ({MAX_AGE_DAYS} derniers jours)")
    return articles


# ─────────────────────────────────────────────
# ÉTAPE 2 : Filtrage et résumé par Mistral
# ─────────────────────────────────────────────

def filter_and_summarize(articles: list[dict]) -> list[dict]:
    """
    Envoie les articles par batch à Mistral.
    Retourne uniquement les articles jugés pertinents, avec résumé et catégorie.
    """

    BATCH_SIZE = 15
    results = []

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i:i + BATCH_SIZE]

        # Construction du prompt
        articles_text = ""
        for idx, art in enumerate(batch):
            articles_text += f"""
---
Article {idx + 1}
Titre : {art['title']}
Source : {art['source']}
Date : {art['published']}
Résumé brut : {art['summary']}
URL : {art['link']}
"""

        prompt = f"""Tu es assistant pédagogique pour un cours de Master universitaire sur les crypto-actifs.

Voici le programme du cours :
{PROGRAMME_COURS}

Voici {len(batch)} articles de la semaine. Pour chacun, tu dois décider s'il est pertinent pour enrichir ou mettre à jour ce cours Master.

Un article est PERTINENT s'il concerne : une attaque ou hack de protocole, une vulnérabilité découverte, un fork important, une mise à jour technique majeure (Ethereum, Bitcoin, Layer 2...), une avancée réglementaire (MiCA, AMF, SEC...), un nouveau projet DeFi ou tokenisation significatif, l'évolution des stablecoins ou CBDC, un exemple pédagogique concret pour illustrer un concept du cours.

Un article est NON PERTINENT s'il concerne : les prix et spéculation, les mouvements de marché, les opinions d'influenceurs, les nouveaux exchange CEX, le trading, les NFT artistiques sans intérêt technique.

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, sous cette forme exacte :
{{
  "articles": [
    {{
      "index": 1,
      "pertinent": true,
      "categorie": "Sécurité / Hack",
      "chapitre": "Chapitre 6 — DeFi",
      "resume_fr": "Résumé en français de 2-3 phrases. Termes techniques en anglais.",
      "interet_pedagogique": "Une phrase sur pourquoi c'est utile pour le cours."
    }},
    {{
      "index": 2,
      "pertinent": false
    }}
  ]
}}

Catégories possibles : "Sécurité / Hack", "Protocole / Fork", "Régulation", "DeFi", "Tokenisation / RWA", "Stablecoins / CBDC", "Layer 2 / Scalabilité", "Wallets / Sécurité", "Bitcoin", "Ethereum", "Gouvernance / DAO", "Institutionnel".

{articles_text}"""

        try:
            response = httpx.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {MISTRAL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MISTRAL_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4000,
                },
                timeout=60,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()

            # Nettoyage JSON si nécessaire
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            parsed = json.loads(content)

            for item in parsed.get("articles", []):
                if not item.get("pertinent"):
                    continue
                idx = item["index"] - 1
                if 0 <= idx < len(batch):
                    art = batch[idx]
                    results.append({
                        **art,
                        "categorie":           item.get("categorie", "Général"),
                        "chapitre":            item.get("chapitre", ""),
                        "resume_fr":           item.get("resume_fr", ""),
                        "interet_pedagogique": item.get("interet_pedagogique", ""),
                    })

        except Exception as e:
            print(f"[WARN] Erreur Mistral sur batch {i//BATCH_SIZE + 1}: {e}")

    print(f"[INFO] {len(results)} articles retenus après filtrage Mistral")
    return results


# ─────────────────────────────────────────────
# ÉTAPE 3 : Génération de la note Markdown
# ─────────────────────────────────────────────

CATEGORIES_ORDER = [
    "Sécurité / Hack",
    "Protocole / Fork",
    "Ethereum",
    "Bitcoin",
    "Layer 2 / Scalabilité",
    "DeFi",
    "Stablecoins / CBDC",
    "Tokenisation / RWA",
    "Régulation",
    "Gouvernance / DAO",
    "Wallets / Sécurité",
    "Institutionnel",
]

def generate_markdown(articles: list[dict], week_start: str, week_end: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Regroupement par catégorie
    by_cat: dict[str, list] = {}
    for art in articles:
        cat = art.get("categorie", "Général")
        by_cat.setdefault(cat, []).append(art)

    # Construction du document
    lines = [
        f"# 🗞️ Veille Crypto — Semaine du {week_start} au {week_end}",
        f"",
        f"> Généré automatiquement le {now} | {len(articles)} articles retenus | Filtré selon un programme de référence crypto-actifs",
        f"",
        f"---",
        f"",
        f"## Sommaire",
        f"",
    ]

    # Sommaire dynamique
    ordered_cats = [c for c in CATEGORIES_ORDER if c in by_cat]
    remaining    = [c for c in by_cat if c not in CATEGORIES_ORDER]
    for cat in ordered_cats + remaining:
        anchor = cat.lower().replace(" ", "-").replace("/", "").replace("é", "e").replace("è", "e")
        lines.append(f"- [{cat}](#{anchor}) ({len(by_cat[cat])})")

    lines += ["", "---", ""]

    # Sections par catégorie
    for cat in ordered_cats + remaining:
        anchor = cat.lower().replace(" ", "-").replace("/", "").replace("é", "e").replace("è", "e")
        lines.append(f"## {cat}")
        lines.append("")

        for art in by_cat[cat]:
            lines += [
                f"### [{art['title']}]({art['link']})",
                f"",
                f"**Source :** {art['source']} | **Date :** {art['published']} | **Chapitre :** {art['chapitre']}",
                f"",
                art['resume_fr'],
                f"",
                f"> 💡 *Intérêt pédagogique :* {art['interet_pedagogique']}",
                f"",
                "---",
                "",
            ]

    lines += [
        "## Métadonnées",
        "",
        f"- **Période couverte :** {week_start} → {week_end}",
        f"- **Sources surveillées :** {len(RSS_FEEDS)} flux RSS",
        f"- **Articles analysés :** {MAX_ARTICLES} max",
        f"- **Articles retenus :** {len(articles)}",
        f"- **Modèle de filtrage :** Mistral ({MISTRAL_MODEL})",
        f"- **Tags Obsidian :** #veille #crypto #master #cours",
        "",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Calcul de la semaine couverte (lundi → vendredi)
    today      = datetime.now(timezone.utc)
    monday     = today - timedelta(days=today.weekday())
    week_start = monday.strftime("%Y-%m-%d")
    week_end   = today.strftime("%Y-%m-%d")
    filename   = f"Veille-Crypto-{today.strftime('%Y-%m-%d')}.md"

    print(f"[INFO] Pipeline veille crypto — {week_start} → {week_end}")

    # Pipeline
    articles  = collect_articles()
    if not articles:
        print("[WARN] Aucun article collecté. Fin du pipeline.")
        return

    filtered  = filter_and_summarize(articles)
    if not filtered:
        print("[WARN] Aucun article retenu après filtrage. Fin du pipeline.")
        return

    markdown  = generate_markdown(filtered, week_start, week_end)

    output_path = OUTPUT_DIR / filename
    output_path.write_text(markdown, encoding="utf-8")
    print(f"[OK] Note générée : {output_path}")

    blog.run(filtered)


if __name__ == "__main__":
    main()
