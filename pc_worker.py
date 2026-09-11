"""Worker local do SOFT Downloaders.

Roda no PC e só fica disponível na rede local até um túnel seguro ser ligado.
Não armazena links, contas ou arquivos de quem usa o site.
"""

import json
import hmac
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "api"))

from extract import (  # noqa: E402
    ALLOWED_HOSTS,
    extract_instagram_embed,
    extract_instagram_post_image,
    normalize_media,
)
from yt_dlp import YoutubeDL  # noqa: E402
from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: E402


PORT = 8787
ALLOWED_ORIGINS = {"https://softdownloaders.vercel.app", "https://softdownloader.site"}
WORKER_SECRET = os.environ.get("SOFT_WORKER_SECRET", "")
MEDIA_HOSTS = (
    "fbcdn.net", "cdninstagram.com", "tiktok.com", "tiktokcdn.com",
    "byteoversea.com", "googlevideo.com", "ytimg.com",
)
MEDIA_CACHE_TTL = 20 * 60
MEDIA_CACHE = {}
MEDIA_CACHE_LOCK = Lock()
RUNTIME_CACHE_DIR = Path(tempfile.gettempdir()) / "soft-downloaders-worker-cache"
RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
TIKTOK_SESSION_COOKIEFILE = RUNTIME_CACHE_DIR / "tiktok-session.cookies.txt"
TIKTOK_EXTRACT_LOCK = Lock()
SAFE_PROXY_HEADERS = {
    "accept", "accept-language", "origin", "referer", "user-agent",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
}


def clean_proxy_headers(headers):
    cleaned = {}
    for name, value in (headers or {}).items():
        if str(name).lower() not in SAFE_PROXY_HEADERS:
            continue
        value = str(value)
        if "\r" in value or "\n" in value:
            continue
        cleaned[str(name)] = value
    return cleaned




def content_disposition(filename):
    safe = Path(filename or "soft-download.mp4").name
    return f"attachment; filename*=UTF-8''{quote(safe)}"

def default_proxy_headers(source):
    referers = {
        "instagram": "https://www.instagram.com/",
        "tiktok": "https://www.tiktok.com/",
        "facebook": "https://www.facebook.com/",
        "youtube": "https://www.youtube.com/",
    }
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": referers.get(source, "https://www.instagram.com/"),
    }


def cleanup_cache_entry(entry):
    for key in ("info_path", "cookiefile"):
        value = entry.get(key) if entry else None
        if not value:
            continue
        try:
            Path(value).unlink(missing_ok=True)
        except Exception:
            pass


