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
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse
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
    safe_filename,
)
from yt_dlp import YoutubeDL  # noqa: E402
from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: E402


PORT = 8787
ALLOWED_ORIGINS = {"https://softdownloaders.vercel.app", "https://softdownloader.site"}
WORKER_SECRET = os.environ.get("SOFT_WORKER_SECRET", "")
MEDIA_HOSTS = (
    "fbcdn.net", "cdninstagram.com", "tiktok.com", "tiktokcdn.com",
    "byteoversea.com", "googlevideo.com", "ytimg.com", "twimg.com", "pinimg.com",
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
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "soft-download.mp4"
    ascii_name = ascii_name.replace('"', "").replace("\r", "").replace("\n", "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


def download_content_type(filename):
    extension = Path(filename or "").suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
    }.get(extension)


def default_proxy_headers(source):
    referers = {
        "instagram": "https://www.instagram.com/",
        "tiktok": "https://www.tiktok.com/",
        "facebook": "https://www.facebook.com/",
        "youtube": "https://www.youtube.com/",
        "twitter": "https://x.com/",
        "pinterest": "https://www.pinterest.com/",
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
    youtube_info=None,
    instagram_info=None,
    facebook_info=None,
    item_index=None,
    media_type=None,
):
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    info_path = None

    info_payload = (
        tiktok_info if source == "tiktok" else
        youtube_info if source == "youtube" else
        instagram_info if source == "instagram" else
        facebook_info if source == "facebook" else
        None
    )
    if info_payload:
        try:
            info_path = RUNTIME_CACHE_DIR / f"{token}.info.json"
            info_path.write_text(
                json.dumps(info_payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as error:
            print(f"Falha ao salvar info-json temporário de {source}: {type(error).__name__}: {error}")
            info_path = None

    entry = {
        "url": url,
        "headers": clean_proxy_headers(headers),
        "source": source,
        "page_url": page_url,
        "filename": filename,
        "info_path": str(info_path) if info_path else None,
        "cookiefile": tiktok_cookiefile if source == "tiktok" else None,
        "item_index": item_index,
        "media_type": media_type,
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
    youtube_info = media.pop("_youtube_info", None)
    facebook_info = media.pop("_facebook_info", None)
    items = media.get("items") if isinstance(media.get("items"), list) else []
    tiktok_cache_used = False
    youtube_cache_used = False

    for item_index, item in enumerate(items, start=1):
        if not item or not item.get("url"):
            continue
        item_source = item.get("source") or media.get("source")
        use_tiktok_bundle = item_source == "tiktok" and not tiktok_cache_used
        use_youtube_bundle = item_source == "youtube" and not youtube_cache_used
        instagram_info = item.pop("_instagram_info", None) if item_source == "instagram" else None
        instagram_index = item.pop("_instagram_index", item_index) if item_source == "instagram" else None
        item_facebook_info = item.pop("_facebook_info", None) if item_source == "facebook" else None
        facebook_index = item.pop("_facebook_index", item_index) if item_source == "facebook" else None
        item["proxy_id"] = cache_media(
            item["url"],
            item.get("http_headers"),
            item_source,
            page_url=source_url if item_source in ("tiktok", "youtube", "instagram", "facebook", "twitter", "pinterest") else None,
            filename=item.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
            youtube_info=youtube_info if use_youtube_bundle else None,
            instagram_info=instagram_info,
            facebook_info=item_facebook_info,
            item_index=facebook_index if item_source == "facebook" else instagram_index,
            media_type=item.get("type"),
        )
        if use_tiktok_bundle:
            tiktok_cache_used = True
        if use_youtube_bundle:
            youtube_cache_used = True
        item.pop("http_headers", None)

    if items and media.get("url") == items[0].get("url"):
        media["proxy_id"] = items[0].get("proxy_id")
    elif media.get("url"):
        media_source = media.get("source")
        use_tiktok_bundle = media_source == "tiktok" and not tiktok_cache_used
        use_youtube_bundle = media_source == "youtube" and not youtube_cache_used
        media["proxy_id"] = cache_media(
            media["url"],
            media.get("http_headers"),
            media_source,
            page_url=source_url if media_source in ("tiktok", "youtube", "instagram", "facebook", "twitter", "pinterest") else None,
            filename=media.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
            youtube_info=youtube_info if use_youtube_bundle else None,
            instagram_info=media.pop("_instagram_info", None) if media_source == "instagram" else None,
            facebook_info=media.pop("_facebook_info", facebook_info) if media_source == "facebook" else None,
            item_index=(
                media.pop("_facebook_index", 1) if media_source == "facebook" else
                media.pop("_instagram_index", 1) if media_source == "instagram" else
                None
            ),
            media_type=media.get("type"),
        )
        if use_tiktok_bundle:
            tiktok_cache_used = True
        if use_youtube_bundle:
            youtube_cache_used = True

    # Se por algum motivo o TikTok não gerou cache, não deixe cookie temporário órfão.
    if tiktok_cookiefile and not tiktok_cache_used:
        try:
            Path(tiktok_cookiefile).unlink(missing_ok=True)
        except Exception:
            pass

    media.pop("http_headers", None)
    media.pop("_instagram_info", None)
    media.pop("_instagram_index", None)
    media.pop("_facebook_info", None)
    media.pop("_facebook_index", None)
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
        return None, "Use um link público de Instagram, TikTok, YouTube, Facebook, X/Twitter ou Pinterest."

    is_tiktok = "tiktok" in host
    is_youtube = "youtube" in host or host == "youtu.be"
    is_instagram = "instagram" in host
    is_facebook = "facebook" in host or host == "fb.watch"
    is_twitter = host == "x.com" or host.endswith(".x.com") or "twitter.com" in host
    is_pinterest = host == "pin.it" or host == "pinterest.com" or host.endswith(".pinterest.com")
    tiktok_cookiefile = None
    tiktok_info = None
    youtube_info = None
    facebook_info = None

    # Pinterest não possui extrator dedicado no yt-dlp. Para Pins públicos,
    # lemos os dados SSR/GraphQL que a própria página envia ao navegador.
    # Isso cobre imagens, vídeos progressivos MP4 e carrosséis quando expostos.
    if is_pinterest:
        try:
            pinterest_media = extract_pinterest_public_media(source_url)
            if pinterest_media:
                return pinterest_media, None
        except Exception as error:
            print(f"Falha no extrator público do Pinterest: {type(error).__name__}: {error}")
        return None, "Não foi possível ler este Pin agora. Confirme se ele é público e tente novamente."

    # X/Twitter oferece um endpoint público de syndication usado nos embeds.
    # Ele é a melhor primeira tentativa porque também expõe posts de foto e
    # carrosséis, que o yt-dlp nem sempre retorna como mídia baixável.
    if is_twitter:
        try:
            twitter_media = extract_x_syndication(source_url)
            if twitter_media:
                return twitter_media, None
        except Exception as error:
            print(f"Falha no syndication do X/Twitter: {type(error).__name__}: {error}")

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
                if is_youtube and info:
                    youtube_info = extractor.sanitize_info(info)
                instagram_info = extractor.sanitize_info(info) if is_instagram and info else None
                facebook_info = extractor.sanitize_info(info) if is_facebook and info else None
            media = normalize_carousel_media(
                info, host, instagram_info if is_instagram else facebook_info
            )
        except Exception as error:
            if is_youtube:
                print(f"Falha na análise do YouTube: {type(error).__name__}: {error}")
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

    # Posts públicos de foto do Facebook nem sempre são expostos pelo yt-dlp.
    # Como fallback, usamos os metadados públicos da própria página (og:image /
    # og:video), sem exigir login ou sessão do usuário.
    if not media and is_facebook:
        try:
            media = extract_facebook_public_metadata(source_url)
        except Exception as error:
            print(f"Falha no fallback público do Facebook: {type(error).__name__}: {error}")
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
    if is_youtube:
        media["_youtube_info"] = youtube_info
    if is_facebook and facebook_info:
        media["_facebook_info"] = facebook_info
    return media, None

def normalize_youtube_media(info, host):
    """YouTube sempre representa vídeo; thumbnail é apenas a capa.

    A maioria dos vídeos atuais entrega vídeo e áudio em streams separados.
    Portanto não exigimos uma URL progressiva aqui: guardamos a página do
    vídeo e deixamos o yt-dlp + FFmpeg preparar o MP4 somente no download.
    """
    if not info:
        return None

    raw_items = info.get("entries") if info.get("entries") else [info]
    for entry in raw_items:
        if not entry:
            continue

        has_video = str(entry.get("vcodec") or "none").lower() != "none"
        if not has_video:
            for fmt in entry.get("formats") or []:
                if not isinstance(fmt, dict):
                    continue
                if str(fmt.get("vcodec") or "none").lower() != "none":
                    has_video = True
                    break
        if not has_video:
            continue

        video_id = entry.get("id") or info.get("id")
        page_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or info.get("webpage_url")
            or info.get("original_url")
            or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
        )
        if not page_url:
            continue

        title = entry.get("title") or info.get("title") or "Vídeo do YouTube"
        return {
            "status": "ready",
            "source": "youtube",
            "type": "video",
            "url": page_url,
            "title": title,
            "thumbnail": entry.get("thumbnail") or info.get("thumbnail"),
            "filename": f"{safe_filename(title)}.mp4",
            "media_count": 1,
            "http_headers": entry.get("http_headers") or info.get("http_headers") or {},
        }

    return None


def select_instagram_video_format(entry):
    """Retorna a melhor URL de vídeo quando o Instagram só expõe a capa no nível principal."""
    candidates = []
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        vcodec = str(fmt.get("vcodec") or "none").lower()
        if vcodec == "none":
            continue
        acodec = str(fmt.get("acodec") or "none").lower()
        ext = str(fmt.get("ext") or "").lower()
        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or 0
        score = (
            1 if acodec != "none" else 0,
            1 if ext == "mp4" else 0,
            1 if vcodec.startswith(("h264", "avc")) else 0,
            height,
            tbr,
        )
        candidates.append((score, fmt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def normalize_instagram_entry(entry, host, index):
    # Reels podem vir sem entry["url"] e trazer apenas thumbnail + formats.
    # Antes de assumir que é foto, procuramos uma mídia de vídeo real.
    video_format = select_instagram_video_format(entry)
    if video_format:
        video_info = dict(entry)
        video_info.update({
            "url": video_format["url"],
            "ext": video_format.get("ext") or entry.get("ext") or "mp4",
            "vcodec": video_format.get("vcodec") or entry.get("vcodec") or "h264",
            "acodec": video_format.get("acodec") or entry.get("acodec"),
            "http_headers": video_format.get("http_headers") or entry.get("http_headers") or {},
        })
        item = normalize_media(video_info, host)
        if item:
            item["type"] = "video"
            return item

    if entry.get("url"):
        return normalize_media(entry, host)

    if entry.get("thumbnail"):
        return {
            "status": "ready", "source": "instagram", "type": "image",
            "url": entry["thumbnail"], "thumbnail": entry["thumbnail"],
            "title": entry.get("title") or f"Imagem {index}",
            "filename": f"instagram-{entry.get('id') or index}.jpg", "media_count": 1,
        }
    return None



def select_facebook_video_format(entry):
    """Escolhe uma faixa MP4 adequada para a prévia do Facebook.

    Para a prévia damos preferência a vídeo progressivo (com áudio), H.264 e
    resolução de até 720p. O download final continua sendo preparado pelo
    yt-dlp + FFmpeg, então não sacrificamos a qualidade do arquivo baixado.
    """
    candidates = []
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        vcodec = str(fmt.get("vcodec") or "none").lower()
        if vcodec == "none":
            continue
        acodec = str(fmt.get("acodec") or "none").lower()
        ext = str(fmt.get("ext") or "").lower()
        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or 0
        score = (
            1 if acodec != "none" else 0,
            1 if ext == "mp4" else 0,
            1 if vcodec.startswith(("h264", "avc")) else 0,
            1 if height and height <= 720 else 0,
            height if height <= 720 else -height,
            tbr,
        )
        candidates.append((score, fmt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def normalize_facebook_entry(entry, host, index):
    video_format = select_facebook_video_format(entry)
    if video_format:
        title = entry.get("title") or f"Vídeo do Facebook {index}"
        return {
            "status": "ready",
            "source": "facebook",
            "type": "video",
            "url": video_format["url"],
            "title": title,
            "thumbnail": entry.get("thumbnail"),
            "filename": f"{safe_filename(title)}.mp4",
            "media_count": 1,
            "http_headers": video_format.get("http_headers") or entry.get("http_headers") or {},
        }

    if entry.get("url"):
        item = normalize_media(entry, host)
        if item:
            item["source"] = "facebook"
        return item
    return None


def extract_facebook_public_metadata(source_url):
    """Fallback leve para posts públicos de foto/vídeo do Facebook."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    page = None
    if curl_requests is not None:
        response = curl_requests.get(
            source_url,
            headers=headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        page = response.text
    else:
        with urlopen(Request(source_url, headers=headers), timeout=20) as response:
            page = response.read().decode("utf-8", "ignore")

    if not page:
        return None

    from html import unescape as html_unescape
    import re

    def meta_value(*properties):
        for prop in properties:
            escaped = re.escape(prop)
            patterns = (
                rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
            )
            for pattern in patterns:
                match = re.search(pattern, page, re.IGNORECASE)
                if match:
                    return html_unescape(match.group(1)).replace("&amp;", "&")
        return None

    title = meta_value("og:title", "twitter:title") or "Mídia do Facebook"
    image_url = meta_value("og:image", "twitter:image")
    video_url = meta_value("og:video:url", "og:video:secure_url", "og:video")

    if video_url and video_url.startswith("https://"):
        return {
            "status": "ready",
            "source": "facebook",
            "type": "video",
            "url": video_url,
            "title": title,
            "thumbnail": image_url,
            "filename": f"{safe_filename(title)}.mp4",
            "media_count": 1,
        }

    if image_url and image_url.startswith("https://"):
        path = urlparse(image_url).path.lower()
        extension = "png" if path.endswith(".png") else "webp" if path.endswith(".webp") else "jpg"
        return {
            "status": "ready",
            "source": "facebook",
            "type": "image",
            "url": image_url,
            "title": title,
            "thumbnail": image_url,
            "filename": f"{safe_filename(title)}.{extension}",
            "media_count": 1,
        }
    return None

def _x_status_id(source_url):
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "status" and parts[index + 1].isdigit():
            return parts[index + 1]
    return None


def _x_original_image_url(media_url):
    """Pede a versão original da imagem ao CDN do X quando possível."""
    parsed = urlparse(media_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    extension = Path(parsed.path).suffix.lower().lstrip(".")
    if extension in ("jpg", "jpeg", "png", "webp") and "format" not in query:
        query["format"] = "jpg" if extension == "jpeg" else extension
    query["name"] = "orig"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _x_image_extension(media_url):
    parsed = urlparse(media_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    extension = str(query.get("format") or Path(parsed.path).suffix.lstrip(".") or "jpg").lower()
    return "jpg" if extension == "jpeg" else extension if extension in ("jpg", "png", "webp", "gif") else "jpg"


def _x_best_video_variant(variants):
    candidates = []
    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        content_type = str(variant.get("content_type") or variant.get("type") or "").lower()
        url = variant.get("url") or variant.get("src")
        if not url or "mp4" not in content_type:
            continue
        bitrate = variant.get("bitrate") or 0
        candidates.append((bitrate, url))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def extract_x_syndication(source_url):
    """Extrai mídias públicas de um post do X/Twitter sem login.

    O endpoint é o mesmo usado pelos embeds públicos do X. Ele retorna fotos,
    vídeos e GIFs animados do post, inclusive múltiplas fotos.
    """
    status_id = _x_status_id(source_url)
    if not status_id:
        return None

    endpoint = f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&token=1&lang=pt"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://platform.twitter.com/",
    }

    if curl_requests is not None:
        response = curl_requests.get(
            endpoint,
            headers=headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    else:
        with urlopen(Request(endpoint, headers=headers), timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))

    if not isinstance(data, dict) or data.get("__typename") == "TweetTombstone":
        return None

    title = str(data.get("text") or "Mídia do X").strip() or "Mídia do X"
    media_details = data.get("mediaDetails") if isinstance(data.get("mediaDetails"), list) else []
    items = []

    for index, detail in enumerate(media_details, start=1):
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("type") or "").lower()
        poster = detail.get("media_url_https") or detail.get("media_url")

        if kind == "photo" and poster:
            media_url = _x_original_image_url(poster)
            extension = _x_image_extension(media_url)
            items.append({
                "status": "ready",
                "source": "twitter",
                "type": "image",
                "url": media_url,
                "title": title,
                "thumbnail": media_url,
                "filename": f"x-{status_id}-{index}.{extension}",
                "media_count": 1,
            })
            continue

        if kind in ("video", "animated_gif"):
            video_info = detail.get("video_info") if isinstance(detail.get("video_info"), dict) else {}
            video_url = _x_best_video_variant(video_info.get("variants"))
            if video_url:
                items.append({
                    "status": "ready",
                    "source": "twitter",
                    "type": "video",
                    "url": video_url,
                    "title": title,
                    "thumbnail": poster,
                    "filename": f"x-{status_id}-{index}.mp4",
                    "media_count": 1,
                })

    # Alguns retornos novos do syndication colocam as mídias em campos de alto
    # nível. Usamos esses campos somente se mediaDetails não trouxe nada.
    if not items:
        photos = data.get("photos") if isinstance(data.get("photos"), list) else []
        for index, photo in enumerate(photos, start=1):
            if not isinstance(photo, dict) or not photo.get("url"):
                continue
            media_url = _x_original_image_url(photo["url"])
            extension = _x_image_extension(media_url)
            items.append({
                "status": "ready",
                "source": "twitter",
                "type": "image",
                "url": media_url,
                "title": title,
                "thumbnail": media_url,
                "filename": f"x-{status_id}-{index}.{extension}",
                "media_count": 1,
            })

    if not items and isinstance(data.get("video"), dict):
        video = data["video"]
        video_url = _x_best_video_variant(video.get("variants"))
        if video_url:
            items.append({
                "status": "ready",
                "source": "twitter",
                "type": "video",
                "url": video_url,
                "title": title,
                "thumbnail": video.get("poster"),
                "filename": f"x-{status_id}.mp4",
                "media_count": 1,
            })

    if not items:
        return None

    primary = dict(items[0])
    primary["items"] = items
    primary["media_count"] = len(items)
    return primary



def _pinterest_pin_id(source_url):
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "pin":
            candidate = parts[index + 1].split("-")[0]
            if candidate.isdigit():
                return candidate
    return None


def _pinterest_meta_value(page, *properties):
    from html import unescape as html_unescape
    import re

    for prop in properties:
        escaped = re.escape(prop)
        patterns = (
            rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, page, re.IGNORECASE)
            if match:
                return html_unescape(match.group(1)).replace("&amp;", "&")
    return None


def _pinterest_extension(media_url, default="jpg"):
    extension = Path(urlparse(media_url).path).suffix.lower().lstrip(".")
    if extension == "jpeg":
        return "jpg"
    if extension in ("jpg", "png", "webp", "gif", "avif", "mp4", "webm"):
        return extension
    return default


def _pinterest_best_image(images):
    """Escolhe a maior imagem de um bloco `images` do Pinterest."""
    candidates = []

    def walk(value, label=""):
        if isinstance(value, dict):
            url = value.get("url") or value.get("src")
            if isinstance(url, str) and url.startswith("https://") and "pinimg.com" in (urlparse(url).hostname or "").lower():
                width = value.get("width") or 0
                height = value.get("height") or 0
                try:
                    area = int(width) * int(height)
                except Exception:
                    area = 0
                orig_bonus = 10**12 if str(label).lower() in ("orig", "original", "originals") or "/originals/" in url else 0
                candidates.append((orig_bonus + area, url))
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, key)
        elif isinstance(value, list):
            for child in value:
                walk(child, label)

    walk(images)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _pinterest_best_video(video_node):
    """Escolhe o MP4 progressivo de maior qualidade exposto no Pin."""
    import re

    candidates = []

    def walk(value, label=""):
        if isinstance(value, dict):
            url = value.get("url") or value.get("src")
            if isinstance(url, str) and url.startswith("https://"):
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                path = parsed.path.lower()
                mime = str(value.get("mime_type") or value.get("mimeType") or value.get("content_type") or "").lower()
                if "pinimg.com" in host and (path.endswith(".mp4") or "video/mp4" in mime):
                    width = value.get("width") or 0
                    height = value.get("height") or 0
                    bitrate = value.get("bitrate") or value.get("bit_rate") or 0
                    try:
                        area = int(width) * int(height)
                    except Exception:
                        area = 0
                    try:
                        bitrate = int(bitrate)
                    except Exception:
                        bitrate = 0
                    quality_numbers = [int(item) for item in re.findall(r"\d+", str(label))]
                    quality = max(quality_numbers, default=0)
                    candidates.append(((area, quality, bitrate), url))
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, key)
        elif isinstance(value, list):
            for child in value:
                walk(child, label)

    walk(video_node)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _pinterest_image_from_node(node):
    if not isinstance(node, dict):
        return None
    for key in ("images", "image", "image_spec", "imageSpec", "cover_image", "coverImage"):
        value = node.get(key)
        if isinstance(value, (dict, list)):
            image = _pinterest_best_image(value)
            if image:
                return image
    # Story/Idea Pins colocam a imagem principal dentro de pages -> blocks.
    for child in node.values():
        if isinstance(child, dict):
            image = _pinterest_image_from_node(child)
            if image:
                return image
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    image = _pinterest_image_from_node(item)
                    if image:
                        return image
    return None


def _pinterest_video_from_node(node):
    """Localiza vídeo mesmo quando o Pinterest o aninha em videoData/blocks.

    Alguns Pins de vídeo não expõem `videos` diretamente no objeto principal.
    A capa continua em `images`, enquanto os MP4s ficam em estruturas como
    `storyPinData.pages[].blocks[].videoData.videoList*`. Por isso a busca de
    vídeo precisa ser recursiva, assim como já fazemos com a imagem.
    """
    if not isinstance(node, dict):
        return None

    # Primeiro prioriza os campos conhecidos de vídeo do Pinterest.
    for key in (
        "videos", "video", "video_data", "videoData",
        "video_list", "videoList", "video_urls", "videoUrls",
        "video_list_1080p", "videoList1080P",
        "video_list_720p", "videoList720P",
        "video_list_mobile", "videoListMobile",
    ):
        value = node.get(key)
        if isinstance(value, (dict, list)):
            video = _pinterest_best_video(value)
            if video:
                return video

    # Pins/Idea Pins podem esconder videoData vários níveis abaixo.
    for child in node.values():
        if isinstance(child, dict):
            video = _pinterest_video_from_node(child)
            if video:
                return video
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    video = _pinterest_video_from_node(item)
                    if video:
                        return video

    return None


def _pinterest_find_pin_objects(data, pin_id):
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            object_id = str(value.get("id") or value.get("pin_id") or value.get("pinId") or "")
            richness = sum(1 for key in (
                "images", "videos", "story_pin_data", "storyPinData",
                "carousel_data", "carouselData", "grid_title", "closeup_unified_description",
            ) if key in value)
            if richness:
                score = richness
                if pin_id and object_id == str(pin_id):
                    score += 100
                candidates.append((score, value))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [value for _, value in candidates]


def _pinterest_make_item(kind, media_url, title, pin_id, index, thumbnail=None):
    extension = "mp4" if kind == "video" else _pinterest_extension(media_url, "jpg")
    return {
        "status": "ready",
        "source": "pinterest",
        "type": kind,
        "url": media_url,
        "title": title,
        "thumbnail": thumbnail or (media_url if kind == "image" else None),
        "filename": f"pinterest-{pin_id or 'pin'}-{index}.{extension}",
        "media_count": 1,
    }


def _pinterest_items_from_pin(pin, pin_id, fallback_title):
    if not isinstance(pin, dict):
        return []

    title = str(
        pin.get("grid_title")
        or pin.get("title")
        or pin.get("closeup_unified_description")
        or pin.get("description")
        or fallback_title
        or "Mídia do Pinterest"
    ).strip() or "Mídia do Pinterest"

    # Carrosséis tradicionais usam carousel_data/carousel_slots. Mantemos um
    # item por slot, escolhendo vídeo quando o slot oferece vídeo e imagem.
    carousel = pin.get("carousel_data") or pin.get("carouselData")
    if isinstance(carousel, dict):
        slots = (
            carousel.get("carousel_slots")
            or carousel.get("carouselSlots")
            or carousel.get("slides")
            or carousel.get("items")
        )
        if isinstance(slots, list) and slots:
            items = []
            for index, slot in enumerate(slots, start=1):
                if not isinstance(slot, dict):
                    continue
                poster = _pinterest_image_from_node(slot)
                video = _pinterest_video_from_node(slot)
                if video:
                    items.append(_pinterest_make_item("video", video, title, pin_id, index, poster))
                elif poster:
                    items.append(_pinterest_make_item("image", poster, title, pin_id, index, poster))
            if items:
                return items

    # Idea/Story Pins podem ter várias páginas, cada uma com blocos próprios.
    story = pin.get("story_pin_data") or pin.get("storyPinData")
    if isinstance(story, dict):
        pages = story.get("pages")
        if isinstance(pages, list) and pages:
            items = []
            for index, page in enumerate(pages, start=1):
                if not isinstance(page, dict):
                    continue
                poster = _pinterest_image_from_node(page)
                # Story/Idea Pins costumam esconder o MP4 dentro de
                # blocks[].videoData; a busca precisa ser recursiva aqui
                # também. Usar apenas _pinterest_best_video(page) fazia
                # esses Pins caírem na thumbnail e serem marcados como foto.
                video = _pinterest_video_from_node(page)
                if video:
                    items.append(_pinterest_make_item("video", video, title, pin_id, index, poster))
                elif poster:
                    items.append(_pinterest_make_item("image", poster, title, pin_id, index, poster))
            if items:
                return items

    poster = _pinterest_image_from_node(pin)
    video = _pinterest_video_from_node(pin)
    if video:
        return [_pinterest_make_item("video", video, title, pin_id, 1, poster)]
    if poster:
        return [_pinterest_make_item("image", poster, title, pin_id, 1, poster)]
    return []


def _pinterest_fetch_page(source_url, headers):
    if curl_requests is not None:
        response = curl_requests.get(
            source_url,
            headers=headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        return response.text, str(response.url)
    with urlopen(Request(source_url, headers=headers), timeout=20) as response:
        return response.read().decode("utf-8", "ignore"), response.geturl()


def _pinterest_resource_pin(pin_id, headers):
    """Consulta o recurso público detalhado de um Pin individual."""
    endpoint = "https://www.pinterest.com/resource/PinResource/get/"
    params = {
        "source_url": f"/pin/{pin_id}/",
        "data": json.dumps({
            "options": {"id": str(pin_id), "field_set_key": "detailed"},
            "context": {},
        }, separators=(",", ":")),
    }
    request_headers = dict(headers)
    request_headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Pinterest-PWS-Handler": "www/[username]/pin.js",
        "Referer": f"https://www.pinterest.com/pin/{pin_id}/",
    })

    if curl_requests is not None:
        response = curl_requests.get(
            endpoint,
            params=params,
            headers=request_headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    else:
        request_url = f"{endpoint}?{urlencode(params)}"
        with urlopen(Request(request_url, headers=request_headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))

    resource = payload.get("resource_response") if isinstance(payload, dict) else None
    if not isinstance(resource, dict) or resource.get("status") != "success":
        return None
    data = resource.get("data")
    return data if isinstance(data, dict) else None


def extract_pinterest_public_media(source_url):
    """Extrai imagens e vídeos de Pins públicos sem sessão do usuário.

    O Pinterest às vezes devolve apenas a capa JPG no PinResource mesmo para
    Pins que são vídeo. Por isso vídeo tem prioridade: resultados somente de
    imagem ficam guardados como fallback enquanto também inspecionamos o HTML,
    os JSONs SSR e as URLs MP4 presentes na página.
    """
    from html import unescape as html_unescape
    import re

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    page = None
    final_url = source_url
    pin_id = _pinterest_pin_id(source_url)
    image_fallback = None

    def finalize_items(items):
        unique = []
        urls = set()
        for item in items or []:
            media_url = item.get("url") if isinstance(item, dict) else None
            if not media_url or media_url in urls:
                continue
            urls.add(media_url)
            unique.append(item)
        if not unique:
            return None
        primary = dict(unique[0])
        primary["items"] = unique
        primary["media_count"] = len(unique)
        return primary

    def has_video(result):
        if not isinstance(result, dict):
            return False
        if result.get("type") == "video":
            return True
        return any(
            isinstance(item, dict) and item.get("type") == "video"
            for item in (result.get("items") or [])
        )

    # pin.it não contém o ID. Uma abertura resolve o redirecionamento e já
    # nos dá o HTML, que é importante porque alguns Pins de vídeo aparecem
    # como imagem no PinResource.
    if not pin_id:
        page, final_url = _pinterest_fetch_page(source_url, headers)
        final_host = (urlparse(final_url).hostname or "").lower().removeprefix("www.")
        if not (final_host == "pinterest.com" or final_host.endswith(".pinterest.com")):
            return None
        pin_id = _pinterest_pin_id(final_url)

    # PinResource continua sendo a primeira fonte estruturada, mas um retorno
    # somente com JPG não encerra mais a extração. Guardamos a imagem e ainda
    # procuramos MP4 na página/SSR antes de decidir que o Pin é foto.
    if pin_id:
        try:
            pin = _pinterest_resource_pin(pin_id, headers)
            if pin:
                title = str(
                    pin.get("grid_title")
                    or pin.get("title")
                    or pin.get("closeup_unified_description")
                    or pin.get("description")
                    or "Mídia do Pinterest"
                ).strip() or "Mídia do Pinterest"
                result = finalize_items(_pinterest_items_from_pin(pin, pin_id, title))
                if result:
                    if has_video(result):
                        return result
                    image_fallback = result
        except Exception as error:
            print(f"Pinterest PinResource indisponível: {type(error).__name__}: {error}")

    if page is None:
        page, final_url = _pinterest_fetch_page(source_url, headers)

    if not page:
        return image_fallback

    final_host = (urlparse(final_url).hostname or "").lower().removeprefix("www.")
    if not (final_host == "pinterest.com" or final_host.endswith(".pinterest.com")):
        return image_fallback

    pin_id = pin_id or _pinterest_pin_id(final_url) or _pinterest_pin_id(source_url)
    fallback_title = _pinterest_meta_value(page, "og:title", "twitter:title") or "Mídia do Pinterest"

    # Dados iniciais SSR/Relay enviados ao navegador. Há páginas em que um
    # objeto contém só a capa e outro objeto, mais interno, contém videoData;
    # por isso também priorizamos qualquer resultado que tenha vídeo.
    script_bodies = re.findall(
        r'<script[^>]+(?:type=["\']application/json["\']|id=["\']__PWS_DATA__["\'])[^>]*>(.*?)</script>',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for body in script_bodies:
        payload = None
        for candidate in (body.strip(), html_unescape(body.strip())):
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
                break
            except Exception:
                continue
        if payload is None:
            continue

        for pin in _pinterest_find_pin_objects(payload, pin_id):
            result = finalize_items(_pinterest_items_from_pin(pin, pin_id, fallback_title))
            if not result:
                continue
            if has_video(result):
                return result
            if image_fallback is None:
                image_fallback = result

    # Open Graph cobre vários Pins de vídeo simples.
    image_url = _pinterest_meta_value(page, "og:image", "twitter:image")
    video_url = _pinterest_meta_value(
        page,
        "og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream",
    )

    if video_url and video_url.startswith("https://") and "pinimg.com" in (urlparse(video_url).hostname or "").lower():
        item = _pinterest_make_item("video", video_url, fallback_title, pin_id, 1, image_url)
        item["items"] = [dict(item)]
        item["media_count"] = 1
        return item

    # Último detector de vídeo: o Pinterest também pode embutir o MP4 como
    # string JSON escapada, sem colocá-lo nos metadados OG. Normalizamos as
    # barras escapadas e escolhemos a melhor URL pinimg.com encontrada.
    normalized_page = html_unescape(page).replace("\\u002F", "/").replace("\\/", "/")
    raw_video_urls = re.findall(
        r'https://[^"\'<>\\\s]+?\.mp4(?:\?[^"\'<>\\\s]*)?',
        normalized_page,
        flags=re.IGNORECASE,
    )
    raw_video_urls = [
        url for url in raw_video_urls
        if "pinimg.com" in (urlparse(url).hostname or "").lower()
    ]
    if raw_video_urls:
        # URLs 1080p/720p costumam carregar a qualidade no próprio caminho.
        def raw_video_score(url):
            lower = url.lower()
            if "1080" in lower:
                return 3
            if "720" in lower:
                return 2
            if "540" in lower or "480" in lower:
                return 1
            return 0

        raw_video_urls.sort(key=raw_video_score, reverse=True)
        item = _pinterest_make_item("video", raw_video_urls[0], fallback_title, pin_id, 1, image_url)
        item["items"] = [dict(item)]
        item["media_count"] = 1
        return item

    # Só agora, depois de esgotar as fontes de vídeo, aceitamos a capa como
    # foto. Isso evita classificar um Pin de vídeo como imagem apenas porque o
    # PinResource entregou primeiro o JPG de poster.
    if image_fallback is not None:
        return image_fallback

    if image_url and image_url.startswith("https://") and "pinimg.com" in (urlparse(image_url).hostname or "").lower():
        item = _pinterest_make_item("image", image_url, fallback_title, pin_id, 1, image_url)
        item["items"] = [dict(item)]
        item["media_count"] = 1
        return item

    return None


def normalize_carousel_media(info, host, sanitized_info=None):
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
    safe_items = []
    if sanitized_info:
        safe_items = sanitized_info.get("entries") if sanitized_info.get("entries") else [sanitized_info]

    items = []
    for index, entry in enumerate(raw_items, start=1):
        if not entry:
            continue
        if "instagram" in host:
            item = normalize_instagram_entry(entry, host, index)
            if item and item.get("type") == "video":
                safe_entry = safe_items[index - 1] if index - 1 < len(safe_items) else None
                if safe_entry:
                    item["_instagram_info"] = safe_entry
                item["_instagram_index"] = index
        elif "facebook" in host or host == "fb.watch":
            item = normalize_facebook_entry(entry, host, index)
            if item and item.get("type") == "video":
                safe_entry = safe_items[index - 1] if index - 1 < len(safe_items) else None
                if safe_entry:
                    item["_facebook_info"] = safe_entry
                item["_facebook_index"] = index
        elif entry.get("url"):
            item = normalize_media(entry, host)
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
                "twitter" if "twimg" in host else
                "pinterest" if "pinimg" in host else
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

        if source == "youtube" and cached:
            return self.proxy_youtube_with_ytdlp(cached, download_requested)

        # A prévia do Instagram continua usando a URL direta para ser rápida.
        # No download final de vídeo, usamos o yt-dlp + FFmpeg para preferir um
        # MP4 que já tenha áudio ou juntar vídeo + áudio quando vierem separados.
        if (
            source == "instagram"
            and cached
            and download_requested
            and cached.get("media_type") == "video"
        ):
            return self.proxy_instagram_with_ytdlp(cached)

        # Facebook também pode entregar vídeo e áudio em faixas separadas.
        # No download final preparamos um MP4 completo para evitar arquivo sem
        # som ou incompatível com a Galeria do Android.
        if (
            source == "facebook"
            and cached
            and download_requested
            and cached.get("media_type") == "video"
        ):
            return self.proxy_facebook_with_ytdlp(cached)

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
                download_filename = cached.get("filename") if cached else None
                forced_type = download_content_type(download_filename) if download_requested else None
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if name == "Content-Type" and forced_type:
                        continue
                    value = upstream.headers.get(name)
                    if value:
                        self.send_header(name, value)
                if forced_type:
                    self.send_header("Content-Type", forced_type)
                if download_requested:
                    self.send_header("Content-Disposition", content_disposition(download_filename))
                    self.send_header("X-Content-Type-Options", "nosniff")
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
                download_filename = cached.get("filename") if cached else None
                forced_type = download_content_type(download_filename) if download_requested else None
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if name == "Content-Type" and forced_type:
                        continue
                    if upstream.headers.get(name):
                        self.send_header(name, upstream.headers[name])
                if forced_type:
                    self.send_header("Content-Type", forced_type)
                if download_requested:
                    self.send_header("Content-Disposition", content_disposition(download_filename))
                    self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
        except Exception as error:
            print(f"Falha no proxy {source or 'desconhecido'} via urllib: {type(error).__name__}: {error}")
            self.respond(502, {"error": "Não foi possível preparar este arquivo."})

    def proxy_instagram_with_ytdlp(self, cached):
        page_url = cached.get("page_url")
        info_path = cached.get("info_path")
        item_index = cached.get("item_index") or 1
        filename = cached.get("filename") or "instagram-video.mp4"
        filename = str(Path(filename).with_suffix(".mp4"))

        if not page_url and not (info_path and Path(info_path).is_file()):
            return self.respond(410, {"error": "Este link expirou. Analise a publicação novamente."})

        # Preferimos MP4 completo quando o Instagram oferece uma faixa
        # progressiva. Caso vídeo e áudio venham separados, o FFmpeg faz a
        # junção para entregar um MP4 padrão (H.264/AAC quando disponível).
        format_selector = (
            "best[ext=mp4][vcodec!=none][acodec!=none]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best"
        )

        with tempfile.TemporaryDirectory(prefix="soft-instagram-") as temp_dir:
            output_template = str(Path(temp_dir) / "download.%(ext)s")

            def run_download(use_info_json):
                command = [
                    sys.executable, "-m", "yt_dlp",
                    "--quiet", "--no-warnings", "--no-progress",
                    "-f", format_selector,
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                ]
                if use_info_json and info_path and Path(info_path).is_file():
                    command.extend(["--load-info-json", info_path])
                elif page_url:
                    # Em carrosséis, baixa somente o item cujo botão foi usado.
                    command.extend(["--playlist-items", str(item_index), page_url])
                else:
                    return None
                return subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )

            result = run_download(True)
            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                if detail:
                    print(f"Falha no download Instagram via info-json: {detail[-1600:]}")
                # URLs temporárias do info-json podem expirar. Fazemos só uma
                # extração nova da publicação como fallback.
                result = run_download(False)

            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                print(f"Falha no download Instagram via yt-dlp: {detail[-1600:]}")
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Instagram."})

            files = [
                path for path in Path(temp_dir).iterdir()
                if path.is_file() and path.suffix.lower() in (".mp4", ".m4v", ".mov")
            ]
            if not files:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Instagram."})
            media_file = max(files, key=lambda path: path.stat().st_size)

            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(media_file.stat().st_size))
            self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            try:
                with media_file.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def proxy_facebook_with_ytdlp(self, cached):
        page_url = cached.get("page_url")
        info_path = cached.get("info_path")
        item_index = cached.get("item_index") or 1
        filename = cached.get("filename") or "facebook-video.mp4"
        filename = str(Path(filename).with_suffix(".mp4"))

        if not page_url and not (info_path and Path(info_path).is_file()):
            return self.respond(410, {"error": "Este link expirou. Analise a publicação novamente."})

        format_selector = (
            "best[ext=mp4][vcodec!=none][acodec!=none]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best"
        )

        with tempfile.TemporaryDirectory(prefix="soft-facebook-") as temp_dir:
            output_template = str(Path(temp_dir) / "download.%(ext)s")

            def run_download(use_info_json):
                command = [
                    sys.executable, "-m", "yt_dlp",
                    "--quiet", "--no-warnings", "--no-progress",
                    "-f", format_selector,
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                ]
                if use_info_json and info_path and Path(info_path).is_file():
                    command.extend(["--load-info-json", info_path])
                elif page_url:
                    command.extend(["--playlist-items", str(item_index), page_url])
                else:
                    return None
                return subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )

            result = run_download(True)
            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                if detail:
                    print(f"Falha no download Facebook via info-json: {detail[-1600:]}")
                result = run_download(False)

            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                print(f"Falha no download Facebook via yt-dlp: {detail[-1600:]}")
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Facebook."})

            files = [
                path for path in Path(temp_dir).iterdir()
                if path.is_file() and path.suffix.lower() in (".mp4", ".m4v", ".mov")
            ]
            if not files:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Facebook."})

            final_path = max(files, key=lambda path: path.stat().st_size)
            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(final_path.stat().st_size))
            self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with final_path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    self.wfile.write(chunk)

    def proxy_youtube_with_ytdlp(self, cached, download_requested=False):
        page_url = cached.get("page_url") or cached.get("url")
        info_path = cached.get("info_path")
        filename = cached.get("filename") or "youtube-video.mp4"

        if not page_url and not (info_path and Path(info_path).is_file()):
            return self.respond(410, {"error": "Este link expirou. Analise o vídeo novamente."})

        if not download_requested:
            # A prévia não precisa de áudio porque o player fica mutado. Usamos
            # uma faixa MP4/H.264 leve (até 480p quando disponível) e enviamos
            # direto ao navegador, sem FFmpeg e sem preparar o download final.
            preview_selector = (
                "best[ext=mp4][vcodec^=avc1][height<=480]/"
                "bestvideo[ext=mp4][vcodec^=avc1][height<=480]/"
                "bestvideo[ext=mp4][height<=480]/bestvideo[height<=480]/"
                "best[ext=mp4]/best"
            )
            command = [
                sys.executable, "-m", "yt_dlp",
                "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
                "-f", preview_selector,
                "-o", "-",
            ]
            if info_path and Path(info_path).is_file():
                command.extend(["--load-info-json", info_path])
            elif page_url:
                command.append(page_url)

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
                        print(f"Falha na prévia YouTube via yt-dlp ({return_code}): {detail[-1600:]}")
                        return self.respond(502, {"error": "Não foi possível preparar a prévia deste vídeo do YouTube."})

                    self.send_response(200)
                    origin = self.headers.get("Origin")
                    if origin in ALLOWED_ORIGINS:
                        self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Content-Type", "video/mp4")
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
                        print(f"Prévia YouTube interrompida pelo yt-dlp ({return_code}): {detail[-1600:]}")
                except (BrokenPipeError, ConnectionResetError):
                    if process and process.poll() is None:
                        process.terminate()
                except Exception as error:
                    if process and process.poll() is None:
                        process.terminate()
                    print(f"Falha no streaming da prévia YouTube: {type(error).__name__}: {error}")
                    if not self.wfile.closed:
                        try:
                            self.respond(502, {"error": "Não foi possível preparar a prévia deste vídeo do YouTube."})
                        except Exception:
                            pass
                finally:
                    if process and process.poll() is None:
                        process.kill()
            return

        format_selector = (
            "bestvideo[ext=mp4][vcodec^=avc1][height<=720]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/"
            "bestvideo[height<=720]+bestaudio/best[ext=mp4]/best"
        )

        with tempfile.TemporaryDirectory(prefix="soft-youtube-") as temp_dir:
            output_template = str(Path(temp_dir) / "download.%(ext)s")

            def run_download(use_info_json):
                command = [
                    sys.executable, "-m", "yt_dlp",
                    "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
                    "-f", format_selector,
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                ]
                if use_info_json and info_path and Path(info_path).is_file():
                    command.extend(["--load-info-json", info_path])
                elif page_url:
                    command.append(page_url)
                else:
                    return None
                return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

            result = run_download(True)
            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                if detail:
                    print(f"Falha no download YouTube via info-json: {detail[-1600:]}")
                # URLs do info-json podem expirar. Fazemos uma única nova
                # extração da página como fallback.
                result = run_download(False)

            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                print(f"Falha no download YouTube via yt-dlp: {detail[-1600:]}")
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do YouTube."})

            files = [path for path in Path(temp_dir).iterdir() if path.is_file()]
            if not files:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do YouTube."})
            media_file = max(files, key=lambda path: path.stat().st_size)

            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Type", "video/mp4" if media_file.suffix.lower() == ".mp4" else "application/octet-stream")
            self.send_header("Content-Length", str(media_file.stat().st_size))
            self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            try:
                with media_file.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
