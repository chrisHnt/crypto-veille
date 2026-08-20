"""
Extension blog public du pipeline de veille crypto.

Part des articles déjà filtrés par veille.py (mêmes appels Mistral, pas de
duplication), les regroupe par sujet réel, rédige un article explicatif par
sujet via Mistral, le fait vérifier par recherche web live via l'Agent API
Perplexity, puis publie (docs/) ou rejette (rejected/ + notification ntfy).
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

def group_by_story(filtered: list[dict]) -> list[dict]:
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

Pour chaque sujet distinct, indique son importance :
- "majeure" : mérite un article de blog dédié pour un public généraliste (hack important, régulation majeure, mise à jour technique significative...)
- "mineure" : trop niche ou peu impactant pour un article grand public autonome

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, sous cette forme exacte :
{{
  "sujets": [
    {{
      "titre_sujet": "Titre court du sujet",
      "importance": "majeure",
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
        return []

    stories = []
    for sujet in parsed.get("sujets", []):
        if sujet.get("importance") != SEUIL_IMPORTANCE:
            continue
        indices = [i - 1 for i in sujet.get("articles", []) if 0 <= i - 1 < len(filtered)]
        if not indices:
            continue
        stories.append({
            "titre_sujet": sujet.get("titre_sujet", "Sans titre"),
            "articles": [filtered[i] for i in indices],
        })

    print(f"[INFO] {len(stories)} sujets majeurs identifiés (sur {len(parsed.get('sujets', []))} regroupés)")
    return stories


# ─────────────────────────────────────────────
# ÉTAPE 2 : Rédaction de l'article
# ─────────────────────────────────────────────

def write_article(story: dict) -> dict | None:
    sources_text = ""
    for art in story["articles"]:
        sources_text += f"""
---
Titre : {art['title']}
Source : {art['source']}
Résumé : {art['resume_fr']}
URL : {art['link']}
"""

    prompt = f"""Tu es journaliste pour un blog public d'actualité crypto, destiné à un lectorat généraliste curieux mais non expert.

Sujet à traiter : {story['titre_sujet']}

Voici les articles sources sur ce sujet :
{sources_text}

Rédige un article explicatif en français de 500 à 700 mots :
- Un titre accrocheur mais factuel (pas putaclic, pas de superlatif du type "premier"/"record" sauf si une source l'affirme explicitement)
- Une intro qui résume ce qui s'est passé
- Une explication du contexte et des enjeux, accessible à un non-spécialiste (définis les termes techniques la première fois que tu les utilises)
- Une conclusion qui reste factuelle

Règles de précision, importantes :
- Utilise le vocabulaire exact des sources (ex. si une source parle d'un "enregistrement", n'écris pas "licence" ; si elle parle d'une "filiale locale", n'écris pas "l'entreprise" au global).
- Ne généralise pas au-delà de ce que dit la source (une annonce limitée à des clients institutionnels n'est pas une annonce grand public).
- N'ajoute aucune conclusion, opinion ou extrapolation ("un pas vers...", "cela illustre la maturité de...") qui ne soit pas explicitement dans les sources.
- Ne t'appuie QUE sur les informations fournies ci-dessus, n'invente aucun fait, chiffre ou citation.

Le contenu doit être au format Markdown simple (titres ##, paragraphes, gras/italique si utile).

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après :
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
                "model": MISTRAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
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
        print(f"[WARN] Erreur rédaction article '{story['titre_sujet']}' : {e}")
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

    try:
        httpx.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"Article rejeté : {article['titre']}\n{', '.join(problemes) or 'Raison inconnue'}".encode("utf-8"),
            headers={"Title": "Blog crypto - article bloque"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Erreur envoi notification ntfy : {e}")

    print(f"[WARN] Article rejeté : {slug}")


# ─────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────

def run(filtered: list[dict]) -> None:
    stories = group_by_story(filtered)
    if not stories:
        print("[INFO] Aucun sujet majeur cette semaine, pas d'article de blog.")
        return

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch(exist_ok=True)

    for story in stories:
        article = write_article(story)
        if article is None:
            continue

        verdict = verify_article(article, story)
        if verdict.get("approuve"):
            publish_article(article, story)
        else:
            reject_article(article, story, verdict)
