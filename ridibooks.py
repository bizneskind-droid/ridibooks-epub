"""
Скачивает главы книги с ridibooks.com и собирает EPUB.

Требуется cloakbrowser (стелс-хромиум, проходит Cloudflare ridibooks):
    python3 -m venv venv && ./venv/bin/pip install cloakbrowser ebooklib
    ./venv/bin/python ridibooks.py

На этом VPS (Ubuntu, AppArmor) нужен --no-sandbox — уже прописан в launch().
"""

import re
import time
from cloakbrowser import launch
from ebooklib import epub
from lxml import html as lxml_html
from lxml import etree

# --- Селекторы / источники данных (проверены на живой странице ridibooks) ---
CONTENT_SELECTOR = "#viewer_contents"          # стабильный id читалки
OG_TITLE = 'meta[property="og:title"]'          # "<название книги> N화"
COVER_URL = "https://img.ridicdn.net/cover/{book_id}"

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

    # Контент читалки (mxviewer) дорисовывается JS — ждём появления.
    page.wait_for_selector(f"{CONTENT_SELECTOR}", timeout=30000)
    # даём тексту догрузиться
    for _ in range(15):
        body = page.locator(CONTENT_SELECTOR).inner_html()
        if len(body) > 500:
            break
        page.wait_for_timeout(1000)

    og_title = page.locator(OG_TITLE).get_attribute("content")
    body = page.locator(CONTENT_SELECTOR).inner_html()
    return og_title, body


def fetch_cover(page, book_id):
    """Скачивает обложку по предсказуемому CDN-URL. Возвращает bytes или None."""
    try:
        resp = page.request.get(COVER_URL.format(book_id=book_id))
        if resp.ok:
            return resp.body()
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

    browser = launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page()

    try:
        num_ch = 1
        for book_id in range(first_id, last_id + 1):
            try:
                og_title, body = load_chapter(page, book_id)
            except Exception as e:
                print(f"[{book_id}] пропущена: {type(e).__name__} {e}")
                continue

            book_name, chap_name = split_book_and_chapter(og_title)

            if not book_title_set and book_name:
                book.set_title(book_name)
                book_title_set = True

            if not cover_set:
                cover_bytes = fetch_cover(page, book_id)
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

            print(f"[{book_id}] {title}")
            num_ch += 1
            page.wait_for_timeout(1000)
    finally:
        browser.close()

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
