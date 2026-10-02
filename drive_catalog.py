import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_FOLDER_ID = "1IctnXWMu5IWqfFHjo9a_cyWvdFSBDfGP"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
CACHE_SECONDS = 300
METADATA_CACHE_SECONDS = 30 * 24 * 60 * 60
NEGATIVE_CACHE_SECONDS = 6 * 60 * 60
METADATA_REQUEST_INTERVAL = 1.0
TITLE_STOP_WORDS = {
    "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos",
    "e", "em", "na", "nas", "no", "nos", "o", "os", "para", "por",
    "the", "and", "of", "to", "in",
}
EDITION_WORDS = {
    "colecao", "coleção", "edicao", "edição", "especial", "ebook", "epub",
    "kindle", "volume", "vol", "livro", "book", "capa", "dura", "digital",
}


class CatalogError(Exception):
    """An error that prevents the Drive catalog from being read."""


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize_title(value).split()
        if token not in TITLE_STOP_WORDS and not token.isdigit()
    }


def _slug_base(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_title(value)).strip("-")
    return slug or "livro"


def _filename_title(filename: str) -> str:
    return _filename_parts(filename)[0]


def _filename_isbn(filename: str) -> str | None:
    for match in re.finditer(
        r"(?<!\d)\d[\d\s-]{8,18}[\dXx](?!\d)",
        PurePosixPath(filename).stem,
    ):
        isbn = re.sub(r"[\s-]", "", match.group(0)).upper()
        if len(isbn) in (10, 13):
            return isbn
    return None


def _looks_like_author(value: str) -> bool:
    words = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    if not 2 <= len(words) <= 5 or any(word.isdigit() for word in words):
        return False
    normalized = _normalize_title(value).split()
    return not any(word in EDITION_WORDS for word in normalized)


