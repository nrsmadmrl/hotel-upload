"""Trivago browser steps and parsing.

Selectors are best-effort and UNVERIFIED until they have run against the live
site. One safety rule does not depend on them: a price is only trusted when the
check-in/check-out dates are confirmed in the page URL (dr-YYYYMMDD-YYYYMMDD).
"""

import hashlib
import re
import statistics
from datetime import date
from urllib.parse import unquote

MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
BLOCK_RE = re.compile(
    r"verify you are human|access denied|unusual traffic|captcha|"
    r"erişiminiz engellendi|insan olduğunuzu|too many requests",
    re.I,
)
DATE_TOKEN = re.compile(r"dr-\d{8}-\d{8}")
GUESTS_TOKEN = re.compile(r"rc-\d+-\d+")


def with_guests(url, rooms, adults):
    """Swap the guest token (rc-ROOMS-ADULTS) in a results URL, or add it when missing."""
    want = f"rc-{rooms}-{adults}"
    if GUESTS_TOKEN.search(url):
        return GUESTS_TOKEN.sub(want, url)
    if ";so-" in url:  # same position the site uses: ...;ds-..;rc-..;so-..
        return url.replace(";so-", f";{want};so-", 1)
    return url + f";{want}"


def guests_in_url(page, rooms, adults):
    """True when the page URL carries the wanted guests (no token = site default, 1 room 2 adults)."""
    m = GUESTS_TOKEN.search(unquote(page.url))
    return (m.group(0) if m else "rc-1-2") == f"rc-{rooms}-{adults}"
MONTH_HEADING = re.compile(r"^(" + "|".join(MONTHS_TR) + r")\s+\d{4}$")

SEARCH_INPUTS = [
    '[data-testid="search-form-destination"]',
    'input[type="search"]',
    'input[name="query"]',
    'input[placeholder*="Nereye"]',
    'input[aria-label*="Nereye"]',
    "input[autocomplete='off']",
]
SUGGESTIONS = ['[data-testid="ssg-element"]', '[role="option"]', "[role='listbox'] li"]
SUBMIT = ['[data-testid="search-button-with-loader"]', 'button[aria-label*="Ara"]',
          'button:has-text("Ara")']
COOKIE_BUTTONS = ['button:has-text("Kabul et")', 'button:has-text("Tümünü kabul et")',
                  'button:has-text("Accept")', '[data-testid="uc-accept-all-button"]']
DATE_FIELDS = ['[data-testid="search-form-calendar-checkin"]', "text=Tarih seçin",
               "text=Giriş/çıkış"]
NEXT_MONTH = ['[data-testid="calendar-button-next"]', 'button[aria-label*="Sonraki"]',
              'button[aria-label*="İleri"]', 'button[aria-label*="Next"]']

CARD_SELECTOR = ('[data-testid="accommodation-list-element"], '
                 'li[data-testid*="accommodation"], article[data-testid*="accommodation"]')
CARD_JS = """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
  const q = s => { const n = el.querySelector(s); return n ? n.innerText.trim() : null; };
  const a = el.querySelector('a[href]');
  return {
    name: q('[data-testid="item-name"]') || q('h2') || q('h3'),
    price_text: q('[data-testid="recommended-price"]') || q('[data-testid*="price"]'),
    href: a ? a.href : null,
    text: el.innerText.slice(0, 1500)
  };
})"""


# ---------------- parsing (network-free, unit-tested) ----------------

def identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def parse_try_minor(text):
    """'₺3.456' / '12.345,50 TL' -> kuruş int, else None. Never 0."""
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{2}))?", text)
    if not m:
        return None
    minor = int(m.group(1).replace(".", "")) * 100 + int(m.group(2) or 0)
    return minor if minor > 0 else None


def parse_rating(text):
    """Guest score is a line of its own, e.g. '9,3' (the '5 Yıldız' line is the star class)."""
    for line in (text or "").splitlines():
        m = re.fullmatch(r"(\d{1,2})[.,](\d)", line.strip())
        if m:
            value = float(f"{m.group(1)}.{m.group(2)}")
            if 0 < value <= 10:
                return value
    return None


