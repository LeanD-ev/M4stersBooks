import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request

from drive_catalog import DriveCatalog


load_dotenv(
    Path(__file__).resolve().with_name(".env"),
    override=not bool(os.environ.get("GOOGLE_DRIVE_API_KEY")),
)

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

catalog = DriveCatalog()
FEATURED_TIMEZONE = timezone(timedelta(hours=-3))


def _daily_featured(books: list[dict], current_date: date) -> dict | None:
    if not books:
        return None
    return books[current_date.toordinal() % len(books)]


@app.get("/")
def index():
    search = request.args.get("q", "").strip()
    selected_genre = request.args.get("genre", "").strip()
    all_books = catalog.get_books_background()
    catalog_status = catalog.sync_state
    catalog_error = catalog.last_error if catalog_status == "error" else None
    catalog_warning = catalog.warning
    if catalog_error:
        app.logger.error("Não foi possível carregar o catálogo do Drive: %s", catalog_error)

    books = all_books
    if search:
        normalized_search = search.casefold()
        books = [
            book
            for book in books
            if normalized_search in book["title"].casefold()
            or normalized_search in book["author"].casefold()
            or normalized_search in book["genre"].casefold()
        ]

    if selected_genre:
        books = [book for book in books if book["genre"] == selected_genre]

    genres = sorted(
        {
            book["genre"]
            for book in all_books
            if book["genre"] and book["genre"] != "Gênero não encontrado"
        }
    )
    featured = _daily_featured(
        all_books, datetime.now(FEATURED_TIMEZONE).date()
    )
    return render_template(
        "index.html",
        books=books,
        featured=featured,
        genres=genres,
        search=search,
        selected_genre=selected_genre,
        catalog_error=catalog_error,
        catalog_warning=catalog_warning,
        catalog_status=catalog_status,
    )


@app.get("/livro/<slug>")
def book_detail(slug):
    book = catalog.get_book_background(slug)
    if book is None:
        if catalog.sync_state == "syncing":
            abort(503, description="O catálogo ainda está sincronizando os arquivos EPUB.")
        if catalog.last_error:
            abort(503, description=catalog.last_error)
        abort(404)
    return render_template("book.html", book=book)


@app.get("/livro/<slug>/epub")
def download_epub(slug):
    book = catalog.get_book_background(slug)
    if book is None:
        if catalog.sync_state == "syncing":
            abort(503, description="O catálogo ainda está sincronizando os arquivos EPUB.")
        if catalog.last_error:
            abort(503, description=catalog.last_error)
        abort(404)
    return redirect(book["download_url"], code=302)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