def _filename_parts(filename: str) -> tuple[str, str | None]:
    stem = PurePosixPath(filename).stem
    stem = re.sub(r"\s+", " ", stem).strip()
    author: str | None = None

    parenthetical_author = re.match(r"^(.+?)\s*\([^)]*\)\s+([^()]*)$", stem)
    if parenthetical_author and _looks_like_author(parenthetical_author.group(2)):
        stem = parenthetical_author.group(1).strip()
        author = parenthetical_author.group(2).strip()

    stem = re.sub(r"^\([^)]*\)\s*", "", stem)

    if author is None:
        parts = re.split(r"\s*[-–—]\s*", stem)
        possible_author = parts[-1].strip() if len(parts) > 1 else ""
        if _looks_like_author(possible_author):
            author = possible_author
            stem = " - ".join(parts[:-1]).strip()

    stem = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", stem)
    stem = re.sub(r"^\s*\d{1,3}[.)_-]?\s*", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -–—_.,")
    title = stem or PurePosixPath(filename).stem or "Título não identificado"
    return title, author


def _metadata_cache_key(filename: str) -> str:
    title, author = _filename_parts(filename)
    normalized = _normalize_title(title)
    if author:
        normalized = f"{normalized}|{_normalize_title(author)}"
    return f"v6:{normalized}"


def _title_candidates(filename: str) -> list[str]:
    title, _ = _filename_parts(filename)
    return [title]


def _best_match(
    candidates: list[str],
    documents: list[dict[str, Any]],
    expected_author: str | None = None,
) -> dict[str, Any] | None:
    best_document: dict[str, Any] | None = None
    best_score = 0.0
    expected_author_tokens = _meaningful_tokens(expected_author or "")
    for document in documents:
        title = document.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        title_tokens = _meaningful_tokens(title)
        for candidate in candidates:
            candidate_tokens = _meaningful_tokens(candidate)
            if not candidate_tokens or not title_tokens:
                continue
            sequence_score = SequenceMatcher(
                None, _normalize_title(candidate), _normalize_title(title)
            ).ratio()
            overlap_score = len(candidate_tokens & title_tokens) / max(
                len(candidate_tokens), len(title_tokens)
            )
            score = sequence_score * 0.55 + overlap_score * 0.45
            if expected_author_tokens:
                result_authors = document.get("author_name") or document.get("authors") or []
                result_author_tokens = _meaningful_tokens(" ".join(result_authors))
                author_overlap = len(expected_author_tokens & result_author_tokens) / max(
                    len(expected_author_tokens), 1
                )
                if author_overlap < 0.5:
                    continue
                score = score * 0.75 + author_overlap * 0.25
            if score > best_score:
                best_score = score
                best_document = document
    minimum_score = 0.72 if expected_author_tokens else 0.82
    if best_score < minimum_score:
        return None
    return best_document


def _book_metadata(filename: str, file_id: str) -> dict[str, Any]:
    title, author = _filename_parts(filename)
    suffix = file_id[-8:].lower()
    return {
        "slug": f"{_slug_base(title)}-{suffix}",
        "title": title,
        "author": author or "Autor não encontrado",
        "description": "Descrição não encontrada nas fontes consultadas.",
        "publisher": "Editora não encontrada",
        "year": "Ano não encontrado",
        "genre": "Gênero não encontrado",
        "cover": None,
        "isbn": "",
        "language": "Idioma não encontrado",
        "metadata_source": None,
        "metadata_match": False,
        "drive_file_id": file_id,
        "download_url": DriveCatalog._drive_download_url(file_id),
    }


class DriveCatalog:
    def __init__(
        self,
        folder_id: str | None = None,
        api_key: str | None = None,
        cache_seconds: int = CACHE_SECONDS,
        metadata_interval: float = METADATA_REQUEST_INTERVAL,
        metadata_cache_path: Path | None = None,
    ) -> None:
        self.folder_id = folder_id or os.environ.get(
            "GOOGLE_DRIVE_FOLDER_ID", DEFAULT_FOLDER_ID
        )
        self.api_key = api_key or os.environ.get("GOOGLE_DRIVE_API_KEY", "")
        self.cache_seconds = cache_seconds
        self.metadata_interval = metadata_interval
        self.metadata_cache_path = metadata_cache_path or (
            Path(__file__).resolve().parent
            / "instance"
            / "book_metadata.json"
        )
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._last_sync = 0.0
        self._last_attempt = 0.0
        self._last_metadata_request = 0.0
        self._open_library_cooldown_until = 0.0
        self._google_books_cooldown_until = 0.0
        self._last_error: str | None = None
        self._syncing = False
        self._sync_thread: threading.Thread | None = None
        self._books: list[dict[str, Any]] = []
        self._books_by_slug: dict[str, dict[str, Any]] = {}
        self._metadata_cache = self._load_metadata_cache()
        self.warning: str | None = None

    def get_books(self) -> list[dict[str, Any]]:
        self._sync_if_needed()
        with self._lock:
            return [dict(book) for book in self._books]

    def get_books_background(self) -> list[dict[str, Any]]:
        self._start_background_sync()
        with self._lock:
            return [dict(book) for book in self._books]

    def get_book_background(self, slug: str) -> dict[str, Any] | None:
        self.get_books_background()
        with self._lock:
            book = self._books_by_slug.get(slug)
            return dict(book) if book else None

    def get_book(self, slug: str) -> dict[str, Any] | None:
        self._sync_if_needed()
        with self._lock:
            book = self._books_by_slug.get(slug)
            return dict(book) if book else None

    @property
    def sync_state(self) -> str:
        with self._lock:
            if self._syncing:
                return "syncing"
            if (
                self._last_error
                and time.monotonic() - self._last_attempt < self.cache_seconds
            ):
                return "error"
            if self._last_sync:
                return "ready"
            return "idle"

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _sync_if_needed(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_sync < self.cache_seconds:
                return
            if self._last_error and now - self._last_attempt < self.cache_seconds:
                raise CatalogError(self._last_error)

        with self._sync_lock:
            with self._lock:
                now = time.monotonic()
                if now - self._last_sync < self.cache_seconds:
                    return
                if self._last_error and now - self._last_attempt < self.cache_seconds:
                    raise CatalogError(self._last_error)
                self._last_attempt = now
            try:
                self._sync()
            except CatalogError as error:
                with self._lock:
                    self._last_error = str(error)
                raise
            with self._lock:
                self._last_error = None

    def _start_background_sync(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._syncing or now - self._last_sync < self.cache_seconds:
                return
            if self._last_error and now - self._last_attempt < self.cache_seconds:
                return
            self._syncing = True
            self._sync_thread = threading.Thread(
                target=self._run_background_sync,
                name="drive-catalog-sync",
                daemon=True,
            )
            self._sync_thread.start()

    def _run_background_sync(self) -> None:
        try:
            self._sync_if_needed()
        except CatalogError:
            pass
        finally:
            with self._lock:
                self._syncing = False

    def _sync(self) -> None:
        if not self.api_key:
            raise CatalogError(
                "A integração com o Google Drive ainda não está configurada. "
                "Defina a variável GOOGLE_DRIVE_API_KEY com uma chave da API "
                "Google Drive."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.folder_id):
            raise CatalogError("O ID da pasta do Google Drive é inválido.")

        entries = sorted(
            self._list_epub_files(),
            key=lambda entry: _filename_title(entry.get("name", "")).casefold(),
        )
        books = [
            _book_metadata(entry.get("name", ""), entry["id"])
            for entry in entries
        ]
        for index, book in enumerate(books):
            book["featured"] = index == 0

        with self._lock:
            self._books = books
            self._books_by_slug = {book["slug"]: book for book in books}
            self._last_sync = time.monotonic()

        unmatched = 0
        for entry, book in zip(entries, books):
            cache_key = _metadata_cache_key(entry.get("name", ""))
            cached = self._cached_metadata(cache_key)
            if cached is None:
                cached = self._lookup_metadata(entry.get("name", ""))
                self._store_metadata(cache_key, cached)
            if cached:
                book.update(cached)
                book["slug"] = _book_metadata(
                    entry.get("name", ""), entry["id"]
                )["slug"]
                book["drive_file_id"] = entry["id"]
                book["download_url"] = self._drive_download_url(entry["id"])
            else:
                unmatched += 1
            with self._lock:
                current = self._books_by_slug.get(book["slug"])
                if current is not None:
                    current.update(book)

        with self._lock:
            self.warning = (
                f"{unmatched} livro(s) não têm correspondência confiável "
                "nas fontes bibliográficas."
                if unmatched
                else None
            )

    def _list_epub_files(self) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {
                "q": f"'{self.folder_id}' in parents and trashed = false",
                "pageSize": "1000",
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            url = f"{DRIVE_API_URL}/files?{urllib.parse.urlencode(params)}"
            response = self._request(url)
            try:
                page = json.loads(response.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CatalogError("A resposta do Google Drive não é válida.") from error

            for entry in page.get("files", []):
                name = entry.get("name", "")
                if name.casefold().endswith(".epub"):
                    files.append(entry)
            page_token = page.get("nextPageToken", "")
            if not page_token:
                return files

    @staticmethod
    def _drive_download_url(file_id: str) -> str:
        params = {"export": "download", "id": file_id, "confirm": "t"}
        return f"https://drive.google.com/uc?{urllib.parse.urlencode(params)}"

    def _request(self, url: str) -> bytes:
        request = urllib.request.Request(
            url, headers={"User-Agent": "M4Books/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = self._google_error_detail(error)
            if error.code in (401, 403):
                raise CatalogError(
                    "O Google Drive recusou a chamada. Verifique se a Google Drive "
                    "API está ativada no mesmo projeto da chave, se a chave permite "
                    "essa API e se a pasta está compartilhada como leitora. "
                    f"Resposta do Google: {detail}"
                ) from error
            if error.code == 404:
                raise CatalogError(
                    "A pasta do Drive não foi encontrada ou não está compartilhada."
                ) from error
            raise CatalogError(
                f"Erro na API do Google Drive (HTTP {error.code}): {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise CatalogError("Não foi possível conectar à API do Google Drive.") from error

    @staticmethod
    def _google_error_detail(error: urllib.error.HTTPError) -> str:
        try:
            response_body = error.read(4096).decode("utf-8", "replace")
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if "automated queries" in response_body.casefold():
                return (
                    "Google bloqueou temporariamente solicitações automatizadas "
                    "desta rede. Aguarde antes de tentar novamente."
                )
            return "a API não retornou detalhes legíveis"

        api_error = payload.get("error", {})
        message = api_error.get("message", "")
        reasons = sorted(
            {
                item.get("reason", "")
                for item in api_error.get("errors", [])
                if item.get("reason")
            }
        )
        details = [message] if message else []
        if reasons:
            details.append(f"motivo: {', '.join(reasons)}")
        return "; ".join(details) or "a API não retornou detalhes do erro"

    def _load_metadata_cache(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.metadata_cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        now = time.time()
        return {
            key: value["metadata"]
            for key, value in payload.items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and isinstance(value.get("metadata"), dict)
            and now - value.get("cached_at", 0)
            < (
                METADATA_CACHE_SECONDS
                if value["metadata"].get("metadata_match")
                else NEGATIVE_CACHE_SECONDS
            )
        }

    def _cached_metadata(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            metadata = self._metadata_cache.get(key)
            return dict(metadata) if metadata is not None else None

    def _store_metadata(self, key: str, metadata: dict[str, Any] | None) -> None:
        with self._lock:
            self._metadata_cache[key] = metadata or {}
            cache_snapshot = {
                cache_key: {"cached_at": time.time(), "metadata": cache_value}
                for cache_key, cache_value in self._metadata_cache.items()
            }
        self.metadata_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.metadata_cache_path.with_suffix(".tmp")
        try:
            temporary_path.write_text(
                json.dumps(cache_snapshot, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary_path.replace(self.metadata_cache_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)

    def _lookup_metadata(self, filename: str) -> dict[str, Any] | None:
        query, expected_author = _filename_parts(filename)
        isbn = _filename_isbn(filename)
        google_books_match = self._search_google_books(
            query, expected_author, isbn
        )
        open_library_match = self._search_open_library(query, expected_author, isbn)
        return self._merge_metadata_sources(
            google_books_match,
            open_library_match,
        )

    @staticmethod
    def _merge_metadata_sources(
        google_books: dict[str, Any] | None,
        open_library: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if google_books is None:
            return dict(open_library) if open_library else None
        if open_library is None:
            merged = dict(google_books)
            source = google_books.get("metadata_source")
            merged["metadata_sources"] = [source] if source else []
            return merged

        merged = dict(google_books)
        fallback_values = {
            "author": "Autor não encontrado",
            "description": "Descrição não encontrada nas fontes consultadas.",
            "publisher": "Editora não encontrada",
            "year": "Ano não encontrado",
            "genre": "Gênero não encontrado",
            "cover": None,
            "isbn": "",
            "language": "Idioma não encontrado",
            "pages": None,
        }
        for field, empty_value in fallback_values.items():
            if merged.get(field) in (None, "", empty_value):
                fallback_value = open_library.get(field)
                if fallback_value not in (None, "", empty_value):
                    merged[field] = fallback_value

        sources = [
            source
            for source in (
                google_books.get("metadata_source"),
                open_library.get("metadata_source"),
            )
            if source
        ]
        merged["metadata_sources"] = list(dict.fromkeys(sources))
        merged["metadata_source"] = (
            google_books.get("metadata_source")
            or open_library.get("metadata_source")
        )
        return merged

    def _wait_for_metadata_slot(self) -> None:
        elapsed = time.monotonic() - self._last_metadata_request
        if self._last_metadata_request and elapsed < self.metadata_interval:
            time.sleep(self.metadata_interval - elapsed)
        self._last_metadata_request = time.monotonic()

    def _search_open_library(
        self, query: str, expected_author: str | None, isbn: str | None = None
    ) -> dict[str, Any] | None:
        if time.monotonic() < self._open_library_cooldown_until:
            return None
        params = {
            "isbn" if isbn else "title": isbn or query,
            "limit": "10",
            "fields": (
                "title,author_name,first_publish_year,subject,publisher,"
                "isbn,cover_i,language,key"
            ),
        }
        if expected_author and not isbn:
            params["author"] = expected_author
        url = f"{OPEN_LIBRARY_URL}?{urllib.parse.urlencode(params)}"
        self._wait_for_metadata_slot()

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "M4Books/1.0 (metadata lookup; https://openlibrary.org)"
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                self._open_library_cooldown_until = time.monotonic() + 30 * 60
            return None
        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None
        documents = payload.get("docs", [])
        if not isinstance(documents, list):
            return None
        match = (
            next(
                (
                    document
                    for document in documents
                    if isbn and isbn in document.get("isbn", [])
                ),
                None,
            )
            if isbn
            else _best_match([query], documents, expected_author)
        )
        if match is None:
            return None

        authors = match.get("author_name") or []
        subjects = match.get("subject") or []
        publishers = match.get("publisher") or []
        isbns = match.get("isbn") or []
        languages = match.get("language") or []
        cover_id = match.get("cover_i")
        work_key = match.get("key")
        return self._metadata_from_search(
            match,
            title=match.get("title") or query,
            authors=authors,
            subjects=subjects,
            publishers=publishers,
            isbns=isbns,
            languages=languages,
            cover_url=(
                f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg?default=false"
                if cover_id
                else None
            ),
            source=(
                f"https://openlibrary.org{work_key}" if work_key else "Open Library"
            ),
        )

    def _search_google_books(
        self, query: str, expected_author: str | None, isbn: str | None = None
    ) -> dict[str, Any] | None:
        if time.monotonic() < self._google_books_cooldown_until:
            return None
        search_query = (
            f"isbn:{isbn}"
            if isbn
            else f'intitle:"{query}"'
        )
        if expected_author and not isbn:
            search_query += f' inauthor:"{expected_author}"'
        params = {"q": search_query, "maxResults": "10", "printType": "books"}
        api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
        if api_key:
            params["key"] = api_key
        url = f"{GOOGLE_BOOKS_URL}?{urllib.parse.urlencode(params)}"
        self._wait_for_metadata_slot()
        request = urllib.request.Request(
            url, headers={"User-Agent": "M4Books/1.0 (book metadata lookup)"}
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                self._google_books_cooldown_until = time.monotonic() + 30 * 60
            return None
        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None
        items = payload.get("items", [])
        if not isinstance(items, list):
            return None
        documents = []
        for item in items:
            info = item.get("volumeInfo", {})
            documents.append(
                {
                    **info,
                    "authors": info.get("authors") or [],
                    "_info": info,
                }
            )
        match = (
            next(
                (
                    document
                    for document in documents
                    if isbn
                    and any(
                        identifier.get("identifier") == isbn
                        for identifier in document["_info"].get(
                            "industryIdentifiers", []
                        )
                    )
                ),
                None,
            )
            if isbn
            else _best_match([query], documents, expected_author)
        )
        if match is None:
            return None

        info = match["_info"]
        identifiers = info.get("industryIdentifiers", [])
        isbn = next(
            (
                item.get("identifier", "")
                for item in identifiers
                if item.get("type") == "ISBN_13"
            ),
            "",
        ) or next(
            (
                item.get("identifier", "")
                for item in identifiers
                if item.get("type") == "ISBN_10"
            ),
            "",
        )
        image_links = info.get("imageLinks") or {}
        cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if cover_url:
            cover_url = cover_url.replace("http://", "https://")
            cover_url = cover_url.replace("&edge=curl", "")
        categories = info.get("categories") or []
        published_date = str(info.get("publishedDate", ""))
        year_match = re.search(r"\d{4}", published_date)
        description = info.get("description") or ""
        source = info.get("infoLink") or info.get("canonicalVolumeLink")
        page_count = info.get("pageCount")
        return self._metadata_from_search(
            match,
            title=info.get("title") or query,
            authors=info.get("authors") or [],
            subjects=categories,
            publishers=[info["publisher"]] if info.get("publisher") else [],
            isbns=[isbn] if isbn else [],
            languages=[info["language"]] if info.get("language") else [],
            cover_url=cover_url,
            source=source or "Google Books",
            description=description,
            year=year_match.group(0) if year_match else None,
            pages=str(page_count) if page_count else None,
        )

    @staticmethod
    def _metadata_from_search(
        match: dict[str, Any],
        *,
        title: str,
        authors: list[str],
        subjects: list[str],
        publishers: list[str],
        isbns: list[str],
        languages: list[str],
        cover_url: str | None,
        source: str,
        description: str = "",
        year: str | None = None,
        pages: str | None = None,
    ) -> dict[str, Any]:
        if not year:
            year = str(match.get("first_publish_year") or "")
        if not description:
            description = "Descrição não encontrada nas fontes consultadas."
        description = re.sub(r"<[^>]+>", " ", description)
        description = re.sub(r"\s+", " ", description).strip()
        metadata = {
            "title": title,
            "author": ", ".join(authors[:3]) or "Autor não encontrado",
            "description": description,
            "publisher": publishers[0] if publishers else "Editora não encontrada",
            "year": year or "Ano não encontrado",
            "genre": ", ".join(subjects[:3]) or "Gênero não encontrado",
            "cover": cover_url,
            "isbn": isbns[0] if isbns else "",
            "language": ", ".join(languages[:3]) or "Idioma não encontrado",
            "metadata_source": source,
            "metadata_sources": [source],
            "metadata_match": True,
        }
        if pages:
            metadata["pages"] = pages
        return metadata