def parse_reviews(text):
    """'(10B misafir puanı)' -> 10000, '(8.9B ...)' -> 8900, '(662 misafir puanı)' -> 662 (B = bin)."""
    m = re.search(r"\((\d+(?:[.,]\d+)?)\s*B\b", text or "")
    if m:
        return int(round(float(m.group(1).replace(",", ".")) * 1000))
    m = re.search(r"\((\d{1,3}(?:\.\d{3})+|\d+)(?:\s+misafir puanı)?\)", text or "")
    return int(m.group(1).replace(".", "")) if m else None


def parse_stars(text):
    m = re.search(r"(\d)\s*Yıldız", text or "")
    return int(m.group(1)) if m else None


def parse_distance_km(text):
    """'Kemer, Antalya, Türkiye 4,1 km uzaklıkta' -> 4.1 (distance to the searched centre, NOT the airport)."""
    m = re.search(r"(\d+(?:,\d+)?)\s*km\s+uzaklıkta", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


MEAL_PATTERNS = [  # most specific first; null when the card says nothing. Note 'dâhil' (circumflex) on the site.
    ("ultra_all_inclusive", r"ultra\s+her\s*şey\s+d[aâ]hil"),
    ("all_inclusive", r"her\s*şey\s+d[aâ]hil"),
    ("full_board", r"tam\s+pansiyon"),
    ("half_board", r"yarım\s+pansiyon"),
    ("breakfast_included", r"kahvaltı\s+d[aâ]hil|ücretsiz\s+kahvaltı"),
    ("room_only", r"sadece\s+oda|kahvaltı\s+(?:d[aâ]hil\s+değil|yok)"),
]


def parse_meal_plan(text):
    for label, pattern in MEAL_PATTERNS:
        if re.search(pattern, text or "", re.I):
            return label
    return None


def parse_card_prices(text, nights):
    """(per_night_minor, stay_total_minor) read from the card and cross-checked.

    Every card shows a nightly price and, below it, '₺58.711 toplam' (the stay total).
    A pair is accepted only when nightly x nights ~ total, which also skips 'Önceki fiyat'
    (old struck-through price) and login-only member prices. First consistent pair wins.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = re.fullmatch(r"₺\s?([\d.]+)\s+toplam", line)
        if not m or not nights:
            continue
        total = int(m.group(1).replace(".", ""))
        for j in range(i - 1, max(i - 7, -1), -1):
            p = re.fullmatch(r"₺\s?([\d.]+)", lines[j])
            if p:
                per_night = int(p.group(1).replace(".", ""))
                if abs(per_night * nights - total) <= nights:  # <= 1 TL rounding per night
                    return per_night * 100, total * 100
    return None, None


def flat(text, limit=1200):
    return " | ".join(line.strip() for line in (text or "").splitlines() if line.strip())[:limit]


def card_to_record(card, region_id, nights=None):
    # Name text contains the star line ("Hotel X\n5 Yıldız"); keep only the name
    # so hotel_id stays stable. Trivago hotel ids are not known, so the id is
    # derived from region + name.
    name = (card.get("name") or "").split("\n")[0].strip()
    if not name:
        return None
    body = card.get("text") or ""
    price_text = card.get("price_text")
    per_night, total = parse_card_prices(body, nights)
    listed_minor = parse_try_minor(price_text)
    scope = "per_night" if per_night else None
    if not per_night and nights == 1 and listed_minor:
        # One night: nightly price == stay total, so the listed price is unambiguous.
        per_night = total = listed_minor
        scope = "single_night"
    elif not per_night and listed_minor:
        scope = "unknown"  # a price is shown but nightly/total could not be verified
    return dict(
        hotel_id=identifier(f"{region_id}|{name.lower()}")[:16],
        hotel_name=name,
        # verified nightly price; falls back to the card's price element (scope then unknown)
        price_minor=per_night or listed_minor,
        price_total_minor=total,
        price_label=(price_text or "").split("\n")[0].strip() or None,
        stars=parse_stars(body),
        rating=parse_rating(body),
        review_count=parse_reviews(body),
        distance_km=parse_distance_km(body),
        meal_plan=parse_meal_plan(body),
        price_scope=scope,  # per_night | single_night | unknown | None (no price)
        price_text_raw=flat(price_text),
        card_text_raw=flat(body),
        href=card.get("href"),
    )


def propose_registry(records, sel):
    """Price terciles of priced, well-reviewed hotels; take per_segment from each."""
    pool = [
        r for r in records[: sel["candidate_pool"]]
        if r["price_minor"] and r["rating"] and r["rating"] >= sel["min_rating"]
        and (r["review_count"] is None or r["review_count"] >= sel["min_reviews"])
    ]
    pool.sort(key=lambda r: r["price_minor"])
    n = len(pool)
    if n < 3:
        return []
    out = []
    for i, label in enumerate(sel["segments"]):
        band = pool[i * n // 3: (i + 1) * n // 3]
        if not band:
            continue
        median = statistics.median(r["price_minor"] for r in band)
        band = sorted(band, key=lambda r: abs(r["price_minor"] - median))
        out += [{**r, "segment": label} for r in band[: sel["per_segment"]]]
    return out


# ---------------- browser steps (selectors unverified) ----------------

def first_visible(page, selectors):
    for s in selectors:
        loc = page.locator(s).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            pass
    return None


def accept_cookies(page):
    page.wait_for_timeout(2500)
    btn = first_visible(page, COOKIE_BUTTONS)
    if btn:
        try:
            btn.click(timeout=4000)
            page.wait_for_timeout(1000)
        except Exception:
            pass


def header_text(page):
    return page.inner_text("body")[:1500]


def has_dates(page):
    # The search bar shows 'Tarih seçin' while no dates are active.
    return "Tarih seçin" not in header_text(page)


def dates_in_url(page, stay):
    want = "dr-{}-{}".format(stay["check_in_date"].replace("-", ""),
                             stay["check_out_date"].replace("-", ""))
    return want in unquote(page.url)


def resolve_region(page, base_url, search_text):
    """Use the site's own search once; return the results URL it lands on."""
    page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
    accept_cookies(page)
    box = first_visible(page, SEARCH_INPUTS)
    if not box:
        raise RuntimeError("search_box_not_found")
    box.click()
    box.fill("")
    box.type(search_text, delay=90)
    page.wait_for_timeout(2500)
    suggestion = first_visible(page, SUGGESTIONS)
    if suggestion:
        suggestion.click()
    else:
        box.press("Enter")
    page.wait_for_timeout(1500)
    submit = first_visible(page, SUBMIT)
    if submit:
        try:
            submit.click(timeout=4000)
        except Exception:
            pass
    for _ in range(25):
        if "search=" in page.url or "/srl" in page.url:
            break
        page.wait_for_timeout(1000)
    else:
        raise RuntimeError("results_url_not_reached: " + page.url)
    page.wait_for_timeout(4000)
    return page.url


def calendar_open(page):
    return page.get_by_text(MONTH_HEADING).count() > 0


def open_calendar(page):
    if calendar_open(page):
        return True
    for sel in DATE_FIELDS:
        try:
            page.locator(sel).first.click(timeout=3000)
            page.wait_for_timeout(1000)
            if calendar_open(page):
                return True
        except Exception:
            pass
    return False


def next_month(page):
    for sel in NEXT_MONTH:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                return True
        except Exception:
            pass
    try:  # fallback: the round arrow that follows the right-hand month heading
        headings = page.get_by_text(MONTH_HEADING)
        if headings.count():
            headings.last.locator("xpath=following::button[1]").click(timeout=3000)
            return True
    except Exception:
        pass
    return False


def day_locators(page, d):
    iso = d.isoformat()
    label = re.compile(r"(^|[\s,])%d %s %d" % (d.day, MONTHS_TR[d.month - 1], d.year))
    return [
        page.locator(f'[data-testid$="{iso}"]'),
        page.locator(f'[data-testid*="{iso}"]'),
        page.locator(f'[data-date="{iso}"]'),
        page.locator(f'[datetime="{iso}"]'),
        page.locator(f'[aria-label*="{iso}"]'),
        page.get_by_label(label),
    ]


def panel_day(page, d):
    """Fallback: find the month heading, climb to its own panel, take the day number."""
    heading = page.get_by_text(f"{MONTHS_TR[d.month - 1]} {d.year}", exact=True)
    if heading.count() != 1:
        return None
    node = heading.first
    for _ in range(4):
        node = node.locator("xpath=..")
        if node.get_by_text(MONTH_HEADING).count() > 1:
            return None  # climbed past the single month panel
        cell = node.get_by_text(str(d.day), exact=True)
        if cell.count():
            return cell.first
    return None


def click_day(page, d, max_pages=10):
    for _ in range(max_pages):
        for loc in day_locators(page, d):
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4000)
                    return True
            except Exception:
                continue
        cell = panel_day(page, d)
        if cell is not None:
            try:
                cell.click(timeout=4000)
                return True
            except Exception:
                pass
        if not next_month(page):
            return False
        page.wait_for_timeout(500)
    return False