def cache_media(
    url,
    headers,
    source,
    page_url=None,
    filename=None,
    tiktok_info=None,
    tiktok_cookiefile=None,
):
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    info_path = None

    if source == "tiktok" and tiktok_info:
        try:
            info_path = RUNTIME_CACHE_DIR / f"{token}.info.json"
            info_path.write_text(
                json.dumps(tiktok_info, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as error:
            print(f"Falha ao salvar info-json temporário do TikTok: {type(error).__name__}: {error}")
            info_path = None

    entry = {
        "url": url,
        "headers": clean_proxy_headers(headers),
        "source": source,
        "page_url": page_url,
        "filename": filename,
        "info_path": str(info_path) if info_path else None,
        "cookiefile": tiktok_cookiefile if source == "tiktok" else None,
        "expires": now + MEDIA_CACHE_TTL,
    }
    with MEDIA_CACHE_LOCK:
        expired = [key for key, item in MEDIA_CACHE.items() if item["expires"] <= now]
        for key in expired:
            cleanup_cache_entry(MEDIA_CACHE.pop(key, None))
        MEDIA_CACHE[token] = entry
    return token


def get_cached_media(token):
    now = time.monotonic()
    with MEDIA_CACHE_LOCK:
        entry = MEDIA_CACHE.get(token)
        if not entry or entry["expires"] <= now:
            cleanup_cache_entry(MEDIA_CACHE.pop(token, None))
            return None
        return {**entry, "headers": dict(entry["headers"])}

def select_browser_safe_tiktok_format(info_path):
    """Escolhe um MP4 H.264 + áudio que navegadores reproduzem sem HEVC."""
    if not info_path or not Path(info_path).is_file():
        return None
    try:
        info = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except Exception:
        return None

    candidates = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not fmt.get("format_id"):
            continue
        if str(fmt.get("ext") or "").lower() != "mp4":
            continue
        vcodec = str(fmt.get("vcodec") or "none").lower()
        acodec = str(fmt.get("acodec") or "none").lower()
        if acodec == "none" or vcodec == "none":
            continue
        if not (vcodec.startswith("h264") or vcodec.startswith("avc")):
            continue

        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or 0
        # Até 720p deixa a prévia leve; se só existir H.264 maior, ainda funciona.
        preferred = 1 if height and height <= 720 else 0
        candidates.append((preferred, height, tbr, str(fmt["format_id"])))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def prepare_media_response(media, source_url):
    if not media:
        return media

    tiktok_info = media.pop("_tiktok_info", None)
    tiktok_cookiefile = media.pop("_tiktok_cookiefile", None)
    items = media.get("items") if isinstance(media.get("items"), list) else []
    tiktok_cache_used = False

    for item in items:
        if not item or not item.get("url"):
            continue
        item_source = item.get("source") or media.get("source")
        use_tiktok_bundle = item_source == "tiktok" and not tiktok_cache_used
        item["proxy_id"] = cache_media(
            item["url"],
            item.get("http_headers"),
            item_source,
            page_url=source_url if item_source == "tiktok" else None,
            filename=item.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
        )
        if use_tiktok_bundle:
            tiktok_cache_used = True
        item.pop("http_headers", None)

    if items and media.get("url") == items[0].get("url"):
        media["proxy_id"] = items[0].get("proxy_id")
    elif media.get("url"):
        media_source = media.get("source")
        use_tiktok_bundle = media_source == "tiktok" and not tiktok_cache_used
        media["proxy_id"] = cache_media(
            media["url"],
            media.get("http_headers"),
            media_source,
            page_url=source_url if media_source == "tiktok" else None,
            filename=media.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
        )
        if use_tiktok_bundle:
            tiktok_cache_used = True

    # Se por algum motivo o TikTok não gerou cache, não deixe cookie temporário órfão.
    if tiktok_cookiefile and not tiktok_cache_used:
        try:
            Path(tiktok_cookiefile).unlink(missing_ok=True)
        except Exception:
            pass

    media.pop("http_headers", None)
    return media

def extract_tiktok_fast(source_url):
    """Primeira tentativa do TikTok com sessão isolada e curta.

    Evita serializar todas as análises no cookie jar compartilhado. Se a
    publicação abrir normalmente, já devolvemos os metadados e os cookies
    dessa própria tentativa para o download posterior via info-json.
    """
    cookiefile = RUNTIME_CACHE_DIR / f"fast-{secrets.token_urlsafe(18)}.cookies.txt"
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "skip_download": True,
        "ignoreerrors": False,
        "ignore_no_formats_error": False,
        "socket_timeout": 12,
        "impersonate": ImpersonateTarget.from_str("chrome"),
        "cookiefile": str(cookiefile),
    }
    try:
        with YoutubeDL(options) as extractor:
            info = extractor.extract_info(source_url, download=False)
            sanitized = extractor.sanitize_info(info) if info else None
        if info:
            return info, sanitized, str(cookiefile) if cookiefile.is_file() else None
    except Exception as error:
        print(f"Falha na análise rápida do TikTok: {type(error).__name__}: {error}")

    try:
        cookiefile.unlink(missing_ok=True)
    except Exception:
        pass
    return None, None, None


def extract_tiktok_with_session(source_url):
    """Fallback do TikTok com o cookie jar persistente do worker.

    Só roda quando a tentativa rápida falha. Uma única tentativa evita o
    antigo efeito de 3 extrações pesadas em sequência (dezenas de segundos).
    """
    with TIKTOK_EXTRACT_LOCK:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "skip_download": True,
            "ignoreerrors": False,
            "ignore_no_formats_error": False,
            "socket_timeout": 18,
            "impersonate": ImpersonateTarget.from_str("chrome"),
            "cookiefile": str(TIKTOK_SESSION_COOKIEFILE),
        }
        try:
            with YoutubeDL(options) as extractor:
                info = extractor.extract_info(source_url, download=False)
                sanitized = extractor.sanitize_info(info) if info else None
            if info:
                cookie_snapshot = RUNTIME_CACHE_DIR / (
                    f"extract-{secrets.token_urlsafe(18)}.cookies.txt"
                )
                if TIKTOK_SESSION_COOKIEFILE.is_file():
                    shutil.copy2(TIKTOK_SESSION_COOKIEFILE, cookie_snapshot)
                    cookie_snapshot_value = str(cookie_snapshot)
                else:
                    cookie_snapshot_value = None
                return info, sanitized, cookie_snapshot_value
        except Exception as error:
            print(f"Falha no fallback de sessão TikTok: {type(error).__name__}: {error}")
    return None, None, None


