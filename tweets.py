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

# The house format (Shark Deals):
#
#   ¡Bajó 60%! 🔥 AHORA $799.999 🤑🤑🤑
#   https://meli.la/2UE9UV9
#   Tv Philco 58 Pulgadas Android Tv 4k 220V · antes $1.999.999 🦈🦈🦈
#
# NO blank lines, and the link sits on line 2 (or line 1, with link_position:
# "top") — never at the bottom. This is production-critical: a tall,
# blank-line-heavy tweet gets folded under X's "Show more", hiding the
# affiliate link from the timeline entirely (no clicks, no commission). The
# hook + link must land inside the first couple of lines every time.

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


def _place_link(hook: str, link: str, rest: str, tail: str, position: str) -> str:
    """Assemble hook + link + rest with NO blank lines between them — the link
    must land on line 1 or 2, never scrolled past X's "Show more" fold.

    position "bottom" (default): hook, then link, then rest.
    position "top": link first, then hook, then rest.
    """
    lines = [link, hook] if position == "top" else [hook, link]
    if rest:
        lines.append(rest)
    return "\n".join(lines) + tail


def _assemble(body: str, link: str, tail: str, position: str = "bottom") -> str:
    """Split LLM copy into hook (line 1) + rest, fit it in 280, place the link
    on line 1 or 2 per `position` — no blank lines, so the link stays visible
    above X's "Show more" fold."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    hook = lines[0] if lines else ""
    rest = " · ".join(lines[1:]).strip()

    room = TWEET_LIMIT - LINK_WEIGHT - len(tail) - len(hook) - 1
    if rest and len(rest) > room:
        rest = _shorten_title(rest, max(0, room))
    return _place_link(hook, link, rest, tail, position)


def tweet_length(text: str, link: str) -> int:
    """Length as X counts it: the URL always weighs 23, whatever its real size."""
    return len(text) - len(link) + LINK_WEIGHT if link in text else len(text)


def _link_position(settings: Any) -> str:
    pos = (settings.get("link_position") or "bottom").strip().lower()
    return "top" if pos == "top" else "bottom"


def render_template(product: Product, link: str, settings: Any) -> str:
    """The house format, link on line 1 or 2 (no blank lines — see the module
    docstring on why: X folds tall tweets and hides the link below "Show more"):

        ¡Bajó 60%! 🔥 AHORA $799.999 🤑🤑🤑
        <link>
        <title> · antes $1.999.999 🦈🦈🦈
    """
    tail = _tail(settings)
    position = _link_position(settings)
    now = _fmt_price(product.price, product.currency)
    disc = product.discount_pct or 0

    if disc and product.original_price:
        hook = f"¡Bajó {disc}%! 🔥 AHORA {now} 🤑🤑🤑"
        suffix = f" · antes {_fmt_price(product.original_price, product.currency)} 🦈🦈🦈"
    else:
        # No reliable discount/before-price: still lead with the price.
        hook = f"🦈🔥 AHORA {now} 🤑🤑🤑"
        suffix = " 🦈🦈🦈"

    # Budget the title against everything else so the tweet fits in 280.
    fixed = len(hook) + len(suffix) + 2 + LINK_WEIGHT + len(tail)
    room = max(20, TWEET_LIMIT - fixed)
    rest = f"{_shorten_title(product.title, room)}{suffix}"
    return _place_link(hook, link, rest, tail, position)


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
        f"- PRIMERA línea (el gancho): '¡Bajó X%!' o similar + 'AHORA' + el "
        f"precio actual + emojis (ej: '¡Bajó 40%! 🔥 AHORA $99.999 🤑🤑🤑').\n"
        f"- SEGUNDA línea: el nombre del producto.\n"
        f"- Máximo 2 líneas, SIN líneas en blanco entre ellas — el link se "
        f"agrega automáticamente después y tiene que quedar arriba del corte "
        f"de 'Show more' de X, así que el tweet tiene que ser corto.\n"
        f"- NO incluyas el link vos, se agrega después.\n"
        f"- Máximo {budget} caracteres.\n"
        f"- Usá 2 o 3 emojis como mucho.\n"
        f"- No inventes características que no estén arriba. No agregues hashtags.\n"
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
    return _assemble(text, link, tail, _link_position(settings))


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