def set_dates_ui(page, stay):
    a = date.fromisoformat(stay["check_in_date"])
    b = date.fromisoformat(stay["check_out_date"])
    if not open_calendar(page):
        return False
    if not click_day(page, a):
        return False
    page.wait_for_timeout(600)
    if not click_day(page, b):
        return False
    page.wait_for_timeout(1500)
    submit = first_visible(page, SUBMIT)
    if submit:
        try:
            submit.click(timeout=3000)
        except Exception:
            pass
    return True


def ensure_dates(page, template, stay):
    """Make the page search the stay's dates. Returns 'url', 'calendar' or None.

    Only a result confirmed in the URL (dr-YYYYMMDD-YYYYMMDD) counts as verified.
    """
    a = stay["check_in_date"].replace("-", "")
    b = stay["check_out_date"].replace("-", "")
    want = f"dr-{a}-{b}"
    url = DATE_TOKEN.sub(want, template) if DATE_TOKEN.search(template) else template
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    if has_dates(page) and want in unquote(page.url):
        return "url"
    if set_dates_ui(page, stay):
        for _ in range(12):
            if want in unquote(page.url):
                return "calendar"
            page.wait_for_timeout(1000)
    return None


def read_cards(page, max_rounds=12):
    page.keyboard.press("Escape")  # close an auto-opened calendar popup
    prior, stable = -1, 0
    for _ in range(max_rounds):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(1500)
        n = page.locator(CARD_SELECTOR).count()
        stable = stable + 1 if n == prior else 0
        if stable >= 3:
            break
        prior = n
    text = page.inner_text("body")
    cards = page.evaluate(CARD_JS, CARD_SELECTOR)
    return text, cards


