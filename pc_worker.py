"""Worker local do SOFT Downloaders.

Roda no PC e só fica disponível na rede local até um túnel seguro ser ligado.
Não armazena links, contas ou arquivos de quem usa o site.
"""

import json
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
            return self.respond(200, media)
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
        source_url = parse_qs(urlparse(self.path).query).get("url", [""])[0]
        parsed = urlparse(source_url)
        host = parsed.hostname.lower() if parsed.hostname else ""
        if parsed.scheme != "https" or not any(host == item or host.endswith("." + item) for item in MEDIA_HOSTS):
            return self.respond(400, {"error": "Arquivo de mídia não permitido."})
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.instagram.com/"}
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
