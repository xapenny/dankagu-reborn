import base64
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from dankagu.assets.portal import render_portal_html
from dankagu.config import settings
from dankagu.core.net import detect_local_ip

logger = logging.getLogger("dankagu.assets.server")

router = APIRouter(tags=["Assets & Portal"])

MIME_MAP = {
    ".db": "application/x-sqlite3",
    ".txt": "text/plain",
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
    ".xab": "application/octet-stream",
}

# Image extensions eligible for the announcement placeholder below.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# Announcement banners are hosted by the original service and are not part of
# the downloadable AssetBundle set, so they are almost never present locally.
# Returning 404 for them makes Unity report an unrecoverable ProtocolError and
# the client retries; a neutral 1x1 white image keeps the UI intact instead.
#
# The placeholder is chosen to match the requested file extension: serving JPEG
# bytes for a ".png" request (or vice versa) would hand the decoder a payload
# that contradicts the Content-Type.
_PLACEHOLDER_B64: dict[str, tuple[str, str]] = {
    ".jpg": (
        "image/jpeg",
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEP"
        "ERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4e"
        "Hh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCAABAAEDASIA"
        "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEB"
        "AAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ALLAB//Z",
    ),
    ".png": (
        "image/png",
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4"
        "AAAAAElFTkSuQmCC",
    ),
    ".gif": (
        "image/gif",
        "R0lGODdhAQABAIEAAP///wAAAAAAAAAAACwAAAAAAQABAAAIBAABBAQAOw==",
    ),
    ".webp": (
        "image/webp",
        "UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAUAmJaQAA3AA/vz0AAA=",
    ),
}
# ".jpeg" is the same encoding as ".jpg".
_PLACEHOLDER_B64[".jpeg"] = _PLACEHOLDER_B64[".jpg"]
PLACEHOLDER_IMAGES: dict[str, tuple[str, bytes]] = {
    ext: (mime, base64.b64decode(data)) for ext, (mime, data) in _PLACEHOLDER_B64.items()
}


