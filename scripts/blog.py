"""
Extension blog public du pipeline de veille crypto.

Part des articles déjà filtrés par veille.py (mêmes appels Mistral, pas de
duplication), les regroupe par sujet réel (plafonné à MAX_ARTICLES_PER_RUN
sujets les mieux classés), rédige un article explicatif par sujet via
l'Agent API Perplexity (recherche web dès l'écriture), le fait vérifier par
un second appel Perplexity indépendant, corrige via Mistral si besoin, puis
publie (docs/) ou rejette (rejected/ + notification ntfy).
"""

import os
import json
import re
import httpx
import bleach
import markdown as md_lib
from datetime import datetime, timezone
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from perplexity import Perplexity

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

MISTRAL_API_KEY    = os.environ["MISTRAL_API_KEY"]
MISTRAL_MODEL      = "mistral-small-latest"
# Rédaction sur un modèle plus capable que le filtrage/regroupement : les
# premiers runs réels ont montré mistral-small inventer des détails précis
# (sigles, montants, attributions légales) quand on lui demande d'élaborer
# 500-700 mots à partir de résumés courts. Surcoût négligeable au volume visé.
MISTRAL_WRITER_MODEL = "mistral-large-latest"  # utilisé par revise_article()

PERPLEXITY_API_KEY = os.environ["PERPLEXITY_API_KEY"]
# Agent API : Sonar Chat Completions est retiré le 27/09/2026, on utilise donc
# l'Agent API (client.responses.create), qui remplace le choix de modèle par
# des presets ("fast", "low", "medium", "high", "xhigh") — la recherche web
# s'active automatiquement selon le preset choisi. Cette offre est très
# récente : vérifier sur docs.perplexity.ai/docs/agent-api que le mapping
# ci-dessous est toujours d'actualité avant de s'y fier aveuglément.
PERPLEXITY_PRESET  = os.environ["PERPLEXITY_PRESET"]

NTFY_TOPIC = os.environ["NTFY_TOPIC"]

SCRIPT_DIR   = Path(__file__).parent
TEMPLATE_DIR = SCRIPT_DIR / "templates"
DOCS_DIR     = Path(os.environ.get("DOCS_DIR", "./docs"))
REJECTED_DIR = Path(os.environ.get("REJECTED_DIR", "./rejected"))

SEUIL_IMPORTANCE = "majeure"  # sujets "mineure" ignorés pour le blog
# Plafond dur sur le nombre de sujets traités par run, pour borner le coût
# même une semaine chargée. Si plus de sujets "majeure" que ce plafond, on
# ne garde que les mieux classés (voir "priorite" dans group_by_story).
MAX_ARTICLES_PER_RUN = 3

jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)

perplexity_client = Perplexity(api_key=PERPLEXITY_API_KEY)

ALLOWED_HTML_TAGS = ["p", "h2", "h3", "strong", "em", "a", "ul", "ol", "li", "blockquote", "br"]
ALLOWED_HTML_ATTRS = {"a": ["href", "title"]}


# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────

def _clean_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return content.strip()


def _slugify(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:80]


def send_ntfy(title: str, message: str) -> None:
    try:
        httpx.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Erreur envoi notification ntfy : {e}")


def _render_markdown(text: str) -> str:
    html = md_lib.markdown(text)
    return bleach.clean(html, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRS, strip=True)


def _plain_excerpt(markdown_text: str, length: int = 220) -> str:
    html = md_lib.markdown(markdown_text)
    # ignore les titres de section, ne garde que les paragraphes pour l'extrait
    paragraphs = re.findall(r"<p>(.*?)</p>", html, re.DOTALL)
    text = bleach.clean(" ".join(paragraphs), tags=[], strip=True).strip()
    return text[:length]


# ─────────────────────────────────────────────
# ÉTAPE 1 : Regroupement par sujet
# ─────────────────────────────────────────────

