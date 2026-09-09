import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

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

            options = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "socket_timeout": 20,
                "http_headers": {"User-Agent": "Mozilla/5.0"},
            }
            with YoutubeDL(options) as extractor:
                info = extractor.extract_info(source_url, download=False)

            if not info:
                return self.respond(422, {"error": "Não foi possível encontrar mídia pública nesse link."})
            if info.get("entries"):
                entries = [entry for entry in info["entries"] if entry and entry.get("url")]
                info = entries[0] if entries else None
                media_count = len(entries)
            else:
                media_count = 1
            if not info or not info.get("url"):
                return self.respond(422, {"error": "Não foi possível obter uma mídia baixável desse link."})

            media_url = info["url"]
            extension = str(info.get("ext", "mp4")).lower()
            thumbnail = info.get("thumbnail")
            title = info.get("title") or "Mídia pronta para baixar"
            self.respond(200, {
                "status": "ready",
                "source": detect_source(host),
                "type": media_type(info, extension),
                "url": media_url,
                "title": title,
                "thumbnail": thumbnail,
                "filename": f"{safe_filename(title)}.{extension}",
                "media_count": media_count,
            })
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


def safe_filename(value):
    return "".join(character if character.isalnum() or character in " -_" else "" for character in value).strip()[:80] or "soft-download"
