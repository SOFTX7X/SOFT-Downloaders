import json
import os
import re
import time
from html import unescape
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from yt_dlp import YoutubeDL

ALLOWED_HOSTS = (
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "facebook.com", "fb.watch",
)
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "ogg", "opus", "aac"}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            source_url = str(payload.get("url", "")).strip()
            parsed = urlparse(source_url)
            host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
            if parsed.scheme not in ("http", "https") or not any(host == item or host.endswith("." + item) for item in ALLOWED_HOSTS):
                return self.respond(400, {"error": "Use um link público de Instagram, TikTok, YouTube ou Facebook."})

            worker_url = os.environ.get("SOFT_WORKER_URL", "https://api.forgeaioficial.online").rstrip("/")
            worker_secret = os.environ.get("SOFT_WORKER_SECRET", "")
            if worker_url:
                proxied = request_pc_worker(worker_url, worker_secret, source_url)
                if proxied:
                    return self.respond(200, proxied)
                # No TikTok, uma resposta sem proxy_id deixa apenas uma URL
                # temporária do CDN. Ela pode carregar a prévia e falhar com
                # 403/502 no download. Se o worker não responder, falhamos
                # dentro do site em vez de devolver esse fallback instável.
                if "tiktok" in host:
                    return self.respond(503, {
                        "error": "Não foi possível preparar este vídeo agora. Tente analisar novamente."
                    })

            options = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "socket_timeout": 20,
                "http_headers": {"User-Agent": "Mozilla/5.0"},
            }
            try:
                with YoutubeDL(options) as extractor:
                    info = extractor.extract_info(source_url, download=False)
                media = normalize_media(info, host)
            except Exception:
                media = None

            # Instagram altera com frequência o endpoint usado por extratores.
            # Quando isso ocorre, tentamos a página de incorporação pública como segunda fonte.
            if not media and "instagram" in host:
                media = extract_instagram_embed(source_url)
            if not media:
                return self.respond(422, {"error": "Não foi possível ler este link agora. Confirme se a publicação é pública e tente novamente."})
            self.respond(200, media)
        except Exception:
            self.respond(422, {"error": "Não foi possível ler este link agora. Confirme se a publicação é pública e tente novamente."})

    def do_GET(self):
        self.respond(405, {"error": "Use POST."})

    def respond(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def detect_source(host):
    if "instagram" in host:
        return "instagram"
    if "tiktok" in host:
        return "tiktok"
    if "youtube" in host or host == "youtu.be":
        return "youtube"
    return "facebook"


def media_type(info, extension):
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in AUDIO_EXTENSIONS or info.get("vcodec") == "none":
        return "audio"
    return "video"


def normalize_media(info, host):
    if not info:
        return None
    if info.get("entries"):
        entries = [entry for entry in info["entries"] if entry and entry.get("url")]
        info = entries[0] if entries else None
        media_count = len(entries)
    else:
        media_count = 1
    if not info or not info.get("url"):
        return None
    extension = str(info.get("ext", "mp4")).lower()
    title = info.get("title") or "Mídia pronta para baixar"
    return {
        "status": "ready", "source": detect_source(host), "type": media_type(info, extension),
        "url": info["url"], "title": title, "thumbnail": info.get("thumbnail"),
        "filename": f"{safe_filename(title)}.{extension}", "media_count": media_count,
        "http_headers": info.get("http_headers") or {},
    }


def extract_instagram_embed(source_url):
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in ("p", "reel", "reels"):
        return None
    embed_url = f"https://www.instagram.com/{parts[0]}/{parts[1]}/embed/captioned/"
    request = Request(embed_url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"})
    with urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", "ignore")
    video_url = extract_embedded_url(page, "video_url")
    image_url = extract_embedded_url(page, "display_url")
    media_url = video_url or image_url
    if not media_url:
        return None
    kind = "video" if video_url else "image"
    extension = "mp4" if kind == "video" else "jpg"
    return {
        "status": "ready", "source": "instagram", "type": kind, "url": media_url,
        "title": "Mídia do Instagram", "thumbnail": image_url,
        "filename": f"instagram-{parts[1]}.{extension}", "media_count": 1,
    }


def extract_instagram_post_image(source_url):
    """Gets the public cover image for an Instagram photo or a carousel's first item."""
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "p":
        return None
    request = Request(
        f"https://www.instagram.com/p/{parts[1]}/",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"},
    )
    with urlopen(request, timeout=25) as response:
        page = response.read().decode("utf-8", "ignore")
    match = re.search(
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page, re.IGNORECASE
    )
    if not match:
        return None
    image_url = unescape(match.group(1))
    title_match = re.search(
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page, re.IGNORECASE
    )
    title = unescape(title_match.group(1)) if title_match else "Imagem do Instagram"
    return {
        "status": "ready", "source": "instagram", "type": "image", "url": image_url,
        "title": title, "thumbnail": image_url,
        "filename": f"instagram-{parts[1]}.jpg", "media_count": 1,
    }


def extract_embedded_url(page, field):
    match = re.search(rf'{field}\\?"\s*:\\?"(https.*?)(?<!\\)\\?"', page)
    if not match:
        return None
    return match.group(1).replace("\\u0026", "&").replace("\\/", "/").replace("\\", "")


def request_pc_worker(worker_url, worker_secret, source_url):
    """Uses the PC worker and retries once for transient tunnel/TikTok failures."""
    body = json.dumps({"url": source_url}).encode("utf-8")
    for attempt in range(2):
        request = Request(
            f"{worker_url}/extract",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Soft-Worker-Key": worker_secret,
                "User-Agent": "SOFT-Downloaders/1.0",
            },
        )
        try:
            with urlopen(request, timeout=35) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "ready" and data.get("url"):
                return data
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.7)
    return None


def safe_filename(value):
    return "".join(character if character.isalnum() or character in " -_" else "" for character in value).strip()[:80] or "soft-download"