def group_by_story(filtered: list[dict]) -> list[dict] | None:
    """
    Regroupe les articles retenus par sujet réel (un hack couvert par 3 sources
    ne doit donner qu'un seul article de blog). Ne garde que les sujets jugés
    "majeure" : les news techniques trop niches pour un lectorat généraliste
    restent dans la note de cours (notes/) mais ne génèrent pas d'article public.
    """
    if not filtered:
        return []

    articles_text = ""
    for idx, art in enumerate(filtered):
        articles_text += f"""
---
Article {idx + 1}
Titre : {art['title']}
Source : {art['source']}
Résumé : {art['resume_fr']}
Catégorie : {art['categorie']}
"""

    prompt = f"""Tu es rédacteur en chef d'un blog public d'actualité crypto.

Voici {len(filtered)} articles retenus cette semaine (déjà filtrés pour leur pertinence). Plusieurs peuvent parler du même événement réel (ex. un hack couvert par CoinDesk, The Block et Decrypt) : regroupe-les par sujet.

Pour chaque sujet distinct, indique :
- "importance" : "majeure" (mérite un article de blog dédié pour un public généraliste — hack important, régulation majeure, mise à jour technique significative...) ou "mineure" (trop niche ou peu impactant pour un article grand public autonome)
- "priorite" : un entier de 1 à 10 sur l'intérêt/impact pour un lecteur généraliste (10 = à ne surtout pas manquer, 1 = mineur même parmi les sujets "majeure"). Uniquement pour les sujets "majeure".

Pour la priorité, privilégie le crypto "pur" (protocoles, hacks/sécurité, DeFi, mises à jour techniques Bitcoin/Ethereum/Layer 2, tokenisation) par rapport aux sujets de pure régulation/législation (lois, agences, textes en discussion) — sauf si la régulation a un impact direct et immédiat sur le marché ou les utilisateurs, pas juste une étape procédurale. Les sources étant majoritairement américaines, ne laisse pas la régulation US dominer artificiellement le classement au détriment de sujets crypto plus intrinsèquement intéressants.

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, sous cette forme exacte :
{{
  "sujets": [
    {{
      "titre_sujet": "Titre court du sujet",
      "importance": "majeure",
      "priorite": 7,
      "articles": [1, 4]
    }}
  ]
}}

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
        content = _clean_json(response.json()["choices"][0]["message"]["content"])
        parsed = json.loads(content)
    except Exception as e:
        print(f"[WARN] Erreur regroupement par sujet : {e}")
        return None  # échec technique, distinct d'une semaine sans sujet majeur ([])

    stories = []
    for sujet in parsed.get("sujets", []):
        if sujet.get("importance") != SEUIL_IMPORTANCE:
            continue
        indices = [i - 1 for i in sujet.get("articles", []) if 0 <= i - 1 < len(filtered)]
        if not indices:
            continue
        stories.append({
            "titre_sujet": sujet.get("titre_sujet", "Sans titre"),
            "priorite": sujet.get("priorite", 0),
            "articles": [filtered[i] for i in indices],
        })

    print(f"[INFO] {len(stories)} sujets majeurs identifiés (sur {len(parsed.get('sujets', []))} regroupés)")

    stories.sort(key=lambda s: s["priorite"], reverse=True)
    if len(stories) > MAX_ARTICLES_PER_RUN:
        print(f"[INFO] Plafond de {MAX_ARTICLES_PER_RUN} atteint, on ne garde que les mieux classés")
        stories = stories[:MAX_ARTICLES_PER_RUN]

    return stories


# ─────────────────────────────────────────────
# ÉTAPE 2 : Rédaction de l'article
# ─────────────────────────────────────────────

WRITE_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "article_redige",
        "schema": {
            "type": "object",
            "properties": {
                "titre": {"type": "string"},
                "contenu_markdown": {"type": "string"},
            },
            "required": ["titre", "contenu_markdown"],
        },
    },
}


def write_article(story: dict) -> dict | None:
    """
    Rédaction via l'Agent API Perplexity plutôt que Mistral : la recherche
    web se fait dès l'écriture (grounded dès le départ) plutôt qu'après coup
    lors de la vérification. Les résumés de story['articles'] servent de
    pistes de départ, pas de source exclusive.
    """
    sources_text = ""
    for art in story["articles"]:
        sources_text += f"""
---
Titre : {art['title']}
Source : {art['source']}
Résumé (analyse) : {art['resume_fr']}
Extrait brut de la source : {art['summary']}
URL : {art['link']}
"""

    prompt = f"""Tu es journaliste pour un blog public d'actualité crypto, destiné à un lectorat généraliste curieux mais non expert.

Sujet à traiter : {story['titre_sujet']}

