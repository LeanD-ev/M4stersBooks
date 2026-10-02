import argparse
import hashlib
import logging
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
SYNC_ROOT = Path.home() / "Desktop" / "M4_Books_push"
BOOKS_SOURCE = SYNC_ROOT / "Books"
DEFAULT_DRIVE_FOLDER_ID = "1IctnXWMu5IWqfFHjo9a_cyWvdFSBDfGP"
STABILITY_DELAY_SECONDS = 15
LOGGER = logging.getLogger("m4books.drive_sync")
LOCAL_AUTH_DIR = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
) / "M4Books"

load_dotenv(PROJECT_DIR / ".env")


@dataclass(frozen=True)
class SyncConfig:
    books_dir: Path = BOOKS_SOURCE
    drive_folder_id: str = DEFAULT_DRIVE_FOLDER_ID
    credentials_path: Path = LOCAL_AUTH_DIR / "client_secret.json"
    token_path: Path = LOCAL_AUTH_DIR / "drive_sync_token.json"
    interval_seconds: int = 60
    stability_delay_seconds: int = STABILITY_DELAY_SECONDS

    @classmethod
    def from_environment(cls) -> "SyncConfig":
        return cls(
            books_dir=Path(os.environ.get("M4_BOOKS_SOURCE_DIR", BOOKS_SOURCE)),
            drive_folder_id=os.environ.get(
                "GOOGLE_DRIVE_FOLDER_ID", DEFAULT_DRIVE_FOLDER_ID
            ),
            credentials_path=Path(
                os.environ.get(
                    "GOOGLE_OAUTH_CLIENT_FILE",
                    LOCAL_AUTH_DIR / "client_secret.json",
                )
            ),
            token_path=Path(
                os.environ.get(
                    "GOOGLE_OAUTH_TOKEN_FILE",
                    LOCAL_AUTH_DIR / "drive_sync_token.json",
                )
            ),
            interval_seconds=max(
                10, int(os.environ.get("M4_SYNC_INTERVAL_SECONDS", "60"))
            ),
            stability_delay_seconds=max(
                5,
                int(
                    os.environ.get(
                        "M4_SYNC_STABILITY_SECONDS",
                        str(STABILITY_DELAY_SECONDS),
                    )
                ),
            ),
        )


