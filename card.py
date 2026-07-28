"""
Compose a shareable offer card image.

Deal accounts don't post the bare product photo — they post a card showing the
product *with* its price, the strike-through original and the discount. That
can't be a screenshot here: Mercado Libre's product pages sit behind a login
wall, so a headless grab would capture the login prompt. It also shouldn't be —
a generated card is faster, deterministic, carries your own branding, and needs
no network beyond the product image we already have.

Everything on the card comes from data already scraped into the store.

Renders 1200x675 (16:9), which X displays without cropping in the timeline.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Optional

from lib.log import log_warn

W, H = 1200, 675
PAD = 56

# Dark card: product photos are nearly always on white, so a dark surround
# makes the cutout read as deliberate rather than as a pasted rectangle.
BG = (17, 19, 24)
PANEL = (255, 255, 255)
TEXT = (245, 246, 248)
MUTED = (150, 156, 168)
ACCENT = (0, 200, 110)      # discount green, close to ML's own
STRIKE = (150, 156, 168)

# Checked in order; the first that exists wins. macOS first, then the paths
# GitHub's ubuntu runners provide.
_FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ],
}


def _font(kind: str, size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES[kind]:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
    # Bitmap fallback: ugly and unscaled, but it still renders something.
    return ImageFont.load_default()


def fonts_available() -> bool:
    """True when a real TTF was found — the bitmap fallback looks bad enough
    that callers may prefer the plain product photo instead."""
    return any(
        Path(p).is_file() for paths in _FONT_CANDIDATES.values() for p in paths
    )


def _fmt_price(value: Optional[float], currency: str = "ARS") -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in ("ARS", "MXN", "CLP", "COP", "UYU") else "R$"
    return f"{symbol}{int(round(value)):,}".replace(",", ".")


def _wrap(draw: Any, text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    if lines and len(lines) == max_lines:
        # Ellipsise the last line if there was more text than fits.
        consumed = len(" ".join(lines).split())
        if consumed < len(words):
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = last + "…"
    return lines


def _fetch_image(url: str, timeout: float = 25.0):
    """Product photo at the largest render available, as a PIL image."""
    import httpx
    from PIL import Image

    from lib.twitter_post import _image_candidates

    with httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for candidate in _image_candidates(url):
            try:
                r = c.get(candidate)
            except Exception:  # noqa: BLE001 - try the next variant
                continue
            if r.status_code == 200 and r.content:
                return Image.open(io.BytesIO(r.content)).convert("RGB")
    raise RuntimeError(f"could not fetch product image: {url}")


def _rounded_panel(size: tuple[int, int], radius: int, colour: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        [(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=colour + (255,)
    )
    return panel


def compose_screenshot(
    shot_path: Path | str, out_path: Path | str, *, product: Any = None
) -> Path:
    """Place a captured offer card on a 16:9 canvas.

    The raw card is portrait (~850x1600, aspect 0.53). X won't display anything
    that tall uncropped, and cropping would slice off the price — the one thing
    the image exists to show. A 3:4 canvas is the tallest X shows in full, and
    it leaves the card nearly filling the frame rather than stranded in the
    middle of a 16:9 letterbox.

    The margins are filled with a blurred, darkened copy of the card so the
    result reads as designed rather than as padding.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    # 3:4 — the tallest aspect X renders without cropping.
    cw, ch = 1080, 1440

    shot = Image.open(shot_path).convert("RGB")
    canvas = Image.new("RGB", (cw, ch), BG)

    # Backdrop: the card itself, blown up, blurred and dimmed.
    backdrop = shot.copy()
    ratio = max(cw / backdrop.width, ch / backdrop.height)
    backdrop = backdrop.resize(
        (int(backdrop.width * ratio) + 1, int(backdrop.height * ratio) + 1),
        Image.LANCZOS,
    ).filter(ImageFilter.GaussianBlur(28))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.35)
    canvas.paste(backdrop, ((cw - backdrop.width) // 2, (ch - backdrop.height) // 2))

    # The card, scaled to just under full height.
    target_h = ch - 36
    scale = target_h / shot.height
    card = shot.resize((max(1, int(shot.width * scale)), target_h), Image.LANCZOS)
    if card.width > cw - 36:  # very wide cards: fit to width instead
        scale = (cw - 36) / shot.width
        card = shot.resize((cw - 36, max(1, int(shot.height * scale))), Image.LANCZOS)
    canvas.paste(card, ((cw - card.width) // 2, (ch - card.height) // 2))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG", optimize=True)
    return out_path


def render(product: Any, out_path: Path | str, *, brand: str = "") -> Path:
    """Draw the offer card for `product` and write it as PNG."""
    from PIL import Image, ImageDraw

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    card = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(card)

    # ---- left: product photo on a white panel ---------------------------
    panel_size = H - PAD * 2
    panel = _rounded_panel((panel_size, panel_size), 28, PANEL)
    try:
        photo = _fetch_image(product.image)
        inner = panel_size - 36
        photo.thumbnail((inner, inner), Image.LANCZOS)
        panel.paste(
            photo,
            ((panel_size - photo.width) // 2, (panel_size - photo.height) // 2),
        )
    except Exception as e:  # noqa: BLE001 - an empty panel still beats no card
        log_warn(f"card: product photo unavailable ({type(e).__name__}: {e})")
    card.paste(panel, (PAD, PAD), panel)

    # ---- right: the numbers ---------------------------------------------
    x = PAD + panel_size + 48
    right = W - PAD
    width = right - x
    y = PAD + 4

    discount = product.discount_pct or 0
    if discount:
        badge_font = _font("bold", 44)
        label = f"{discount}% OFF"
        tw = draw.textlength(label, font=badge_font)
        draw.rounded_rectangle(
            [(x, y), (x + tw + 40, y + 68)], radius=16, fill=ACCENT
        )
        draw.text((x + 20, y + 10), label, font=badge_font, fill=(10, 20, 14))
        y += 96

    title_font = _font("bold", 38)
    for line in _wrap(draw, product.title, title_font, width, 3):
        draw.text((x, y), line, font=title_font, fill=TEXT)
        y += 48
    y += 20

    if product.original_price and product.original_price != product.price:
        was_font = _font("regular", 32)
        was = _fmt_price(product.original_price, product.currency)
        draw.text((x, y), was, font=was_font, fill=STRIKE)
        tw = draw.textlength(was, font=was_font)
        draw.line([(x, y + 20), (x + tw, y + 20)], fill=STRIKE, width=3)
        y += 46

    now_font = _font("bold", 76)
    draw.text((x, y), _fmt_price(product.price, product.currency),
              font=now_font, fill=TEXT)
    y += 92

    saved = product.savings
    if saved:
        save_font = _font("bold", 30)
        draw.text((x, y), f"Ahorrás {_fmt_price(saved, product.currency)}",
                  font=save_font, fill=ACCENT)

    # ---- footer ----------------------------------------------------------
    foot_font = _font("regular", 26)
    bits = [b for b in (brand, product.matched_label) if b]
    if bits:
        draw.text((x, H - PAD - 26), "  ·  ".join(bits), font=foot_font, fill=MUTED)

    card.save(out_path, "PNG", optimize=True)
    return out_path
