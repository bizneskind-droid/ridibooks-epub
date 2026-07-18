"""
Скачивает главы книги с ridibooks.com и собирает EPUB.

Требуется cloakbrowser (стелс-хромиум, проходит Cloudflare ridibooks):
    python3 -m venv venv && ./venv/bin/pip install cloakbrowser ebooklib
    ./venv/bin/python ridibooks.py

На этом VPS (Ubuntu, AppArmor) нужен --no-sandbox — уже прописан в launch().
"""

import json
import os
import re
import time
from cloakbrowser import launch
from ebooklib import epub
from lxml import html as lxml_html
from lxml import etree

# --- Селекторы / источники данных (проверены на живой странице ridibooks) ---
CONTENT_SELECTOR = "#viewer_contents"          # стабильный id читалки
OG_TITLE = 'meta[property="og:title"]'          # "<название книги> N화"
OG_URL = 'meta[property="og:url"]'              # содержит book_id — якорь главы
COVER_URL = "https://img.ridicdn.net/cover/{book_id}/xxlarge"  # 480x689 вместо 120x172

# --- Кэш глав: повторный запуск не качает заново, только недостающее ---
CACHE_DIR = "cache"            # рядом со скриптом, ключ = book_id

# --- Куки для платных глав / залогиненной сессии ---
# Ищем рядом со скриптом (или путь в переменной окружения RIDI_COOKIES).
# Поддерживаем два формата:
#   cookies.txt          — Netscape, из расширения "Get cookies.txt LOCALLY"
#   storage_state.json   — родной формат Playwright (куки + localStorage)
COOKIES_TXT = "cookies.txt"
STORAGE_STATE_JSON = "storage_state.json"


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def parse_netscape_cookies(path):
    """cookies.txt (Netscape) -> список куки для Playwright add_cookies()."""
    cookies = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            # '#HttpOnly_' — валидная строка, обычные '#' комментарии пропускаем
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, _flag, cpath, secure, expires, name, value = parts
            cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cpath,
                "secure": secure.upper() == "TRUE",
            }
            if expires and expires != "0":
                cookie["expires"] = int(expires)
            cookies.append(cookie)
    return cookies


def find_cookie_source():
    """Возвращает ('storage_state'|'cookies'|None, значение).
    storage_state -> путь к json; cookies -> список куки."""
    env = os.environ.get("RIDI_COOKIES")
    candidates = []
    if env:
        candidates.append(env)
    d = _script_dir()
    candidates += [os.path.join(d, STORAGE_STATE_JSON),
                   os.path.join(d, COOKIES_TXT)]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        if path.endswith(".json"):
            return "storage_state", path
        return "cookies", parse_netscape_cookies(path)
    return None, None


def cache_path(book_id):
    return os.path.join(_script_dir(), CACHE_DIR, f"{book_id}.json")