Pistes de départ (à vérifier et compléter par ta propre recherche web, ne t'y limite pas) :
{sources_text}

Utilise ta recherche web pour vérifier et compléter chaque fait précis (chiffres, dates, noms, montants) plutôt que de te fier uniquement aux résumés ci-dessus.

Rédige un article explicatif en français de 500 à 700 mots :
- Un titre accrocheur mais factuel (pas putaclic, pas de superlatif du type "premier"/"record" sauf si confirmé par ta recherche)
- Une intro qui résume ce qui s'est passé
- Une explication du contexte et des enjeux, accessible à un non-spécialiste (définis les termes techniques la première fois que tu les utilises)
- Une conclusion qui reste factuelle

Règles de précision, importantes :
- N'avance aucun chiffre, date, montant, sigle ou attribution précise que ta recherche web ne confirme pas explicitement. En cas de doute, reste vague plutôt que d'inventer.
- Ne généralise pas au-delà de ce que confirment tes sources (une annonce limitée à des clients institutionnels n'est pas une annonce grand public).
- N'ajoute aucune conclusion, opinion ou extrapolation ("un pas vers...", "cela illustre la maturité de...") qui ne soit pas explicitement étayée.

Le contenu doit être au format Markdown simple (titres ##, paragraphes, gras/italique si utile)."""

    try:
        response = perplexity_client.responses.create(
            preset=PERPLEXITY_PRESET,
            input=prompt,
            response_format=WRITE_JSON_SCHEMA,
        )
        if getattr(response, "status", "completed") != "completed":
            raise RuntimeError(f"statut de réponse inattendu : {response.status}")
        parsed = json.loads(response.output_text)
        if not parsed.get("titre") or not parsed.get("contenu_markdown"):
            return None
        return parsed
    except Exception as e:
        print(f"[WARN] Erreur rédaction article '{story['titre_sujet']}' : {e}")
        return None


def revise_article(article: dict, verdict: dict) -> dict | None:
    """
    Renvoie l'article à Mistral avec les corrections trouvées par Perplexity
    (qui, elle, a accès au web) : Mistral ne "réessaie" pas en aveugle, il
    corrige avec des faits déjà vérifiés en main.
    """
    problemes_text = "\n".join(f"- {p}" for p in verdict.get("problemes", []))

    prompt = f"""Voici un article que tu as rédigé, avec des corrections factuelles identifiées par une vérification web indépendante :

TITRE ACTUEL : {article['titre']}

CONTENU ACTUEL :
{article['contenu_markdown']}

Corrections à apporter (informations vérifiées par recherche web) :
{problemes_text}

Réécris l'article en corrigeant précisément ces points, en utilisant les informations correctes données ci-dessus. Ne réintroduis aucune autre erreur. Si une correction ne te donne pas assez d'information pour être précis sur un détail, reste vague ou supprime ce détail plutôt que d'inventer.

Réponds UNIQUEMENT en JSON valide :
{{
  "titre": "...",
  "contenu_markdown": "..."
}}"""

    try:
        response = httpx.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MISTRAL_WRITER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 2500,
            },
            timeout=90,
        )
        response.raise_for_status()
        content = _clean_json(response.json()["choices"][0]["message"]["content"])
        parsed = json.loads(content)
        if not parsed.get("titre") or not parsed.get("contenu_markdown"):
            return None
        return parsed
    except Exception as e:
        print(f"[WARN] Erreur correction article '{article['titre']}' : {e}")
        return None


# ─────────────────────────────────────────────
# ÉTAPE 3 : Vérification par l'Agent API Perplexity
# ─────────────────────────────────────────────

VERIFY_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict_article",
        "schema": {
            "type": "object",
            "properties": {
                "approuve": {"type": "boolean"},
                "problemes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["approuve", "problemes"],
        },
    },
}


def verify_article(article: dict, story: dict) -> dict:
    """
    Vérifie l'article par recherche web live (pas seulement contre le texte
    source fourni) : capable de détecter le cas où la source RSS elle-même
    s'est trompée, contrairement à un simple check de cohérence texte-contre-texte.
    """
    sources_urls = "\n".join(f"- {a['link']}" for a in story["articles"])

    prompt = f"""Voici un article de blog en français à vérifier avant publication :

TITRE : {article['titre']}

CONTENU :
{article['contenu_markdown']}

Sources originales citées par l'auteur :
{sources_urls}

Utilise ta recherche web pour vérifier les faits centraux de l'article (dates, montants, noms, ce qui s'est réellement passé) — ne te fie pas uniquement aux sources listées ci-dessus, elles peuvent elles-mêmes être erronées.

Il s'agit d'un article grand public, pas d'un communiqué juridique : une certaine simplification de vocabulaire et de nuance est normale et attendue. Ne signale QUE :
- une affirmation factuellement fausse (un fait, chiffre, date ou nom incorrect)
- une confusion qui change le sens réel de l'événement (ex. présenter une annonce limitée à quelques clients comme s'adressant au grand public)
- une exagération non étayée par les sources (ex. "le premier/la première" alors que ce n'est pas le cas)

Ne signale PAS :
- un vocabulaire simplifié mais qui ne trahit pas le sens (ex. "autorisation" à la place de "enregistrement réglementaire", si le sens général reste correct)
- l'absence de détails secondaires ou d'exhaustivité
- le style journalistique ou la mise en contexte, tant qu'ils ne contredisent pas les faits

Réponds avec "approuve" (true si l'article est globalement fidèle aux faits même s'il n'est pas parfaitement exhaustif ; false seulement en cas de problème listé ci-dessus) et "problemes" (liste des problèmes trouvés, vide si aucun)."""

    try:
        # Suit le pattern documenté par le guide de migration Sonar -> Agent API
        # (docs.perplexity.ai/docs/agent-api/migrate-from-sonar) : preset +
        # input, la recherche web étant activée automatiquement par le preset.
        response = perplexity_client.responses.create(
            preset=PERPLEXITY_PRESET,
            input=prompt,
            response_format=VERIFY_JSON_SCHEMA,
        )
        if getattr(response, "status", "completed") != "completed":
            raise RuntimeError(f"statut de réponse inattendu : {response.status}")
        return json.loads(response.output_text)
    except Exception as e:
        print(f"[WARN] Erreur vérification Perplexity '{article['titre']}' : {e}")
        return {"approuve": False, "problemes": [f"Erreur technique de vérification : {e}"]}