def extract_media(source_url):
    parsed = urlparse(source_url)
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if parsed.scheme not in ("http", "https") or not any(
        host == item or host.endswith("." + item) for item in ALLOWED_HOSTS
    ):
        return None, "Use um link público de Instagram, TikTok, YouTube ou Facebook."

    is_tiktok = "tiktok" in host
    tiktok_cookiefile = None
    tiktok_info = None

    if is_tiktok and curl_requests is not None:
        info, tiktok_info, tiktok_cookiefile = extract_tiktok_fast(source_url)
        if not info:
            info, tiktok_info, tiktok_cookiefile = extract_tiktok_with_session(source_url)
        media = normalize_carousel_media(info, host) if info else None
    else:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "skip_download": True,
            "ignoreerrors": True,
            "ignore_no_formats_error": True,
            "socket_timeout": 25,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        try:
            with YoutubeDL(options) as extractor:
                info = extractor.extract_info(source_url, download=False)
            media = normalize_carousel_media(info, host)
        except Exception:
            media = None

    # Fallback para publicação de foto única, quando o Instagram não retorna
    # os metadados completos do carrossel.
    if not media and "instagram" in host and parsed.path.startswith("/p/"):
        try:
            media = extract_instagram_post_image(source_url)
        except Exception:
            media = None

    if not media and "instagram" in host:
        try:
            media = extract_instagram_embed(source_url)
        except Exception:
            media = None

    if not media:
        if tiktok_cookiefile:
            try:
                Path(tiktok_cookiefile).unlink(missing_ok=True)
            except Exception:
                pass
        return None, "Não foi possível ler este link agora. Confirme se a publicação é pública e tente novamente."

    if is_tiktok:
        media["_tiktok_info"] = tiktok_info
        media["_tiktok_cookiefile"] = tiktok_cookiefile
    return media, None

def normalize_youtube_media(info, host):
    """YouTube sempre representa vídeo; thumbnail é apenas a capa da prévia.

    O yt-dlp pode devolver metadados sem ``url`` no nível principal quando a
    seleção padrão aponta para streams separados. Para o fluxo por proxy do
    SOFT Downloaders, preferimos o melhor formato progressivo (vídeo + áudio)
    e nunca promovemos a thumbnail para arquivo baixável.
    """
    if not info:
        return None

    raw_items = info.get("entries") if info.get("entries") else [info]
    for entry in raw_items:
        if not entry:
            continue

        # Se o yt-dlp já escolheu um arquivo de vídeo completo, use-o.
        if entry.get("url") and str(entry.get("vcodec") or "none").lower() != "none":
            item = normalize_media(entry, host)
            if item and item.get("type") == "video":
                return item

        candidates = []
        for fmt in entry.get("formats") or []:
            if not isinstance(fmt, dict) or not fmt.get("url"):
                continue
            vcodec = str(fmt.get("vcodec") or "none").lower()
            acodec = str(fmt.get("acodec") or "none").lower()
            if vcodec == "none" or acodec == "none":
                continue

            ext = str(fmt.get("ext") or "").lower()
            height = int(fmt.get("height") or 0)
            tbr = float(fmt.get("tbr") or 0)
            # MP4 tem a melhor compatibilidade para preview/download no navegador.
            candidates.append((1 if ext == "mp4" else 0, height, tbr, fmt))

        if not candidates:
            continue

        candidates.sort(key=lambda item: item[:3], reverse=True)
        selected_format = candidates[0][3]
        selected = dict(entry)
        selected.update(selected_format)
        selected["thumbnail"] = entry.get("thumbnail")
        selected["title"] = entry.get("title") or "Vídeo do YouTube"
        selected["http_headers"] = (
            selected_format.get("http_headers")
            or entry.get("http_headers")
            or {}
        )
        item = normalize_media(selected, host)
        if item:
            item["source"] = "youtube"
            item["type"] = "video"
            return item

    return None