def read_cache(book_id):
    """Возвращает (og_title, body) из кэша или None."""
    p = cache_path(book_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d["og_title"], d["body"]
    except Exception:
        return None


def write_cache(book_id, og_title, body):
    os.makedirs(os.path.join(_script_dir(), CACHE_DIR), exist_ok=True)
    with open(cache_path(book_id), "w", encoding="utf-8") as f:
        json.dump({"og_title": og_title, "body": body}, f, ensure_ascii=False)

STYLE_CSS = """
body {
    line-height: 1.8;
    font-family: serif;
}
"""


def html_to_xhtml(fragment):
    """inner_html() отдаёт HTML (незакрытые <br>, <img>, сырые &).
    Пересобираем во валидный XHTML, иначе ebooklib молча вернёт пустое тело
    и упадёт на генерации nav (lxml ParserError: Document is empty)."""
    if not fragment or not fragment.strip():
        return ""
    node = lxml_html.fragment_fromstring(fragment, create_parent="div")
    return etree.tostring(node, method="xml", encoding="unicode")


def make_xhtml(title, body):
    # Без ведущих отступов, иначе в XHTML попадут лишние пробелы перед тегами.
    # Без пролога <?xml encoding?>: lxml не парсит str с объявлением encoding,
    # ebooklib молча вернёт пустое тело. Свой пролог ebooklib допишет сам.
    body = html_to_xhtml(body)
    return f"""<html xmlns="http://www.w3.org/1999/xhtml" lang="ko">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
</head>
<body>
{body}
</body>
</html>
"""


def split_book_and_chapter(og_title):
    """'어두운 바다의 등불이 되어 1화' -> ('어두운 바다의 등불이 되어', '1화')."""
    if not og_title:
        return None, None
    m = re.search(r"\s*(\d+\s*화)\s*$", og_title)
    if m:
        return og_title[:m.start()].strip(), m.group(1).replace(" ", "")
    return og_title.strip(), None


def load_chapter(page, book_id):
    """Открывает главу, возвращает (og_title, html-тело контента)."""
    url = f"https://ridibooks.com/books/{book_id}/view"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Cloudflare может показать промежуточную страницу — ждём настоящий title.
    for _ in range(20):
        t = page.title()
        if "moment" not in t.lower() and "just a" not in t.lower():
            break
        time.sleep(2)

    # SPA-читалка ridibooks: при переходе на новую главу метаданные (og:url)
    # и контent обновляются не мгновенно, старая глава ещё висит в DOM.
    # Ждём, пока og:url не станет указывать на НУЖНЫЙ book_id — иначе поймаем
    # предыдущую главу и запишем дубль в кэш.
    page.wait_for_selector(f"{CONTENT_SELECTOR}", timeout=30000)
    marker = f"/books/{book_id}/"
    for _ in range(20):
        og_url = page.locator(OG_URL).get_attribute("content") or ""
        body = page.locator(CONTENT_SELECTOR).inner_html()
        if marker in og_url and len(body) > 500:
            break
        page.wait_for_timeout(1000)

    og_url = page.locator(OG_URL).get_attribute("content") or ""
    if marker not in og_url:
        # Страница так и не переключилась на нужную главу — не отдаём чужой контент.
        raise RuntimeError(f"og:url={og_url!r} не соответствует book_id {book_id}")

    og_title = page.locator(OG_TITLE).get_attribute("content")
    body = page.locator(CONTENT_SELECTOR).inner_html()
    return og_title, body


def fetch_cover(book_id):
    """Скачивает обложку по предсказуемому CDN-URL (обычный HTTP, без браузера).
    Возвращает bytes или None."""
    import urllib.request
    try:
        req = urllib.request.Request(
            COVER_URL.format(book_id=book_id),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception:
        pass
    return None


def main():
    first_id = int(input("Введите адрес первой главы: ").split('/')[-2])
    last_id = int(input("Введите адрес последней главы: ").split('/')[-2])

    book = epub.EpubBook()
    book.set_identifier(f"ridibooks-{first_id}")
    book.set_language("ko")
    book.add_author("Unknown")

    style = epub.EpubItem(
        uid="style",
        file_name="style/style.css",
        media_type="text/css",
        content=STYLE_CSS,
    )
    book.add_item(style)

    toc = []
    spine = ["nav"]
    book_title_set = False
    cover_set = False

    # Браузер запускаем лениво — только если есть незакэшированные главы.
    # Повторный прогон, где всё уже в кэше, обойдётся без Chromium вообще.
    state = {"browser": None, "page": None}

    def get_page():
        if state["page"] is not None:
            return state["page"]
        browser = launch(headless=True, args=["--no-sandbox"])
        kind, value = find_cookie_source()
        if kind == "storage_state":
            context = browser.new_context(storage_state=value)
            print(f"Куки загружены из {os.path.basename(value)}")
        else:
            context = browser.new_context()
            if kind == "cookies":
                context.add_cookies(value)
                print(f"Куки загружены ({len(value)} шт.) из cookies.txt")
            else:
                print("Куки не найдены — качаю как гость (только бесплатные главы).")
        state["browser"] = browser
        state["page"] = context.new_page()
        return state["page"]

    try:
        num_ch = 1
        for book_id in range(first_id, last_id + 1):
            cached = read_cache(book_id)
            if cached:
                og_title, body = cached
                from_cache = True
            else:
                try:
                    og_title, body = load_chapter(get_page(), book_id)
                except Exception as e:
                    print(f"[{book_id}] пропущена: {type(e).__name__} {e}")
                    continue
                write_cache(book_id, og_title, body)
                from_cache = False

            book_name, chap_name = split_book_and_chapter(og_title)

            if not book_title_set and book_name:
                book.set_title(book_name)
                book_title_set = True

            if not cover_set:
                cover_bytes = fetch_cover(book_id)
                if cover_bytes:
                    book.set_cover("cover.jpg", cover_bytes)
                    cover_set = True

            title = chap_name or og_title or f"Chapter {num_ch}"

            chapter = epub.EpubHtml(
                title=title,
                file_name=f"chapter_{num_ch}.xhtml",
                lang="ko",
            )
            chapter.content = make_xhtml(title, body)
            chapter.add_item(style)

            book.add_item(chapter)
            toc.append(chapter)
            spine.append(chapter)

            print(f"[{book_id}] {title}" + ("  (из кэша)" if from_cache else ""))
            num_ch += 1
            if not from_cache:
                state["page"].wait_for_timeout(1000)
    finally:
        if state["browser"] is not None:
            state["browser"].close()

    if not book_title_set:
        book.set_title("Unknown")

    if len(spine) == 1:
        raise RuntimeError("Не удалось скачать ни одной главы")

    book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out = f"{book.title}.epub"
    epub.write_epub(out, book, {})
    print(f"Готово: {len(toc)} глав -> {out}")


if __name__ == '__main__':
    main()

# https://ridibooks.com/books/425270925/view
# https://ridibooks.com/books/425270949/view