def next_page(page):
    btn = page.get_by_text("Sonraki sayfa", exact=True)
    try:
        if btn.count() and btn.first.is_visible():
            btn.first.click(timeout=4000)
            return True
    except Exception:
        pass
    return False


def read_listing(page, region_id, nights, wanted_ids=None, max_pages=1, max_rounds=12,
                 read_all=False, on_no_next=None):
    """Cards of page 1, then following pages.

    Paging continues until every wanted hotel has been seen (collect), or up to
    max_pages when read_all is set (discover). The site lists ~30 of 200+ hotels
    per page, so a tracked hotel can slip to a later page.
    on_no_next(page, pages_read) is called when the "next page" control cannot be
    found, so the caller can keep evidence of why paging stopped.
    Returns (page-1 text, records, pages_read).
    """
    text, cards = read_cards(page, max_rounds)
    records = {}

    def add(batch):
        for c in batch:
            r = card_to_record(c, region_id, nights)
            if r and r["hotel_id"] not in records:
                records[r["hotel_id"]] = r

    add(cards)
    pages = 1
    while pages < max_pages and (read_all or (wanted_ids and not set(wanted_ids) <= set(records))):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        if not next_page(page):
            if on_no_next:
                on_no_next(page, pages)
            break
        page.wait_for_timeout(6000)
        _, more = read_cards(page, max_rounds)
        add(more)
        pages += 1
    return text, list(records.values()), pages


def save_debug(page, stem):
    """Screenshot of the search bar + page text/html, for fixing selectors."""
    try:
        page.keyboard.press("Escape")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(800)
        page.screenshot(path=str(stem.with_suffix(".png")))
        stem.with_suffix(".txt").write_text(page.inner_text("body"), encoding="utf-8")
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def save_bottom(page, stem):
    """Screenshot of the CURRENT viewport (page bottom, where paging lives) + the last
    2500 characters of page text. Used when "next page" cannot be found."""
    try:
        page.screenshot(path=str(stem.with_suffix(".png")))
        stem.with_suffix(".txt").write_text(page.inner_text("body")[-2500:], encoding="utf-8")
    except Exception:
        pass