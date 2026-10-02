import json
import tempfile
import threading
import urllib.error
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import app
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

    def test_sync_applies_local_research_data_and_cover_without_online_lookup(self):
        catalog = DriveCatalog(api_key="test-key", metadata_interval=0)
        entry = {
            "id": "file-mn6ir6qw",
            "name": "A Beira da Loucura.epub",
        }
        with (
            patch.object(catalog, "_list_epub_files", return_value=[entry]),
            patch.object(
                catalog,
                "_lookup_metadata",
                side_effect=AssertionError("local research data should be used"),
            ),
        ):
            books = catalog.get_books()

        self.assertEqual(1, len(books))
        self.assertEqual("B. A. Paris", books[0]["author"])
        self.assertEqual("SUSPENSE E MISTERIO", books[0]["genre"])
        self.assertEqual("2018", books[0]["year"])
        self.assertEqual("Record", books[0]["publisher"])
        self.assertEqual("9788501113832", books[0]["isbn"])
        self.assertEqual("350", books[0]["pages"])
        self.assertTrue(books[0]["cover"].startswith("/static/capas/"))
        self.assertTrue(
            Path(__file__).resolve().parent.parent.joinpath(
                "static",
                "capas",
                books[0]["cover"].rsplit("/", 1)[-1],
            ).is_file()
        )
        self.assertEqual("ISBN corroborado", books[0]["research_status"])
        self.assertTrue(books[0]["metadata_match"])

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

    def test_cached_metadata_avoids_second_open_library_lookup(self):
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

    def test_metadata_lookup_matches_a_title_and_returns_open_library_cover(self):
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
            metadata = catalog._lookup_metadata("Livro de Exemplo.epub")

        request = open_url.call_args.args[0]
        self.assertIn("openlibrary.org/search.json", request.full_url)
        self.assertTrue(metadata["metadata_match"])
        self.assertEqual("Autora Exemplo", metadata["author"])
        self.assertEqual("2020", metadata["year"])
        self.assertIn("/b/id/456-M.jpg", metadata["cover"])
        self.assertEqual("https://openlibrary.org/works/OL123W", metadata["metadata_source"])

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
        self.assertEqual("https://books.example/cover.jpg", metadata["cover"])
        self.assertEqual("https://books.google.com/books?id=example", metadata["metadata_source"])

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
            "v3:a espia|tess gerritsen",
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

    def test_book_detail_renders_researched_metadata_and_local_cover(self):
        class CatalogStub:
            def get_book_background(self, slug):
                if slug != "a-beira-da-loucura-mn6ir6qw":
                    return None
                return {
                    "slug": slug,
                    "title": "A Beira da Loucura",
                    "research_title": "A beira da loucura",
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
                    "cover": "/static/capas/9788501113832.jpg",
                    "description": "Cass enfrenta culpa e desconfiança.",
                    "research_status": "ISBN corroborado",
                    "metadata_match": True,
                    "metadata_source": "https://example.com/book",
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
        self.assertIn(b"ISBN corroborado", response.data)
        self.assertIn(b"9788501113832", response.data)
        self.assertIn(b"350", response.data)
        self.assertIn(b"/static/capas/9788501113832.jpg", response.data)
        self.assertIn(b"Ver fonte", response.data)

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