def _announcement_placeholder(clean_path: str) -> tuple[str, bytes] | None:
    """Return ``(media_type, bytes)`` for an announcement image placeholder.

    ``None`` when the path is not an announcement image at all, so the caller
    can fall through to its normal 404 handling.
    """
    if "announcements" not in clean_path.split("/"):
        return None
    suffix = Path(clean_path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return None
    # Every accepted suffix has an entry, so the lookup cannot miss.
    return PLACEHOLDER_IMAGES[suffix]


class AssetCatalogResolver:
    """Resolves missing asset requests to existing files on disk using the manifest catalog.

    When a requested AssetBundle is absent, a same-category bundle is adapted:
    the internal object names are rewritten to match the request so the client's
    LoadAsset() call resolves instead of failing.
    """

    def __init__(self) -> None:
        self.manifest_entries: dict[str, dict] = {}
        self.category_files: dict[str, list[tuple[str, Path, int]]] = {}
        self.generic_fallback: Path | None = None
        self._loaded: bool = False

    def load(self) -> None:
        if self._loaded:
            return
        manifest_file = settings.data_dir / "manifest" / "3760-20221018201621-0358.json"
        if not manifest_file.exists():
            return
        try:
            with open(manifest_file, encoding="utf-8") as fp:
                catalog = json.load(fp)

            # Index categories 1(Android), 2 (iOS), 3 (Common/Sound/Charts), and 4 (DB)
            for cat_key in ("1", "2", "3", "4"):
                for entry in catalog.get("manifest", {}).get(cat_key, []):
                    hp = entry.get("HashedPath", "").lstrip("/").replace("\\", "/")
                    ap = entry.get("AssetPath", "")
                    salt = int(entry.get("DownloadOption", 0)) & 0xFFF
                    self.manifest_entries[hp] = entry

                    file_on_disk = settings.data_dir / "assets" / hp
                    if file_on_disk.exists() and file_on_disk.is_file():
                        cat = "/".join(ap.split("/")[:-1])
                        self.category_files.setdefault(cat, []).append((ap, file_on_disk, salt))
                        if (
                            self.generic_fallback is None
                            and file_on_disk.stat().st_size > 1000
                            and ap.endswith(".xab")
                        ):
                            self.generic_fallback = file_on_disk

            self._loaded = True
            logger.info(
                "📦 Loaded asset fallback catalog with %d categories", len(self.category_files)
            )
        except Exception as exc:
            logger.warning("Could not load asset fallback catalog: %s", exc)

    def _patch_and_cache_xab(
        self,
        tmpl_path: Path,
        tmpl_salt: int,
        target_path: Path,
        target_salt: int,
        old_name: str,
        new_name: str,
    ) -> Path | None:
        import UnityPy

        from dankagu.assets.generator import modify_lightweight_2001

        raw_template = tmpl_path.read_bytes()
        dec = modify_lightweight_2001(raw_template, tmpl_salt)
        if not dec.startswith(b"UnityFS"):
            return None

        env = UnityPy.load(dec)
        for obj in env.objects:
            try:
                tree = obj.read_typetree()
            except Exception:
                continue

            modified = False
            m_name = tree.get("m_Name", "")
            if old_name in m_name:
                tree["m_Name"] = m_name.replace(old_name, new_name)
                modified = True

            if obj.type.name == "AssetBundle":
                c = tree.get("m_Container", [])
                new_c = []
                for item in c:
                    k, v = item[0], item[1]
                    new_k = k.replace(old_name, new_name)
                    new_c.append((new_k, v))
                tree["m_Container"] = new_c
                modified = True

            if obj.type.name == "TextAsset":
                script = tree.get("m_Script", "")
                if isinstance(script, str) and old_name in script:
                    tree["m_Script"] = script.replace(old_name, new_name)
                    modified = True

            if modified:
                obj.save_typetree(tree)

        new_unityfs = env.file.save()
        enc = modify_lightweight_2001(new_unityfs, target_salt)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(enc)
        return target_path

    def resolve_fallback(self, clean_path: str) -> Path | None:
        self.load()
        hp = clean_path
        if "data/" in clean_path:
            hp = clean_path.split("data/")[-1].lstrip("/")

        entry = self.manifest_entries.get(hp)
        if not entry:
            if clean_path.endswith(".xab") and self.generic_fallback:
                return self.generic_fallback
            return None

        ap = entry.get("AssetPath", "")
        target_salt = int(entry.get("DownloadOption", 0)) & 0xFFF
        cat = "/".join(ap.split("/")[:-1])
        base_name = ap.split("/")[-1]

        # 1. Handle .xab AssetBundles
        if ap.endswith(".xab"):
            cached_target = settings.data_dir / "assets" / hp
            if cached_target.exists():
                return cached_target

            candidates = self.category_files.get(cat, [])
            if not candidates:
                parent_cat = "/".join(cat.split("/")[:-1])
                for c, files in self.category_files.items():
                    if c.startswith(parent_cat) and files:
                        candidates = files
                        break

            if candidates:
                tmpl_ap, tmpl_path, tmpl_salt = candidates[0]
                old_base = tmpl_ap.split("/")[-1].replace(".xab", "")
                new_base = base_name.replace(".xab", "")
                try:
                    patched = self._patch_and_cache_xab(
                        tmpl_path=tmpl_path,
                        tmpl_salt=tmpl_salt,
                        target_path=cached_target,
                        target_salt=target_salt,
                        old_name=old_base,
                        new_name=new_base,
                    )
                    if patched and patched.exists():
                        logger.info(
                            "🛠️ [Asset Auto-Patch] Dynamically generated %s (%s) from %s",
                            ap,
                            hp,
                            tmpl_path.name,
                        )
                        return patched
                except Exception as patch_exc:
                    logger.warning("Dynamic patch failed for %s: %s", ap, patch_exc)

                logger.info("🔄 [Asset Fallback] %s -> %s (category fallback)", ap, tmpl_path.name)
                return tmpl_path

            if self.generic_fallback:
                logger.info(
                    "🔄 [Asset Fallback] %s -> generic fallback (%s)",
                    ap,
                    self.generic_fallback.name,
                )
                return self.generic_fallback
            return None

        # 2. Handle non-.xab files (.acb, .awb, .dat, .db): ONLY match same extension
        suffix = Path(ap).suffix.lower()
        candidates = self.category_files.get(cat, [])
        for c_ap, path, _ in candidates:
            if c_ap.endswith(suffix):
                logger.info("🔄 [Asset Fallback] %s -> %s (same suffix %s)", ap, path.name, suffix)
                return path

        return None


asset_resolver = AssetCatalogResolver()


def find_asset_file(path_str: str) -> Path | None:
    """Search for the requested asset path across known asset directories.

    Handles both direct paths and CDN paths prefixed with upload ID / data.
    """
    cleaned = path_str.split("?")[0].replace("\\", "/").lstrip("/")

    candidate_subpaths = [cleaned]
    if "data/" in cleaned:
        candidate_subpaths.append(cleaned.split("data/")[-1].lstrip("/"))

    candidate_bases = [
        settings.data_dir / "assets",
    ]

    for subpath in candidate_subpaths:
        for base in candidate_bases:
            candidate = (base / subpath).resolve()
            if candidate.exists() and candidate.is_file():
                return candidate

    return asset_resolver.resolve_fallback(cleaned)


@router.get("/", response_class=HTMLResponse)
async def get_portal_index() -> HTMLResponse:
    """Deliver a user-friendly web portal with one-click certificate installation."""
    lan_ip = settings.lan_ip or detect_local_ip()
    html_content = render_portal_html(
        lan_ip=lan_ip,
        http_port=settings.asset_port,
        lcx_port=settings.lcx_port,
        grpc_port=settings.grpc_port,
        public_host=settings.public_host,
    )
    return HTMLResponse(content=html_content)


@router.get("/ca.crt")
@router.get("/cert")
async def download_ca_certificate() -> Response:
    """Serve DanKagu Root CA certificate for iOS/Android device installation.

    Using application/x-x509-ca-cert prompts native iOS Safari profile installation.
    """
    ca_path = settings.certs_dir / "ca.crt"
    if not ca_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CA certificate not found. Run 'python -m dankagu.core.certs' first.",
        )

    return FileResponse(
        path=ca_path,
        media_type="application/x-x509-ca-cert",
        filename="DanKagu-RootCA.crt",
        headers={
            "Content-Disposition": 'inline; filename="DanKagu-RootCA.crt"',
        },
    )


