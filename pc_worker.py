"""Worker local do SOFT Downloaders.

Roda no PC e só fica disponível na rede local até um túnel seguro ser ligado.
Não armazena links, contas ou arquivos de quem usa o site.
"""

import json
import hmac
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "api"))

from extract import (  # noqa: E402
    ALLOWED_HOSTS,
    extract_instagram_embed,
    extract_instagram_post_image,
    normalize_media,
)
from yt_dlp import YoutubeDL  # noqa: E402


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


def cache_media(url, headers, source):
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    entry = {
        "url": url,
        "headers": clean_proxy_headers(headers),
        "source": source,
        "expires": now + MEDIA_CACHE_TTL,
    }
    with MEDIA_CACHE_LOCK:
        expired = [key for key, item in MEDIA_CACHE.items() if item["expires"] <= now]
        for key in expired:
            MEDIA_CACHE.pop(key, None)
        MEDIA_CACHE[token] = entry
    return token


def get_cached_media(token):
    now = time.monotonic()
    with MEDIA_CACHE_LOCK:
        entry = MEDIA_CACHE.get(token)
        if not entry or entry["expires"] <= now:
            MEDIA_CACHE.pop(token, None)
            return None
        return {**entry, "headers": dict(entry["headers"])}


def prepare_media_response(media):
    if not media:
        return media

    items = media.get("items") if isinstance(media.get("items"), list) else []
    for item in items:
        if not item or not item.get("url"):
            continue
        item["proxy_id"] = cache_media(
            item["url"], item.get("http_headers"), item.get("source") or media.get("source")
        )
        item.pop("http_headers", None)

    if items and media.get("url") == items[0].get("url"):
        media["proxy_id"] = items[0].get("proxy_id")
    elif media.get("url"):
        media["proxy_id"] = cache_media(
            media["url"], media.get("http_headers"), media.get("source")
        )

    media.pop("http_headers", None)
    return media


def extract_media(source_url):
    parsed = urlparse(source_url)
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if parsed.scheme not in ("http", "https") or not any(
        host == item or host.endswith("." + item) for item in ALLOWED_HOSTS
    ):
        return None, "Use um link público de Instagram, TikTok, YouTube ou Facebook."

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
        return None, "Não foi possível ler este link agora. Confirme se a publicação é pública e tente novamente."
    return media, None


def normalize_carousel_media(info, host):
    if not info:
        return None

    raw_items = info.get("entries") if info.get("entries") else [info]
    items = []
    for index, entry in enumerate(raw_items, start=1):
        if not entry:
            continue
        if entry.get("url"):
            item = normalize_media(entry, host)
        elif entry.get("thumbnail"):
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
            return self.respond(200, prepare_media_response(media))
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

        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            with urlopen(Request(source_url, headers=headers), timeout=45) as upstream:
                self.send_response(getattr(upstream, "status", 200))
                origin = self.headers.get("Origin")
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if upstream.headers.get(name):
                        self.send_header(name, upstream.headers[name])
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
        except Exception:
            self.respond(502, {"error": "Não foi possível preparar este arquivo."})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WorkerHandler)
    print(f"SOFT Downloaders worker ativo na porta {PORT}.")
    server.serve_forever()