class DriveSync:
    def __init__(self, service: Any, config: SyncConfig) -> None:
        self.service = service
        self.config = config
        self._checksum_cache: dict[Path, tuple[int, int, str]] = {}

    def sync_once(self) -> None:
        self.config.books_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Sincronizando arquivos em %s.", self.config.books_dir)
        self._sync_directory(self.config.books_dir, self.config.drive_folder_id)

    def _sync_directory(self, local_dir: Path, remote_parent_id: str) -> None:
        remote_files = self._list_children(remote_parent_id)
        by_name: dict[str, list[dict[str, Any]]] = {}
        for remote_file in remote_files:
            by_name.setdefault(remote_file.get("name", ""), []).append(remote_file)

        failures: list[str] = []
        for local_path in sorted(
            local_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
            try:
                if (
                    local_path.name.startswith(".")
                    or local_path.name in {"Thumbs.db"}
                    or local_path.name.startswith("~$")
                ):
                    LOGGER.info("Ignorando arquivo temporário: %s", local_path)
                    continue
                if local_path.is_symlink():
                    LOGGER.warning("Ignorando link simbólico: %s", local_path)
                    continue
                if local_path.is_dir():
                    LOGGER.warning(
                        "Ignorando subpasta '%s'; coloque os EPUBs diretamente em "
                        "Books para que o site os catalogue.",
                        local_path.name,
                    )
                    continue
                if not local_path.is_file():
                    continue
                if not self._is_stable(local_path):
                    LOGGER.info("Aguardando o arquivo terminar de ser gravado: %s", local_path)
                    continue
                self._sync_file(local_path, remote_parent_id, by_name)
            except Exception:
                LOGGER.exception("Falha ao sincronizar %s", local_path)
                failures.append(str(local_path))

        if failures:
            raise RuntimeError(
                f"{len(failures)} item(ns) falharam ao sincronizar em {local_dir}; "
                "os itens restantes serão tentados novamente no próximo ciclo."
            )

    def _sync_file(
        self,
        local_path: Path,
        remote_parent_id: str,
        remote_by_name: dict[str, list[dict[str, Any]]],
    ) -> None:
        local_md5 = self._file_checksum(local_path)
        remote_matches = remote_by_name.get(local_path.name, [])
        if len(remote_matches) > 1:
            matching = [
                item
                for item in remote_matches
                if item.get("md5Checksum") == local_md5
            ]
            if len(matching) == 1:
                LOGGER.info("Já sincronizado: %s", local_path.name)
                return
            raise RuntimeError(
                f"Há nomes duplicados no Drive para '{local_path.name}'; "
                "nenhum arquivo foi alterado."
            )

        remote_file = remote_matches[0] if remote_matches else None
        if remote_file and remote_file.get("md5Checksum") == local_md5:
            LOGGER.info("Já sincronizado: %s", local_path.name)
            return
        if remote_file:
            raise RuntimeError(
                f"Já existe '{local_path.name}' no Drive com conteúdo diferente; "
                "o arquivo remoto não será sobrescrito."
            )

        current_stat = local_path.stat()
        mime_type = (
            mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        )
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            str(local_path),
            mimetype=mime_type,
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        request = self.service.files().create(
            body={"name": local_path.name, "parents": [remote_parent_id]},
            media_body=media,
            supportsAllDrives=True,
            fields="id,name,md5Checksum",
        )
        response = None
        while response is None:
            _, response = request.next_chunk()

        if (
            local_path.stat().st_size != current_stat.st_size
            or _md5_file(local_path) != local_md5
        ):
            raise RuntimeError(
                f"'{local_path.name}' mudou enquanto era enviado; "
                "o arquivo será conferido novamente no próximo ciclo."
            )
        remote_checksum = response.get("md5Checksum")
        if remote_checksum and remote_checksum != local_md5:
            raise RuntimeError(
                f"A verificação de integridade falhou para '{local_path.name}'."
            )
        remote_by_name[local_path.name] = [
            {
                "id": response["id"],
                "name": local_path.name,
                "md5Checksum": remote_checksum or local_md5,
            }
        ]
        LOGGER.info("Enviado ao Drive: %s", local_path.name)

    def _is_stable(self, path: Path) -> bool:
        return time.time() - path.stat().st_mtime >= self.config.stability_delay_seconds

    def _file_checksum(self, path: Path) -> str:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self._checksum_cache.get(path)
        if cached and cached[:2] == signature:
            return cached[2]
        checksum = _md5_file(path)
        final_stat = path.stat()
        if (final_stat.st_size, final_stat.st_mtime_ns) != signature:
            raise RuntimeError(f"'{path.name}' mudou durante a verificação.")
        self._checksum_cache[path] = (*signature, checksum)
        return checksum

    def _list_children(self, parent_id: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    pageSize=1000,
                    pageToken=page_token,
                    fields="nextPageToken,files(id,name,mimeType,md5Checksum)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
                .execute()
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authorize(config: SyncConfig) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Dependências OAuth ausentes. Instale "
            "'python -m pip install -r sync-requirements.txt'."
        ) from error

    config.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    if not config.credentials_path.is_file():
        raise FileNotFoundError(
            "Credenciais OAuth não encontradas. Baixe o JSON de cliente OAuth "
            f"do tipo aplicativo para computador e salve em {config.credentials_path}."
        )

    scopes = ["https://www.googleapis.com/auth/drive"]
    credentials = None
    if config.token_path.is_file():
        credentials = Credentials.from_authorized_user_file(
            str(config.token_path), scopes
        )
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.credentials_path), scopes
        )
        credentials = flow.run_local_server(port=0, open_browser=True)

    config.token_path.parent.mkdir(parents=True, exist_ok=True)
    config.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Envia arquivos novos da pasta local de livros ao Google Drive."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa uma única verificação em vez de continuar monitorando.",
    )
    arguments = parser.parse_args()
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                SYNC_ROOT / "drive_sync.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ],
    )

    config = SyncConfig.from_environment()
    if not config.drive_folder_id:
        LOGGER.error("GOOGLE_DRIVE_FOLDER_ID não está configurado.")
        return 2

    try:
        service = _authorize(config)
        synchronizer = DriveSync(service, config)
        if arguments.once:
            synchronizer.sync_once()
            return 0

        LOGGER.info(
            "Monitorando novos arquivos a cada %s segundos.",
            config.interval_seconds,
        )
        while True:
            try:
                synchronizer.sync_once()
            except Exception:
                LOGGER.exception(
                    "O ciclo de sincronização falhou; uma nova tentativa ocorrerá "
                    "em %s segundos.",
                    config.interval_seconds,
                )
            time.sleep(config.interval_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Sincronização interrompida pelo usuário.")
        return 0
    except Exception:
        LOGGER.exception("Não foi possível iniciar a sincronização.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