# ─────────────────────────────────────────────
# ÉTAPE 4a : Publication
# ─────────────────────────────────────────────

def publish_article(article: dict, story: dict) -> None:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    slug = f"{date_str}-{_slugify(article['titre'])}"

    articles_dir = DOCS_DIR / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = DOCS_DIR / "articles.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []

    template = jinja_env.get_template("article.html.j2")
    html = template.render(
        titre=article["titre"],
        contenu_html=_render_markdown(article["contenu_markdown"]),
        date=date_str,
        sources=story["articles"],
    )
    (articles_dir / f"{slug}.html").write_text(html, encoding="utf-8")

    manifest.insert(0, {
        "slug": slug,
        "titre": article["titre"],
        "date": date_str,
        "extrait": _plain_excerpt(article["contenu_markdown"]),
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    index_template = jinja_env.get_template("index.html.j2")
    index_html = index_template.render(articles=manifest)
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    print(f"[OK] Article publié : {slug}")


# ─────────────────────────────────────────────
# ÉTAPE 4b : Rejet
# ─────────────────────────────────────────────

def reject_article(article: dict, story: dict, verdict: dict) -> None:
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = f"{date_str}-{_slugify(article['titre'])}"

    lines = [
        f"# [REJETÉ] {article['titre']}",
        "",
        f"**Sujet :** {story['titre_sujet']}",
        f"**Date :** {date_str}",
        "",
        "## Problèmes relevés par Perplexity",
        "",
    ]
    problemes = verdict.get("problemes", [])
    for pb in problemes:
        lines.append(f"- {pb}")
    lines += ["", "## Contenu proposé", "", article["contenu_markdown"]]

    (REJECTED_DIR / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")

    send_ntfy("Blog crypto - article bloque", f"Article rejeté : {article['titre']}\n{', '.join(problemes) or 'Raison inconnue'}")

    print(f"[WARN] Article rejeté : {slug}")


# ─────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────

def run(filtered: list[dict]) -> None:
    stories = group_by_story(filtered)
    if stories is None:
        send_ntfy(
            "Blog crypto - panne pipeline",
            "Échec technique du regroupement par sujet (Mistral) — aucun article de blog cette semaine. Voir les logs GitHub Actions.",
        )
        return
    if not stories:
        print("[INFO] Aucun sujet majeur cette semaine, pas d'article de blog.")
        return

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch(exist_ok=True)

    write_failures = 0

    for story in stories:
        article = write_article(story)
        if article is None:
            write_failures += 1
            continue

        verdict = verify_article(article, story)

        # Une seule passe de correction : Perplexity a déjà fait la recherche
        # web et donne les faits corrects dans son verdict, donc Mistral
        # corrige avec de vraies infos en main plutôt que de réessayer en aveugle.
        if not verdict.get("approuve"):
            revised = revise_article(article, verdict)
            if revised is not None:
                article = revised
                verdict = verify_article(article, story)

        if verdict.get("approuve"):
            publish_article(article, story)
        else:
            reject_article(article, story, verdict)

    # Si TOUTES les rédactions échouent, c'est le signal typique d'une panne
    # Perplexity (crédits épuisés, clé invalide, API en panne) plutôt que
    # des échecs isolés — alerte dédiée, distincte des rejets normaux.
    if write_failures > 0 and write_failures == len(stories):
        send_ntfy(
            "Blog crypto - panne Perplexity ?",
            f"Échec de rédaction sur les {write_failures} sujet(s) traité(s) cette semaine — vérifier la clé API et le crédit Perplexity. Voir les logs GitHub Actions.",
        )
    elif write_failures > 0:
        send_ntfy(
            "Blog crypto - échecs partiels",
            f"{write_failures} sujet(s) sur {len(stories)} n'ont pas pu être rédigés (erreur technique). Voir les logs GitHub Actions.",
        )