def normalize_carousel_media(info, host):
    if not info:
        return None

    # YouTube não possui "imagem para baixar" neste produto. A thumbnail é
    # somente capa; a mídia principal deve ser sempre um vídeo.
    if "youtube" in host or host == "youtu.be":
        item = normalize_youtube_media(info, host)
        if not item:
            return None
        primary = dict(item)
        primary["items"] = [item]
        primary["media_count"] = 1
        return primary

    raw_items = info.get("entries") if info.get("entries") else [info]
    items = []
    for index, entry in enumerate(raw_items, start=1):
        if not entry:
            continue
        if entry.get("url"):
            item = normalize_media(entry, host)
        elif entry.get("thumbnail") and "instagram" in host:
            item = {
                "status": "ready", "source": "instagram", "type": "image",
                "url": entry["thumbnail"], "thumbnail": entry["thumbnail"],
                "title": entry.get("title") or f"Imagem {index}",
                "filename": f"instagram-{entry.get('id') or index}.jpg", "media_count": 1,
            }
        else:
            item = None
        if item and item.get("url"):
            items.append(item)

    if not items:
        return None
    primary = dict(items[0])
    primary["items"] = items
    primary["media_count"] = len(items)
    primary["title"] = info.get("title") or primary["title"]
    return primary


class WorkerHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.respond(204, {})

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"status": "online", "service": "SOFT Downloaders worker"})
        if self.path.startswith("/media?"):
            return self.proxy_media()
        return self.respond(404, {"error": "Rota não encontrada."})

    def do_POST(self):
        if self.path != "/extract":
            return self.respond(404, {"error": "Rota não encontrada."})
        if WORKER_SECRET and not hmac.compare_digest(
            self.headers.get("X-Soft-Worker-Key", ""), WORKER_SECRET
        ):
            return self.respond(401, {"error": "Acesso não autorizado."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size) or b"{}")
            media, error = extract_media(str(data.get("url", "")).strip())
            if error:
                return self.respond(422, {"error": error})
            return self.respond(200, prepare_media_response(media, str(data.get("url", "")).strip()))
        except Exception:
            return self.respond(422, {"error": "Não foi possível processar esse link agora."})

    def respond(self, status, payload):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_response(status)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def proxy_media(self):
        query = parse_qs(urlparse(self.path).query)
        proxy_id = query.get("id", [""])[0]
        download_requested = query.get("dl", [""])[0] == "1"
        cached = get_cached_media(proxy_id) if proxy_id else None

        if proxy_id:
            if not cached:
                return self.respond(410, {"error": "Este link expirou. Analise a publicação novamente."})
            source_url = cached["url"]
            source = cached.get("source")
            headers = default_proxy_headers(source)
            headers.update(cached.get("headers") or {})
        else:
            source_url = query.get("url", [""])[0]
            parsed = urlparse(source_url)
            host = parsed.hostname.lower() if parsed.hostname else ""
            if parsed.scheme != "https" or not any(
                host == item or host.endswith("." + item) for item in MEDIA_HOSTS
            ):
                return self.respond(400, {"error": "Arquivo de mídia não permitido."})
            source = (
                "tiktok" if "tiktok" in host or "byte" in host else
                "facebook" if "fbcdn" in host else
                "youtube" if "googlevideo" in host or "ytimg" in host else
                "instagram"
            )
            headers = default_proxy_headers(source)

        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return self.respond(400, {"error": "Arquivo de mídia não permitido."})

        # No TikTok, reutilizamos o info-json e os cookies gerados na própria
        # análise. Assim o download NÃO abre a página do TikTok uma segunda vez,
        # evitando o erro intermitente "Unable to extract universal data for rehydration".
        if source == "tiktok" and cached:
            return self.proxy_tiktok_with_ytdlp(cached, download_requested)

        requested_range = self.headers.get("Range")
        if requested_range:
            headers["Range"] = requested_range
        elif source == "tiktok":
            # Alguns CDNs do TikTok recusam a transferência completa sem Range,
            # embora aceitem a mesma URL durante a prévia do navegador.
            headers["Range"] = "bytes=0-"

        # TikTok valida não só os headers, mas também o fingerprint TLS/HTTP do
        # cliente. urllib pode receber 403 mesmo com a URL e os headers corretos.
        # curl_cffi usa impersonação de navegador e já é dependência do projeto.
        if source == "tiktok" and curl_requests is not None:
            upstream = None
            try:
                upstream = curl_requests.get(
                    source_url,
                    headers=headers,
                    impersonate="chrome",
                    default_headers=True,
                    accept_encoding="identity",
                    allow_redirects=True,
                    stream=True,
                    timeout=45,
                )
                upstream.raise_for_status()
                self.send_response(upstream.status_code)
                origin = self.headers.get("Origin")
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    value = upstream.headers.get(name)
                    if value:
                        self.send_header(name, value)
                if download_requested:
                    self.send_header("Content-Disposition", content_disposition(cached.get("filename") if cached else None))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for chunk in upstream.iter_content():
                    if chunk:
                        self.wfile.write(chunk)
                return
            except Exception as error:
                print(f"Falha no proxy TikTok via curl_cffi: {type(error).__name__}: {error}")
            finally:
                if upstream is not None:
                    try:
                        upstream.close()
                    except Exception:
                        pass

        try:
            with urlopen(Request(source_url, headers=headers), timeout=45) as upstream:
                self.send_response(getattr(upstream, "status", 200))
                origin = self.headers.get("Origin")
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if upstream.headers.get(name):
                        self.send_header(name, upstream.headers[name])
                if download_requested:
                    self.send_header("Content-Disposition", content_disposition(cached.get("filename") if cached else None))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
        except Exception as error:
            print(f"Falha no proxy {source or 'desconhecido'} via urllib: {type(error).__name__}: {error}")
            self.respond(502, {"error": "Não foi possível preparar este arquivo."})

    def proxy_tiktok_with_ytdlp(self, cached, download_requested=False):
        info_path = cached.get("info_path")
        cookiefile = cached.get("cookiefile")
        page_url = cached.get("page_url")
        filename = cached.get("filename")

        browser_safe_format = select_browser_safe_tiktok_format(info_path)
        if not download_requested and not browser_safe_format:
            # Sem H.264 o navegador pode tocar apenas o áudio e mostrar um quadro vazio.
            # Retornar erro faz o frontend manter a capa em vez de exibir uma prévia quebrada.
            return self.respond(415, {"error": "Prévia de vídeo indisponível para este formato."})

        format_selector = (
            browser_safe_format
            or "best[ext=mp4][vcodec^=h264][acodec!=none][height<=720]/"
               "best[ext=mp4][vcodec^=h264][acodec!=none]/"
               "best[ext=mp4][acodec!=none]/best[ext=mp4]/best"
        )

        command = [
            sys.executable, "-m", "yt_dlp",
            "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
            "--impersonate", "chrome",
            "-f", format_selector,
            "-o", "-",
        ]

        # Caminho principal: usa os metadados + cookies da análise que acabou de
        # funcionar. --load-info-json não reabre a página nem resolve o desafio
        # JavaScript de novo; ele vai direto para a transferência da mídia.
        if info_path and Path(info_path).is_file():
            if cookiefile and Path(cookiefile).is_file():
                command.extend(["--cookies", cookiefile])
            command.extend(["--load-info-json", info_path])
        elif page_url:
            # Fallback para tokens antigos ainda existentes na memória depois de
            # atualização de código. Tokens novos sempre usam info-json.
            command.append(page_url)
        else:
            return self.respond(410, {"error": "Este link expirou. Analise a publicação novamente."})

        process = None
        with tempfile.TemporaryFile(mode="w+b") as error_log:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=error_log,
                    bufsize=0,
                )
                first_chunk = process.stdout.read(64 * 1024) if process.stdout else b""
                if not first_chunk:
                    return_code = process.wait(timeout=20)
                    error_log.seek(0)
                    detail = error_log.read().decode("utf-8", "ignore").strip()
                    print(f"Falha no download TikTok via yt-dlp ({return_code}): {detail[-1600:]}")
                    return self.respond(502, {"error": "Não foi possível preparar este vídeo do TikTok."})

                self.send_response(200)
                origin = self.headers.get("Origin")
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Content-Type", "video/mp4")
                if download_requested:
                    self.send_header("Content-Disposition", content_disposition(filename))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(first_chunk)

                while process.stdout:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

                return_code = process.wait()
                if return_code != 0:
                    error_log.seek(0)
                    detail = error_log.read().decode("utf-8", "ignore").strip()
                    print(f"TikTok interrompido pelo yt-dlp ({return_code}): {detail[-1600:]}")
            except (BrokenPipeError, ConnectionResetError):
                if process and process.poll() is None:
                    process.terminate()
            except Exception as error:
                if process and process.poll() is None:
                    process.terminate()
                print(f"Falha no streaming TikTok via yt-dlp: {type(error).__name__}: {error}")
                if not self.wfile.closed:
                    try:
                        self.respond(502, {"error": "Não foi possível preparar este vídeo do TikTok."})
                    except Exception:
                        pass
            finally:
                if process and process.poll() is None:
                    process.kill()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WorkerHandler)
    print(f"SOFT Downloaders worker ativo na porta {PORT}.")
    server.serve_forever()