@router.get("/server.crt")
async def download_server_certificate() -> Response:
    """Serve server TLS certificate."""
    cert_path = settings.certs_dir / "server.crt"
    if not cert_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server certificate not found.",
        )

    return FileResponse(
        path=cert_path,
        media_type="application/x-x509-ca-cert",
        filename="server.crt",
        headers={
            "Content-Disposition": 'attachment; filename="server.crt"',
        },
    )


@router.get("/status")
async def get_server_status() -> JSONResponse:
    """API endpoint providing current server operational status."""
    lan_ip = settings.lan_ip or detect_local_ip()
    ca_path = settings.certs_dir / "ca.crt"
    server_crt_path = settings.certs_dir / "server.crt"

    return JSONResponse(
        content={
            "status": "online",
            "lan_ip": lan_ip,
            "public_host": settings.public_host or None,
            "ports": {
                "http_portal_cdn": settings.asset_port,
                "lcx_auth_https": settings.lcx_port,
                "lcx_auth_https_ios": settings.lcx_ios_port,
                "takasho_grpc": settings.grpc_port,
                "takasho_grpc_tls": settings.grpc_tls_port,
            },
            "certificates": {
                "ca_present": ca_path.exists(),
                "server_cert_present": server_crt_path.exists(),
            },
        }
    )


@router.get("/assets/{asset_path:path}")
@router.get("/ver/{asset_path:path}")
@router.get("/static/{asset_path:path}")
async def get_asset(asset_path: str, request: Request) -> Response:
    """Serve an asset by hash path, with fallbacks for boot-critical files."""
    clean_path = asset_path.split("?")[0].lstrip("/\\")
    file_path = find_asset_file(clean_path)

    # 1. Exact or searched file found
    if file_path is not None and file_path.exists():
        suffix = file_path.suffix.lower()
        media_type = MIME_MAP.get(suffix, "application/octet-stream")
        resp_headers = {}
        if suffix in [".html", ".txt", ".js", ".json"]:
            resp_headers = {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=file_path.name,
            headers=resp_headers,
        )

    # 2. Terms of service fallback (required for client boot CheckTosVersion)
    if clean_path.endswith("tos.txt") or "tos" in clean_path:
        return Response(
            content="v1",
            media_type="text/plain",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # 3. Privacy policy fallback
    if clean_path.endswith("pp.txt") or "pp" in clean_path:
        return Response(content="1", media_type="text/plain")

    # 4. Static HTML / WebView fallback
    if clean_path.endswith(".html") or "index" in clean_path:
        no_cache_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        static_html = settings.data_dir / "static" / "index.html"
        if static_html.exists():
            return FileResponse(
                path=static_html,
                media_type="text/html",
                filename="index.html",
                headers=no_cache_headers,
            )
        return HTMLResponse(
            content="<html><body>Touhou Danmaku Kagura Offline Preservation</body></html>",
            headers=no_cache_headers,
        )

    # 5. Announcement images: serve a neutral placeholder rather than 404.
    #    These banners live on the original service's CDN and are not part of
    #    the downloadable asset set, so a local copy usually does not exist.
    placeholder = _announcement_placeholder(clean_path)
    if placeholder is not None:
        media_type, image_bytes = placeholder
        logger.debug("Serving placeholder for missing announcement image: %s", clean_path)
        return Response(
            content=image_bytes,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Asset not found: {clean_path}",
    )
