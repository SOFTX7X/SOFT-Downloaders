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
from urllib.parse import urlparse

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
ALLOWED_ORIGINS = {"https://softdownloaders.vercel.app"}
WORKER_SECRET = os.environ.get("SOFT_WORKER_SECRET", "")


def extract_media(source_url):
    parsed = urlparse(source_url)
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if parsed.scheme not in ("http", "https") or not any(
        host == item or host.endswith("." + item) for item in ALLOWED_HOSTS
    ):
        return None, "Use um link público de Instagram, TikTok, YouTube ou Facebook."

    # O Instagram não entrega fotos como "formato de vídeo" ao yt-dlp.
    # Lemos a imagem pública do post antes de tentar a extração de vídeos.
    if "instagram" in host and parsed.path.startswith("/p/"):
        try:
            image = extract_instagram_post_image(source_url)
            if image:
                return image, None
        except Exception:
            pass

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 25,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    try:
        with YoutubeDL(options) as extractor:
            media = normalize_media(extractor.extract_info(source_url, download=False), host)
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


class WorkerHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.respond(204, {})

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"status": "online", "service": "SOFT Downloaders worker"})
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

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WorkerHandler)
    print(f"SOFT Downloaders worker ativo na porta {PORT}.")
    server.serve_forever()
