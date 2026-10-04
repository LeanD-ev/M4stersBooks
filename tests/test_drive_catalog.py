import json
import tempfile
import threading
import urllib.error
import unittest
from datetime import date

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import _daily_featured, app
from drive_catalog import (
    CatalogError,
    DriveCatalog,
    _best_match,
    _book_metadata,
    _filename_isbn,
    _filename_parts,
    _metadata_cache_key,
)


class DriveCatalogTests(unittest.TestCase):
    def test_daily_featured_advances_with_each_calendar_day(self):
        books = [{"title": f"Livro {index}"} for index in range(3)]

        first_day = _daily_featured(books, date(2026, 10, 2))
        next_day = _daily_featured(books, date(2026, 10, 3))
        after_full_cycle = _daily_featured(books, date(2026, 10, 5))

        self.assertIsNot(first_day, next_day)
        self.assertIs(first_day, after_full_cycle)
        self.assertIsNone(_daily_featured([], date(2026, 10, 2)))

    def test_home_hides_requested_labels_and_shows_icon_only_contact_links(self):
        books = [
            {
                "slug": f"livro-{index}",
                "title": f"Livro {index}",
                "author": f"Autor {index}",
                "genre": "Fantasia",
                "cover": None,
                "metadata_match": True,
                "featured": index == 0,
            }
            for index in range(3)
        ]

        class CatalogStub:
            def get_books_background(self):
                return books

            @property
            def sync_state(self):
                return "ready"

            @property
            def last_error(self):
                return None

            @property
            def warning(self):
                return (
                    "Pesquisa local aplicada a 297 de 299 livros, "
                    "com 258 capas locais."
                )

        with patch("app.catalog", CatalogStub()):
            response = app.test_client().get("/")

        self.assertEqual(200, response.status_code)
        self.assertIn(b'href="#contato">Sobre a curadoria', response.data)
        self.assertIn(b'href="mailto:leazera2@gmail.com"', response.data)
        self.assertIn(
            b'href="https://www.linkedin.com/in/leandro-batista01/"',
            response.data,
        )
        self.assertIn(b'aria-label="Enviar e-mail"', response.data)
        self.assertIn(b'aria-label="Abrir perfil no LinkedIn"', response.data)
        self.assertIn(b'src="/static/m4-books-logo.png"', response.data)
        self.assertNotIn(b"METADADOS PESQUISADOS", response.data)
        self.assertNotIn(b"catalog-count", response.data)
        self.assertNotIn(b"livros encontrados", response.data)
        self.assertNotIn(b"Pesquisa local aplicada", response.data)
        self.assertNotIn(b"identificando os metadados", response.data)
        self.assertNotIn(b">leazera2@gmail.com<", response.data)
        self.assertNotIn(
            b">https://www.linkedin.com/in/leandro-batista01/<",
            response.data,
        )

    def test_brand_logo_is_served_from_static_assets(self):
        response = app.test_client().get("/static/m4-books-logo.png")

        try:
            self.assertEqual(200, response.status_code)
            self.assertEqual("image/png", response.mimetype)
            self.assertGreater(len(response.data), 0)
        finally:
            response.close()

    def test_background_catalog_loading_does_not_block_page_request(self):
        catalog = DriveCatalog(api_key="test-key")
        sync_started = threading.Event()
        finish_sync = threading.Event()

        def wait_for_sync_release():
            sync_started.set()
            finish_sync.wait(timeout=2)

        with patch.object(catalog, "_sync", side_effect=wait_for_sync_release):
            books = catalog.get_books_background()
            self.assertEqual([], books)
            self.assertTrue(sync_started.wait(timeout=1))
            self.assertEqual("syncing", catalog.sync_state)
            finish_sync.set()
            thread = catalog._sync_thread
            assert thread is not None
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

    def test_catalog_throttles_retries_after_drive_failure(self):
        catalog = DriveCatalog(api_key="test-key", cache_seconds=300)
        with patch.object(
            catalog, "_sync", side_effect=CatalogError("Drive indisponível")
        ) as sync:
            with self.assertRaisesRegex(CatalogError, "Drive indisponível"):
                catalog.get_books()
            with self.assertRaisesRegex(CatalogError, "Drive indisponível"):
                catalog.get_books()

        sync.assert_called_once()

    def test_sync_lists_epub_names_without_downloading_epub_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = DriveCatalog(
                api_key="test-key",
                metadata_interval=0,
                metadata_cache_path=Path(directory) / "metadata.json",
            )
            entries = [
                {"id": "file-b-12345678", "name": "Z Livro.epub"},
                {"id": "file-a-12345678", "name": "A Livro.epub"},
            ]
            with (
                patch.object(catalog, "_list_epub_files", return_value=entries),
                patch.object(
                    catalog,
                    "_lookup_metadata",
                    side_effect=[
                        {
                            "author": "Autora Exemplo",
                            "genre": "Romance",
                            "metadata_match": True,
                        },
                        {},
                    ],
                ) as lookup,
                patch.object(
                    catalog,
                    "_download_file",
                    side_effect=AssertionError("EPUB content must not be downloaded"),
                    create=True,
                ),
            ):
                books = catalog.get_books()

        self.assertEqual(
            ["A Livro", "Z Livro"], [book["title"] for book in books]
        )
        self.assertEqual("Autora Exemplo", books[0]["author"])
        self.assertEqual("Romance", books[0]["genre"])
        self.assertEqual(2, lookup.call_count)
        self.assertTrue(all(book["download_url"].startswith("https://drive.google.com/uc?") for book in books))

    def test_sync_uses_online_metadata_without_local_catalog_files(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        entry = {
            "id": "file-12345678",
            "name": "Livro sem registro local - Autora Exemplo.epub",
        }
        with (
            patch.object(catalog, "_list_epub_files", return_value=[entry]),
            patch.object(catalog, "_cached_metadata", return_value=None),
            patch.object(catalog, "_store_metadata"),
            patch.object(
                catalog,
                "_lookup_metadata",
                return_value={
                    "author": "Autora Exemplo",
                    "genre": "Romance",
                    "year": "2024",
                    "cover": "https://books.google.com/cover.jpg",
                    "metadata_source": "https://books.google.com/books?id=example",
                    "metadata_match": True,
                },
            ) as lookup,
        ):
            books = catalog.get_books()

        self.assertEqual(1, len(books))
        self.assertEqual("Autora Exemplo", books[0]["author"])
        self.assertEqual("Romance", books[0]["genre"])
        self.assertEqual("2024", books[0]["year"])
        self.assertEqual("https://books.google.com/cover.jpg", books[0]["cover"])
        self.assertTrue(books[0]["metadata_match"])
        lookup.assert_called_once_with(entry["name"])

    def test_drive_listing_filters_non_epub_files(self):
        response = json.dumps(
            {
                "files": [
                    {"id": "epub-id", "name": "livro.EPUB"},
                    {"id": "pdf-id", "name": "revista.pdf"},
                ]
            }
        ).encode()
        catalog = DriveCatalog(api_key="test-key")
        with patch.object(catalog, "_request", return_value=response):
            entries = catalog._list_epub_files()

        self.assertEqual(["epub-id"], [entry["id"] for entry in entries])

    def test_cached_metadata_avoids_second_google_books_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "metadata.json"
            first = DriveCatalog(
                api_key="test-key",
                metadata_interval=0,
                metadata_cache_path=cache_path,
            )
            entry = {"id": "file-a-12345678", "name": "Livro Exemplo.epub"}
            metadata = {"author": "Autora Cache", "metadata_match": True}
            with (
                patch.object(first, "_list_epub_files", return_value=[entry]),
                patch.object(first, "_lookup_metadata", return_value=metadata) as lookup,
            ):
                first.get_books()
            lookup.assert_called_once()

            second = DriveCatalog(
                api_key="test-key",
                metadata_interval=0,
                metadata_cache_path=cache_path,
            )
            with (
                patch.object(second, "_list_epub_files", return_value=[entry]),
                patch.object(
                    second,
                    "_lookup_metadata",
                    side_effect=AssertionError("metadata should come from cache"),
                ),
            ):
                books = second.get_books()

        self.assertEqual("Autora Cache", books[0]["author"])

    def test_open_library_fallback_maps_metadata_and_cover(self):
        response = {
            "docs": [
                {
                    "title": "Livro de Exemplo",
                    "author_name": ["Autora Exemplo"],
                    "first_publish_year": 2020,
                    "subject": ["Romance", "Ficção"],
                    "publisher": ["Editora Teste"],
                    "isbn": ["9781234567890"],
                    "cover_i": 456,
                    "language": ["por"],
                    "key": "/works/OL123W",
                },
                {"title": "Outro Livro"},
            ]
        }
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        with patch(
            "urllib.request.urlopen",
            return_value=BytesIO(json.dumps(response).encode()),
        ) as open_url:
            metadata = catalog._search_open_library(
                "Livro de Exemplo", None
            )

        request = open_url.call_args.args[0]
        self.assertIn("openlibrary.org/search.json", request.full_url)
        self.assertTrue(metadata["metadata_match"])
        self.assertEqual("Autora Exemplo", metadata["author"])
        self.assertEqual("2020", metadata["year"])
        self.assertIn("/b/id/456-M.jpg", metadata["cover"])
        self.assertEqual("https://openlibrary.org/works/OL123W", metadata["metadata_source"])

    def test_local_cover_is_used_when_online_metadata_has_no_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = DriveCatalog(
                api_key="test-key",
                metadata_interval=0,
                metadata_cache_path=Path(directory) / "metadata.json",
            )
            with (
                patch.object(
                    catalog,
                    "_list_epub_files",
                    return_value=[
                        {"name": "The Breakdown.epub", "id": "file-id"}
                    ],
                ),
                patch.object(
                    catalog,
                    "_lookup_metadata",
                    return_value={
                        "title": "A Beira da Loucura",
                        "cover": None,
                        "metadata_match": True,
                    },
                ),
            ):
                catalog._sync()

            cover = catalog._books[0]["cover"]
            self.assertEqual("/static/capas/9788501113832.jpg", cover)
            response = app.test_client().get(cover)
            response.close()

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.mimetype.startswith("image/"))

    def test_online_cover_takes_precedence_over_local_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = DriveCatalog(
                api_key="test-key",
                metadata_interval=0,
                metadata_cache_path=Path(directory) / "metadata.json",
            )
            with (
                patch.object(
                    catalog,
                    "_list_epub_files",
                    return_value=[
                        {"name": "A Beira da Loucura.epub", "id": "file-id"}
                    ],
                ),
                patch.object(
                    catalog,
                    "_lookup_metadata",
                    return_value={
                        "cover": "https://books.google.com/online-cover.jpg",
                        "metadata_match": True,
                    },
                ),
            ):
                catalog._sync()

        self.assertEqual(
            "https://books.google.com/online-cover.jpg",
            catalog._books[0]["cover"],
        )

    def test_google_books_fallback_maps_metadata_after_title_author_match(self):
        response = {
            "items": [
                {
                    "volumeInfo": {
                        "title": "A Espiã",
                        "authors": ["Tess Gerritsen"],
                        "publishedDate": "2014-07-01",
                        "publisher": "Editora Exemplo",
                        "categories": ["Fiction", "Mystery"],
                        "language": "pt",
                        "description": "<p>Uma história de mistério.</p>",
                        "pageCount": 321,
                        "imageLinks": {
                            "thumbnail": "http://books.example/cover.jpg&edge=curl"
                        },
                        "infoLink": "https://books.google.com/books?id=example",
                        "industryIdentifiers": [
                            {"type": "ISBN_13", "identifier": "9781234567890"}
                        ],
                    }
                }
            ]
        }
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        with patch(
            "urllib.request.urlopen",
            return_value=BytesIO(json.dumps(response).encode()),
        ):
            metadata = catalog._search_google_books(
                "A Espiã", "Tess Gerritsen"
            )

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("Tess Gerritsen", metadata["author"])
        self.assertEqual("2014", metadata["year"])
        self.assertEqual("Uma história de mistério.", metadata["description"])
        self.assertEqual("9781234567890", metadata["isbn"])
        self.assertEqual("321", metadata["pages"])
        self.assertEqual("https://books.example/cover.jpg", metadata["cover"])
        self.assertEqual("https://books.google.com/books?id=example", metadata["metadata_source"])

    def test_lookup_queries_both_sources_and_keeps_google_books_as_primary(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        google_books = {
            "title": "A Espiã",
            "metadata_source": "https://books.google.com/books?id=example",
            "metadata_match": True,
        }
        open_library = {
            "title": "A Espiã",
            "metadata_source": "https://openlibrary.org/works/OL123W",
            "metadata_match": True,
        }
        with (
            patch.object(
                catalog, "_search_google_books", return_value=google_books
            ) as google_search,
            patch.object(
                catalog, "_search_open_library", return_value=open_library
            ) as open_library_search,
        ):
            result = catalog._lookup_metadata("A Espiã - Tess Gerritsen.epub")

        self.assertEqual("A Espiã", result["title"])
        self.assertEqual(
            [
                "https://books.google.com/books?id=example",
                "https://openlibrary.org/works/OL123W",
            ],
            result["metadata_sources"],
        )
        self.assertEqual("https://books.google.com/books?id=example", result["metadata_source"])
        google_search.assert_called_once_with(
            "A Espiã", "Tess Gerritsen", None
        )
        open_library_search.assert_called_once_with(
            "A Espiã", "Tess Gerritsen", None
        )

    def test_lookup_uses_open_library_when_google_books_is_unavailable(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        open_library = {
            "title": "A Espiã",
            "metadata_source": "https://openlibrary.org/works/OL123W",
            "metadata_match": True,
        }
        with (
            patch.object(catalog, "_search_google_books", return_value=None),
            patch.object(
                catalog, "_search_open_library", return_value=open_library
            ) as open_library_search,
        ):
            result = catalog._lookup_metadata("A Espiã - Tess Gerritsen.epub")

        self.assertEqual(open_library, result)
        open_library_search.assert_called_once_with(
            "A Espiã", "Tess Gerritsen", None
        )

    def test_open_library_fills_fields_missing_from_google_books(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        google_books = {
            "title": "A Espiã",
            "author": "Autor não encontrado",
            "description": "Descrição não encontrada nas fontes consultadas.",
            "publisher": "Editora Exemplo",
            "year": "2014",
            "genre": "Gênero não encontrado",
            "cover": None,
            "isbn": "",
            "language": "pt",
            "metadata_source": "https://books.google.com/books?id=example",
            "metadata_match": True,
        }
        open_library = {
            "title": "A Espiã (edição diferente)",
            "author": "Tess Gerritsen",
            "description": "Descrição da Open Library.",
            "publisher": "Outra Editora",
            "year": "2013",
            "genre": "Mistério",
            "cover": "https://covers.openlibrary.org/b/id/123-M.jpg",
            "isbn": "9781234567890",
            "language": "por",
            "metadata_source": "https://openlibrary.org/works/OL123W",
            "metadata_match": True,
        }
        with (
            patch.object(
                catalog, "_search_google_books", return_value=google_books
            ),
            patch.object(
                catalog, "_search_open_library", return_value=open_library
            ),
        ):
            result = catalog._lookup_metadata("A Espiã - Tess Gerritsen.epub")

        self.assertEqual("A Espiã", result["title"])
        self.assertEqual("Tess Gerritsen", result["author"])
        self.assertEqual("Editora Exemplo", result["publisher"])
        self.assertEqual("2014", result["year"])
        self.assertEqual("Mistério", result["genre"])
        self.assertEqual("9781234567890", result["isbn"])
        self.assertEqual("https://covers.openlibrary.org/b/id/123-M.jpg", result["cover"])
        self.assertEqual(
            [
                "https://books.google.com/books?id=example",
                "https://openlibrary.org/works/OL123W",
            ],
            result["metadata_sources"],
        )
        self.assertEqual(
            "https://books.google.com/books?id=example", result["metadata_source"]
        )

    def test_google_books_rate_limit_cools_down_requests(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        error = urllib.error.HTTPError(
            "https://www.googleapis.com/books/v1/volumes",
            429,
            "Too Many Requests",
            {},
            BytesIO(b"{}"),
        )
        with patch("urllib.request.urlopen", side_effect=error) as open_url:
            first = catalog._search_google_books("A Espiã", "Tess Gerritsen")
            second = catalog._search_google_books("A Espiã", "Tess Gerritsen")

        self.assertIsNone(first)
        self.assertIsNone(second)
        open_url.assert_called_once()

    def test_weak_metadata_match_is_not_applied(self):
        document = {"title": "Completely Different Book"}
        self.assertIsNone(_best_match(["A Very Specific Title"], [document]))

    def test_filename_parser_separates_title_series_and_author(self):
        self.assertEqual(
            ("Entrevista com o Vampiro", "Anne Rice"),
            _filename_parts("Entrevista com o Vampiro (As Crônicas #1) Anne Rice.epub"),
        )
        self.assertEqual(
            ("A Família Perfeita", "Lisa Jewell"),
            _filename_parts(
                "(The Family Upstairs #1) A Família Perfeita - Lisa Jewell.epub"
            ),
        )
        self.assertEqual(
            ("A Espiã", "Tess Gerritsen"),
            _filename_parts("A Espiã - Tess Gerritsen.epub"),
        )
        self.assertEqual(
            ("Castelos em seus ossos", None),
            _filename_parts("Castelos em seus ossos (Castelos em seus ossos #1).epub"),
        )

    def test_metadata_matching_rejects_wrong_author(self):
        documents = [
            {"title": "A Espiã", "author_name": ["Outro Autor"]},
        ]
        self.assertIsNone(
            _best_match(["A Espiã"], documents, expected_author="Tess Gerritsen")
        )

    def test_metadata_cache_uses_new_filename_and_author_key(self):
        self.assertEqual(
            "v6:a espia|tess gerritsen",
            _metadata_cache_key("A Espiã - Tess Gerritsen.epub"),
        )

    def test_isbn_is_detected_in_epub_filename(self):
        self.assertEqual(
            "9781234567890",
            _filename_isbn("Livro Exemplo - 978-1-2345-6789-0.epub"),
        )

    def test_book_without_metadata_keeps_drive_download_link(self):
        book = _book_metadata("Livro Sem Dados.epub", "drive-file-12345678")
        self.assertEqual("Livro Sem Dados", book["title"])
        self.assertEqual("Autor não encontrado", book["author"])
        self.assertFalse(book["metadata_match"])
        self.assertIn("drive.google.com/uc?", book["download_url"])

    def test_download_route_redirects_to_matching_drive_file(self):
        class CatalogStub:
            def get_book_background(self, slug):
                if slug != "livro-exemplo-12345678":
                    return None
                return {
                    "slug": slug,
                    "download_url": (
                        "https://drive.google.com/uc?export=download&id=file-id"
                    ),
                }

            @property
            def sync_state(self):
                return "ready"

            @property
            def last_error(self):
                return None

        with patch("app.catalog", CatalogStub()):
            response = app.test_client().get(
                "/livro/livro-exemplo-12345678/epub"
            )

        self.assertEqual(302, response.status_code)
        self.assertEqual(
            "https://drive.google.com/uc?export=download&id=file-id",
            response.headers["Location"],
        )

    def test_book_detail_renders_online_metadata_and_cover(self):
        class CatalogStub:
            def get_book_background(self, slug):
                if slug != "a-beira-da-loucura-mn6ir6qw":
                    return None
                return {
                    "slug": slug,
                    "title": "A Beira da Loucura",
                    "author": "B. A. Paris",
                    "genre": "SUSPENSE E MISTERIO",
                    "year": "2018",
                    "publisher": "Record",
                    "language": "Português",
                    "isbn": "9788501113832",
                    "pages": "350",
                    "translator": "Claudia Costa Guimaraes",
                    "original_title": "The Breakdown",
                    "binding": "Brochura",
                    "cover": "https://books.google.com/cover.jpg",
                    "description": "Cass enfrenta culpa e desconfiança.",
                    "metadata_match": True,
                    "metadata_source": "https://books.google.com/books?id=example",
                }

            @property
            def sync_state(self):
                return "ready"

            @property
            def last_error(self):
                return None

        with patch("app.catalog", CatalogStub()):
            response = app.test_client().get(
                "/livro/a-beira-da-loucura-mn6ir6qw"
            )

        self.assertEqual(200, response.status_code)
        self.assertIn(b'src="/static/m4-books-logo.png"', response.data)
        self.assertIn(b"9788501113832", response.data)
        self.assertIn(b"350", response.data)
        self.assertIn(b"https://books.google.com/cover.jpg", response.data)
        self.assertIn(b"B. A. Paris", response.data)
        self.assertIn(b"SUSPENSE E MISTERIO", response.data)
        self.assertNotIn(b"Pesquisa bibliogr\xc3\xa1fica", response.data)
        self.assertNotIn(b"ISBN corroborado", response.data)
        self.assertNotIn(b"Dados encontrados em uma fonte bibliogr\xc3\xa1fica", response.data)
        self.assertNotIn(b"Ainda n\xc3\xa3o encontramos metadados", response.data)
        self.assertNotIn(b"Ver fonte", response.data)

    def test_drive_api_error_includes_google_reason_without_exposing_key(self):
        api_key = "AIzaTestSecret"
        error_body = json.dumps(
            {
                "error": {
                    "code": 403,
                    "message": "Method doesn't allow unregistered callers.",
                    "errors": [{"reason": "forbidden"}],
                }
            }
        ).encode()
        http_error = urllib.error.HTTPError(
            "https://www.googleapis.com/drive/v3/files",
            403,
            "Forbidden",
            {},
            BytesIO(error_body),
        )
        catalog = DriveCatalog(api_key=api_key)

        try:
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaisesRegex(
                    CatalogError, "unregistered callers"
                ) as raised:
                    catalog._request("https://www.googleapis.com/drive/v3/files")
        finally:
            http_error.close()

        self.assertIn("forbidden", str(raised.exception))
        self.assertNotIn(api_key, str(raised.exception))

    def test_google_automated_query_block_is_reported_clearly(self):
        error = urllib.error.HTTPError(
            "https://www.googleapis.com/drive/v3/files",
            403,
            "Forbidden",
            {},
            BytesIO(
                b"<html>your computer or network may be sending automated queries</html>"
            ),
        )
        try:
            detail = DriveCatalog._google_error_detail(error)
        finally:
            error.close()
        self.assertIn("bloqueou temporariamente", detail)


if __name__ == "__main__":
    unittest.main()
