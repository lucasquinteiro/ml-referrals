"""
Mercado Libre affiliate link generation.

Mercado Libre's Programa de Afiliados attributes a sale through two query
params carried on the product URL:

    matt_word  -> your affiliate tag       (the "word" in the dashboard)
    matt_tool  -> the numeric tool id      (identifies the link source)

So an affiliate link is just the canonical product URL with those appended, e.g.

    https://www.mercadolibre.com.ar/producto/p/MLA123456
      ?matt_word=lucasq&matt_tool=68232872&forceInApp=true

VERIFY ONCE: generate a single link in your affiliate dashboard and check it
against `./run check-affiliate <url>`. If Mercado Libre hands you a different
shape (some accounts get a /social/<tag> wrapper), set `affiliate.link_template`
in config.json to that shape — {url}, {url_encoded}, {tag} and {tool} are
substituted. Attribution only works if the params match your dashboard exactly.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


class AffiliateError(RuntimeError):
    pass


def _clean(url: str) -> str:
    """Drop the click-tracking fragment/query MercadoLibre puts on card links."""
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))


def build_link(
    product_url: str,
    *,
    tag: str,
    tool_id: str,
    extra_params: Optional[dict[str, Any]] = None,
    template: str = "",
) -> str:
    """Return the affiliate-tagged URL for `product_url`.

    Raises AffiliateError when the tag/tool id are missing, so a run can never
    silently post untracked links — that would be lost commission.
    """
    if not tag or not tool_id:
        raise AffiliateError(
            "Missing affiliate tag/tool id. Set ML_AFFILIATE_TAG and "
            "ML_AFFILIATE_TOOL_ID in .env (or affiliate.tag / affiliate.tool_id "
            "in config.json). Both come from your Programa de Afiliados dashboard."
        )

    base = _clean(product_url)

    if template:
        return template.format(
            url=base, url_encoded=quote(base, safe=""), tag=tag, tool=tool_id
        )

    parts = urlparse(base)
    params = dict(parse_qsl(parts.query))
    params["matt_word"] = tag
    params["matt_tool"] = tool_id
    for k, v in (extra_params or {}).items():
        params.setdefault(k, str(v))

    return urlunparse(
        (parts.scheme, parts.netloc, parts.path, parts.params, urlencode(params), "")
    )


def build_link_from_settings(product_url: str, settings: Any) -> str:
    """Convenience wrapper that pulls credentials off a Settings object."""
    aff = settings.affiliate or {}
    return build_link(
        product_url,
        tag=settings.affiliate_tag,
        tool_id=settings.affiliate_tool_id,
        extra_params=aff.get("extra_params"),
        template=aff.get("link_template", ""),
    )
