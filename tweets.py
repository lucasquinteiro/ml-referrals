"""
Tweet copy for an offer.

Templates are the baseline and always work. When an LLM key is configured the
copy is generated instead (more variety, less spammy-looking timeline), with the
template as an automatic fallback on any failure.

Length matters: X counts every link as 23 characters regardless of its real
length, so the affiliate URL is budgeted at 23 and the body is trimmed to fit.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from lib.log import log_warn
from scraper import Product

TWEET_LIMIT = 280
LINK_WEIGHT = 23  # X's t.co length, counted for any URL

# Shark Deals branding: a 🦈 leads every template (varied so the timeline isn't
# monotonous), and an optional signature line closes it (config tweet_signature).
_TEMPLATES = [
    "🦈 {discount}% OFF en {label}\n\n{title}\n\n❌ Antes {was}\n💰 Ahorrás {saved}\n\n✅ AHORA {now}\n\n{link}",
    "🦈⚡ ¡BAJÓ {discount}%!\n\n{title}\n\n🏷️ Antes {was}\n🤑 Te ahorrás {saved}\n\n💸 AHORA {now}\n\n{link}",
    "🦈 Alerta de precio · {label}\n\n{title}\n\n📉 {discount}% OFF — antes {was}\n💵 {saved} menos\n\n✅ AHORA {now}\n\n{link}",
    "🦈🔥 {title}\n\n🏷️ Antes {was} · {discount}% OFF\n💰 Ahorro: {saved}\n\n✅ AHORA {now}\n\n{link}",
]

_SYSTEM = (
    "Sos un curador de ofertas argentino que publica en X/Twitter. Escribís "
    "tweets cortos, concretos y sin exagerar. Nada de clickbait, nada de "
    "'INCREÍBLE!!!', nada de inventar datos. Español rioplatense, tuteo. "
    "Respondé SOLO con el texto del tweet, sin comillas ni explicaciones."
)


def _fmt_price(value: Optional[float], currency: str = "ARS") -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in ("ARS", "MXN", "CLP", "COP", "UYU") else "R$"
    # es-AR style: 1.234.567
    whole = f"{int(round(value)):,}".replace(",", ".")
    return f"{symbol}{whole}"


def _shorten_title(title: str, budget: int) -> str:
    if len(title) <= budget:
        return title
    cut = title[: max(0, budget - 1)].rsplit(" ", 1)[0]
    return (cut or title[: max(0, budget - 1)]).rstrip(" ,-") + "…"


def _tail(settings: Any) -> str:
    """Signature + hashtags + affiliate disclosure, as one trailing block."""
    bits: list[str] = []
    signature = (settings.get("tweet_signature") or "").strip()
    if signature:
        bits.append(signature)
    tags = settings.tweet_hashtags or []
    if tags:
        bits.append(" ".join(tags))
    disclosure = (settings.tweet_disclosure or "").strip()
    if disclosure:
        bits.append(disclosure)
    return "\n\n" + " · ".join(bits) if bits else ""


def _assemble(body: str, link: str, tail: str) -> str:
    """Fit body + link + tail into 280, trimming the body if needed."""
    overhead = LINK_WEIGHT + len(tail) + 1  # +1 for the newline before the link
    room = TWEET_LIMIT - overhead
    body = body.strip()
    if len(body) > room:
        body = _shorten_title(body, room)
    return f"{body}\n{link}{tail}"


def tweet_length(text: str, link: str) -> int:
    """Length as X counts it: the URL always weighs 23, whatever its real size."""
    return len(text) - len(link) + LINK_WEIGHT if link in text else len(text)


def render_template(product: Product, link: str, settings: Any) -> str:
    tail = _tail(settings)
    # Template picked from the product id, not at random: the same product always
    # renders the same tweet, so runs are reproducible and reviewable — while
    # different products still vary the format across the timeline.
    idx = int(hashlib.md5(product.product_id.encode()).hexdigest(), 16) % len(_TEMPLATES)
    template = _TEMPLATES[idx]
    fields = {
        "discount": product.discount_pct or 0,
        "was": _fmt_price(product.original_price, product.currency),
        "now": _fmt_price(product.price, product.currency),
        "saved": _fmt_price(product.savings, product.currency),
        "label": product.matched_label or "Oferta",
    }

    # Budget the title against the scaffold, so the finished tweet fits.
    scaffold = template.format(title="", link="", **fields)
    room = TWEET_LIMIT - len(scaffold) - LINK_WEIGHT - len(tail)

    text = template.format(
        title=_shorten_title(product.title, max(20, room)), link=link, **fields
    )
    return text + tail


def generate_with_llm(product: Product, link: str, settings: Any) -> Optional[str]:
    """LLM copy, or None if unavailable/failed (caller falls back to templates)."""
    api_key = settings.llm_api_key
    if not api_key:
        return None

    from lib.llm import chat_completion_with_fallback

    tail = _tail(settings)
    budget = TWEET_LIMIT - LINK_WEIGHT - len(tail) - 2
    facts = (
        f"Producto: {product.title}\n"
        f"Categoría: {product.matched_label}\n"
        f"Precio anterior: {_fmt_price(product.original_price, product.currency)}\n"
        f"Precio actual: {_fmt_price(product.price, product.currency)}\n"
        f"Descuento: {product.discount_pct}%\n"
        f"Envío gratis: {'sí' if product.free_shipping else 'no'}\n"
        f"Etiqueta MercadoLibre: {product.badge or '—'}\n"
    )
    prompt = (
        f"Escribí un tweet para esta oferta de Mercado Libre.\n\n{facts}\n"
        f"Reglas:\n"
        f"- Empezá con el emoji de tiburón 🦈 (la marca es 'Shark Deals').\n"
        f"- Máximo {budget} caracteres (NO incluyas el link, se agrega después).\n"
        f"- Resaltá el porcentaje de descuento EN MAYÚSCULAS (ej: 'BAJÓ 30%', '30% OFF').\n"
        f"- Poné el precio actual en su propia línea, separado por una línea en blanco del resto de los montos.\n"
        f"- Usá 2 o 3 emojis como mucho (uno es el 🦈 del inicio).\n"
        f"- No inventes características que no estén arriba.\n"
        f"- No agregues hashtags.\n"
    )

    try:
        text = chat_completion_with_fallback(
            system=_SYSTEM,
            user=prompt,
            model=settings.llm.get("model"),
            api_key=api_key,
            base_url=settings.llm.get("base_url"),
            temperature=0.8,
            fallback_model=(settings.get("llm_fallback") or {}).get("model"),
            fallback_api_key=settings.llm_fallback_api_key,
            fallback_base_url=(settings.get("llm_fallback") or {}).get("base_url"),
            label="tweet-copy",
        )
    except Exception as e:  # noqa: BLE001 - templates are a fine fallback
        log_warn(f"LLM copy failed ({type(e).__name__}: {e}); using template")
        return None

    text = text.strip().strip('"').strip()
    if not text:
        return None
    return _assemble(text, link, tail)


def build_tweet(product: Product, link: str, settings: Any, *, deterministic: bool = False) -> str:
    """LLM copy when configured, template otherwise. Always fits in 280.

    `deterministic=True` forces the template path, so the same product always
    produces byte-identical copy — an LLM can't promise that even at
    temperature 0.
    """
    if settings.use_llm_for_copy and not deterministic:
        text = generate_with_llm(product, link, settings)
        if text:
            return text
    return render_template(product, link, settings)
