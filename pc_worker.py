"""Worker local do SOFT Downloaders.

Roda no PC e só fica disponível na rede local até um túnel seguro ser ligado.
Não armazena links, contas ou arquivos de quem usa o site.
"""

import json
import hmac
import os
import re
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
    "redd.it", "redditmedia.com",
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
        "reddit": "https://www.reddit.com/",
        "kwai": "https://www.kwai.com/",
        "vimeo": "https://vimeo.com/",
        "dailymotion": "https://www.dailymotion.com/",
        "soundcloud": "https://soundcloud.com/",
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
    vimeo_info=None,
    dailymotion_info=None,
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
        vimeo_info if source == "vimeo" else
        dailymotion_info if source == "dailymotion" else
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
    vimeo_info = media.pop("_vimeo_info", None)
    dailymotion_info = media.pop("_dailymotion_info", None)
    facebook_info = media.pop("_facebook_info", None)
    items = media.get("items") if isinstance(media.get("items"), list) else []
    tiktok_cache_used = False
    youtube_cache_used = False
    vimeo_cache_used = False
    dailymotion_cache_used = False

    for item_index, item in enumerate(items, start=1):
        if not item or not item.get("url"):
            continue
        item_source = item.get("source") or media.get("source")
        use_tiktok_bundle = item_source == "tiktok" and not tiktok_cache_used
        use_youtube_bundle = item_source == "youtube" and not youtube_cache_used
        use_vimeo_bundle = item_source == "vimeo" and not vimeo_cache_used
        use_dailymotion_bundle = item_source == "dailymotion" and not dailymotion_cache_used
        instagram_info = item.pop("_instagram_info", None) if item_source == "instagram" else None
        instagram_index = item.pop("_instagram_index", item_index) if item_source == "instagram" else None
        item_facebook_info = item.pop("_facebook_info", None) if item_source == "facebook" else None
        facebook_index = item.pop("_facebook_index", item_index) if item_source == "facebook" else None
        item["proxy_id"] = cache_media(
            item["url"],
            item.get("http_headers"),
            item_source,
            page_url=source_url if item_source in ("tiktok", "youtube", "instagram", "facebook", "twitter", "pinterest", "reddit", "kwai", "vimeo", "dailymotion", "soundcloud") else None,
            filename=item.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
            youtube_info=youtube_info if use_youtube_bundle else None,
            vimeo_info=vimeo_info if use_vimeo_bundle else None,
            dailymotion_info=dailymotion_info if use_dailymotion_bundle else None,
            instagram_info=instagram_info,
            facebook_info=item_facebook_info,
            item_index=facebook_index if item_source == "facebook" else instagram_index,
            media_type=item.get("type"),
        )
        if use_tiktok_bundle:
            tiktok_cache_used = True
        if use_youtube_bundle:
            youtube_cache_used = True
        if use_vimeo_bundle:
            vimeo_cache_used = True
        if use_dailymotion_bundle:
            dailymotion_cache_used = True
        item.pop("http_headers", None)

    if items and media.get("url") == items[0].get("url"):
        media["proxy_id"] = items[0].get("proxy_id")
    elif media.get("url"):
        media_source = media.get("source")
        use_tiktok_bundle = media_source == "tiktok" and not tiktok_cache_used
        use_youtube_bundle = media_source == "youtube" and not youtube_cache_used
        use_vimeo_bundle = media_source == "vimeo" and not vimeo_cache_used
        use_dailymotion_bundle = media_source == "dailymotion" and not dailymotion_cache_used
        media["proxy_id"] = cache_media(
            media["url"],
            media.get("http_headers"),
            media_source,
            page_url=source_url if media_source in ("tiktok", "youtube", "instagram", "facebook", "twitter", "pinterest", "reddit", "kwai", "vimeo", "dailymotion", "soundcloud") else None,
            filename=media.get("filename"),
            tiktok_info=tiktok_info if use_tiktok_bundle else None,
            tiktok_cookiefile=tiktok_cookiefile if use_tiktok_bundle else None,
            youtube_info=youtube_info if use_youtube_bundle else None,
            vimeo_info=vimeo_info if use_vimeo_bundle else None,
            dailymotion_info=dailymotion_info if use_dailymotion_bundle else None,
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
        if use_vimeo_bundle:
            vimeo_cache_used = True
        if use_dailymotion_bundle:
            dailymotion_cache_used = True

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


def extract_dailymotion_cli(source_url):
    """Extrai Dailymotion pelo mesmo caminho CLI validado no runtime local.

    O extrator do Dailymotion pode precisar refazer a leitura do HLS com
    impersonação de navegador. O CLI do yt-dlp já executa esse fallback
    automaticamente; usando o mesmo Python do worker mantemos exatamente o
    comportamento que foi validado manualmente com `yt-dlp -F`.
    """
    command = [
        sys.executable, "-m", "yt_dlp",
        "--dump-single-json", "--skip-download",
        "--no-warnings", "--no-playlist",
        source_url,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        print("Falha na análise do Dailymotion: tempo limite excedido")
        return None

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if detail:
            print(f"Falha na análise do Dailymotion via CLI: {detail[-1600:]}")
        return None

    try:
        return json.loads(result.stdout)
    except Exception as error:
        print(f"Falha ao interpretar Dailymotion: {type(error).__name__}: {error}")
        return None


def resolve_dailymotion_stream_cli(source_url, selector="best[height<=720]/best"):
    """Resolve rapidamente a faixa HLS selecionada e os headers do Dailymotion.

    O download final usa essa URL apenas para iniciar um fluxo FFmpeg -> navegador;
    assim o navegador começa a receber bytes sem esperar o vídeo inteiro ser
    baixado em arquivo temporário primeiro.
    """
    command = [
        sys.executable, "-m", "yt_dlp",
        "--dump-single-json", "--skip-download",
        "--no-warnings", "--no-playlist",
        "--impersonate", "chrome",
        "-f", selector,
        source_url,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        print("Falha ao resolver stream Dailymotion: tempo limite excedido")
        return None, {}

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if detail:
            print(f"Falha ao resolver stream Dailymotion: {detail[-1600:]}")
        return None, {}

    try:
        info = json.loads(result.stdout)
    except Exception as error:
        print(f"Falha ao interpretar stream Dailymotion: {type(error).__name__}: {error}")
        return None, {}

    if isinstance(info, dict) and isinstance(info.get("entries"), list):
        info = next((entry for entry in info["entries"] if isinstance(entry, dict)), info)

    media_url = info.get("url") if isinstance(info, dict) else None
    headers = clean_proxy_headers(info.get("http_headers") or {}) if isinstance(info, dict) else {}

    # Alguns extratores deixam a seleção dentro de requested_formats.
    if not media_url and isinstance(info, dict):
        requested = info.get("requested_formats") or []
        if len(requested) == 1 and isinstance(requested[0], dict):
            media_url = requested[0].get("url")
            headers = clean_proxy_headers(requested[0].get("http_headers") or headers)

    return media_url, headers


def extract_media(source_url):
    parsed = urlparse(source_url)
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if parsed.scheme not in ("http", "https") or not any(
        host == item or host.endswith("." + item) for item in ALLOWED_HOSTS
    ):
        return None, "Use um link público de Instagram, TikTok, YouTube, Facebook, X/Twitter, Pinterest, Reddit, Kwai, Vimeo, Dailymotion ou SoundCloud."

    is_tiktok = "tiktok" in host
    is_youtube = "youtube" in host or host == "youtu.be"
    is_instagram = "instagram" in host
    is_facebook = "facebook" in host or host == "fb.watch"
    is_twitter = host == "x.com" or host.endswith(".x.com") or "twitter.com" in host
    is_pinterest = host == "pin.it" or host == "pinterest.com" or host.endswith(".pinterest.com")
    is_reddit = host == "redd.it" or host == "reddit.com" or host.endswith(".reddit.com")
    is_kwai = (
        host == "kwai.com" or host.endswith(".kwai.com")
        or host == "kwai-video.com" or host.endswith(".kwai-video.com")
        or host == "kw.ai" or host.endswith(".kw.ai")
    )
    is_vimeo = host == "vimeo.com" or host.endswith(".vimeo.com")
    is_dailymotion = host == "dailymotion.com" or host.endswith(".dailymotion.com") or host == "dai.ly"
    is_soundcloud = host == "soundcloud.com" or host.endswith(".soundcloud.com")
    tiktok_cookiefile = None
    tiktok_info = None
    youtube_info = None
    vimeo_info = None
    dailymotion_info = None
    facebook_info = None

    # Kwai usa links curtos com redirecionamento e expõe a mídia pública
    # em metadados/JSON da própria página. Tentamos esse caminho antes do
    # extrator genérico para preservar links compartilhados pelo app.
    if is_kwai:
        try:
            kwai_media = extract_kwai_public_media(source_url)
            if kwai_media:
                return kwai_media, None
        except Exception as error:
            print(f"Falha no extrator público do Kwai: {type(error).__name__}: {error}")

    # Reddit expõe metadados públicos em JSON para posts acessíveis sem login.
    # Usamos essa fonte primeiro porque ela preserva galerias de imagens e
    # também informa a URL progressiva de vídeos hospedados em v.redd.it.
    if is_reddit:
        try:
            reddit_media = extract_reddit_public_media(source_url)
            if reddit_media:
                return reddit_media, None
        except Exception as error:
            print(f"Falha no extrator público do Reddit: {type(error).__name__}: {error}")

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

    if is_dailymotion:
        # Usa o mesmo fluxo CLI que foi validado diretamente no runtime.
        # O CLI consegue refazer a leitura do m3u8 com impersonação quando o
        # Dailymotion rejeita a primeira tentativa, enquanto ignoreerrors da
        # API Python podia apenas devolver None e terminar em 422 sem log.
        info = extract_dailymotion_cli(source_url)
        dailymotion_info = info
        media = normalize_carousel_media(info, host) if info else None
    elif is_tiktok and curl_requests is not None:
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
                if is_vimeo and info:
                    vimeo_info = extractor.sanitize_info(info)
                if is_dailymotion and info:
                    dailymotion_info = extractor.sanitize_info(info)
                instagram_info = extractor.sanitize_info(info) if is_instagram and info else None
                facebook_info = extractor.sanitize_info(info) if is_facebook and info else None
            media = normalize_carousel_media(
                info, host, instagram_info if is_instagram else facebook_info
            )
        except Exception as error:
            if is_youtube:
                print(f"Falha na análise do YouTube: {type(error).__name__}: {error}")
            elif is_vimeo:
                print(f"Falha na análise do Vimeo: {type(error).__name__}: {error}")
            elif is_dailymotion:
                print(f"Falha na análise do Dailymotion: {type(error).__name__}: {error}")
            elif is_soundcloud:
                print(f"Falha na análise do SoundCloud: {type(error).__name__}: {error}")
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

    if not media and is_reddit:
        return None, "Não foi possível ler esta publicação do Reddit agora. Confirme se ela é pública e tente novamente."

    if not media and is_kwai:
        return None, "Não foi possível ler esta publicação do Kwai agora. Confirme se ela é pública e tente novamente."

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
    if is_vimeo:
        media["_vimeo_info"] = vimeo_info
    if is_dailymotion:
        media["_dailymotion_info"] = dailymotion_info
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


def normalize_vimeo_media(info, host):
    """Normaliza um vídeo público do Vimeo para uma prévia MP4 progressiva.

    A thumbnail nunca é tratada como a mídia principal. Preferimos uma faixa
    MP4/H.264 com áudio embutido; se ela não existir, usamos uma faixa MP4 de
    vídeo apenas para a prévia. O download final é preparado pelo yt-dlp.
    """
    if not info:
        return None

    raw_items = info.get("entries") if info.get("entries") else [info]
    entry = next((item for item in raw_items if item), None)
    if not entry:
        return None

    candidates = []
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        media_url = fmt.get("url")
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            continue
        ext = str(fmt.get("ext") or "").lower()
        vcodec = str(fmt.get("vcodec") or "none").lower()
        acodec = str(fmt.get("acodec") or "none").lower()
        protocol = str(fmt.get("protocol") or "").lower()
        if ext != "mp4" or vcodec == "none":
            continue

        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or 0
        progressive = protocol in ("http", "https") or protocol.startswith("http")
        combined = acodec != "none"
        h264 = vcodec.startswith(("avc", "h264"))
        preview_size = 1 if height and height <= 720 else 0
        score = (
            1 if progressive else 0,
            1 if combined else 0,
            1 if h264 else 0,
            preview_size,
            height,
            tbr,
        )
        candidates.append((score, fmt))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[0][1]
        media_url = selected.get("url")
        headers = selected.get("http_headers") or entry.get("http_headers") or {}
    else:
        media_url = entry.get("url")
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            return None
        headers = entry.get("http_headers") or {}

    title = entry.get("title") or info.get("title") or "Vídeo do Vimeo"
    video_id = entry.get("id") or info.get("id") or "video"
    return {
        "status": "ready",
        "source": "vimeo",
        "type": "video",
        "url": media_url,
        "title": title,
        "thumbnail": entry.get("thumbnail") or info.get("thumbnail"),
        "filename": f"vimeo-{video_id}.mp4",
        "media_count": 1,
        "http_headers": headers,
    }


def normalize_dailymotion_media(info, host):
    """Normaliza um vídeo público do Dailymotion.

    O Dailymotion normalmente expõe HLS com H.264/AAC. A URL escolhida aqui
    serve apenas como referência/cache; prévia e download final são preparados
    pelo yt-dlp no worker para o navegador receber um MP4 compatível.
    """
    if not info:
        return None

    raw_items = info.get("entries") if info.get("entries") else [info]
    entry = next((item for item in raw_items if item), None)
    if not entry:
        return None

    candidates = []
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        media_url = fmt.get("url")
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            continue
        vcodec = str(fmt.get("vcodec") or "none").lower()
        if vcodec == "none":
            continue
        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or 0
        # Para a referência de prévia, priorizamos uma faixa até 480p.
        preferred = 1 if height and height <= 480 else 0
        candidates.append(((preferred, height if preferred else -height, tbr), fmt))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[0][1]
        media_url = selected.get("url")
        headers = selected.get("http_headers") or entry.get("http_headers") or {}
    else:
        media_url = entry.get("url")
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            return None
        headers = entry.get("http_headers") or {}

    title = entry.get("title") or info.get("title") or "Vídeo do Dailymotion"
    video_id = entry.get("id") or info.get("id") or "video"
    return {
        "status": "ready",
        "source": "dailymotion",
        "type": "video",
        "url": media_url,
        "title": title,
        "thumbnail": entry.get("thumbnail") or info.get("thumbnail"),
        "filename": f"dailymotion-{video_id}.mp4",
        "media_count": 1,
        "http_headers": headers,
    }



def normalize_soundcloud_media(info, host):
    """Normaliza uma faixa pública do SoundCloud como áudio baixável.

    Priorizamos MP3 progressivo por HTTP porque é o formato mais compatível
    com navegadores e celulares e evita depender de uma playlist HLS apenas
    para tocar a prévia ou iniciar o download.
    """
    if not info:
        return None

    entries = info.get("entries") if isinstance(info, dict) and info.get("entries") else [info]
    entry = next((item for item in entries if isinstance(item, dict)), None)
    if not entry:
        return None

    formats = [fmt for fmt in (entry.get("formats") or []) if isinstance(fmt, dict) and fmt.get("url")]

    def score(fmt):
        ext = str(fmt.get("ext") or "").lower()
        protocol = str(fmt.get("protocol") or "").lower()
        format_id = str(fmt.get("format_id") or "").lower()
        vcodec = str(fmt.get("vcodec") or "none").lower()
        acodec = str(fmt.get("acodec") or "none").lower()
        audio_only = vcodec == "none" and acodec != "none"
        if not audio_only:
            return (-1, 0)
        if ext == "mp3" and (protocol in ("http", "https") or format_id.startswith("http_mp3")):
            base = 4
        elif ext == "mp3":
            base = 3
        elif ext in ("m4a", "aac"):
            base = 2
        else:
            base = 1
        bitrate = fmt.get("abr") or fmt.get("tbr") or 0
        return (base, float(bitrate or 0))

    candidates = [fmt for fmt in formats if score(fmt)[0] >= 0]
    selected = max(candidates, key=score) if candidates else None

    media_url = selected.get("url") if selected else entry.get("url")
    if not media_url:
        return None

    extension = str((selected or entry).get("ext") or "mp3").lower()
    if extension not in ("mp3", "m4a", "aac", "ogg", "opus", "wav"):
        extension = "mp3"

    track_title = str(entry.get("track") or entry.get("title") or info.get("title") or "Áudio do SoundCloud").strip()
    artist = str(entry.get("uploader") or entry.get("artist") or entry.get("creator") or "").strip()
    display_title = f"{artist} - {track_title}" if artist and artist.lower() not in track_title.lower() else track_title
    headers = (selected or {}).get("http_headers") or entry.get("http_headers") or info.get("http_headers") or {}

    duration = entry.get("duration") or info.get("duration")

    return {
        "status": "ready",
        "source": "soundcloud",
        "type": "audio",
        "url": media_url,
        "title": display_title,
        "track_title": track_title,
        "artist": artist,
        "duration": duration,
        "thumbnail": entry.get("thumbnail") or info.get("thumbnail"),
        "filename": f"{safe_filename(display_title)}.{extension}",
        "media_count": 1,
        "http_headers": headers,
    }

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



def _kwai_clean_url(value):
    if not value:
        return None
    from html import unescape as html_unescape
    value = html_unescape(str(value)).strip().strip('"\'')
    value = value.replace("\\u0026", "&").replace("\\u003d", "=")
    value = value.replace("\\/", "/").replace("&amp;", "&")
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith("https://"):
        return None
    return value


def _kwai_meta_value(page, *properties):
    import re
    from html import unescape as html_unescape
    for prop in properties:
        escaped = re.escape(prop)
        patterns = (
            rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, page, re.IGNORECASE)
            if match:
                return _kwai_clean_url(match.group(1)) if "video" in prop or "image" in prop else html_unescape(match.group(1)).strip()
    return None


def _kwai_fetch_page(source_url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
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
        return response.text, str(response.url), headers

    with urlopen(Request(source_url, headers=headers), timeout=20) as response:
        return response.read().decode("utf-8", "ignore"), response.geturl(), headers


def _kwai_media_extension(media_url, default):
    path = urlparse(media_url or "").path.lower()
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"mp4", "mov", "m4v", "webm", "jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if suffix == "jpeg" else suffix
    return default


def _kwai_collect_json_media(node, videos, images, titles, parent_key=""):
    video_parent_keys = {
        "mainmvurls", "playurls", "playurl", "videourls", "videourl",
        "srcnomark", "srcnowatermark", "photourl", "manifest",
    }
    image_parent_keys = {
        "images", "imageurls", "imageurl", "photourls", "coverurls",
        "coverurl", "poster", "thumbnail", "thumbnails",
    }
    title_keys = {"caption", "title", "description", "desc", "content"}

    if isinstance(node, dict):
        for key, value in node.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in title_keys and isinstance(value, str) and value.strip():
                titles.append(value.strip())
            next_parent = normalized
            if normalized in {"url", "urls", "src"} and parent_key in video_parent_keys.union(image_parent_keys):
                next_parent = parent_key
            _kwai_collect_json_media(value, videos, images, titles, next_parent)
        return

    if isinstance(node, list):
        for value in node:
            _kwai_collect_json_media(value, videos, images, titles, parent_key)
        return

    if not isinstance(node, str):
        return

    media_url = _kwai_clean_url(node)
    if not media_url:
        return

    lower = media_url.lower()
    is_image_url = any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))
    if (parent_key in video_parent_keys or ".mp4" in lower or ".m3u8" in lower) and not is_image_url:
        # URLs de vídeo do Kwai muitas vezes são assinadas e não terminam em .mp4.
        # O contexto do campo (mainMvUrls/playUrl/etc.) é mais confiável que a extensão.
        videos.append(media_url)
    elif parent_key in image_parent_keys or is_image_url:
        images.append(media_url)


def extract_kwai_public_media(source_url):
    """Extrai mídia pública do Kwai, incluindo links curtos compartilhados pelo app."""
    import re

    page, resolved_url, request_headers = _kwai_fetch_page(source_url)
    if not page:
        return None

    title = (
        _kwai_meta_value(page, "og:title", "twitter:title")
        or "Mídia do Kwai"
    )
    thumbnail = _kwai_meta_value(page, "og:image", "twitter:image")
    meta_video = _kwai_meta_value(
        page,
        "og:video:secure_url",
        "og:video:url",
        "og:video",
        "twitter:player:stream",
    )

    videos = []
    images = []
    titles = []

    if meta_video:
        videos.append(meta_video)

    # JSONs completos usados por páginas SSR/Next.
    script_patterns = (
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    )
    for pattern in script_patterns:
        for raw in re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            _kwai_collect_json_media(payload, videos, images, titles)

    # Alguns builds inserem mainMvUrls e URLs assinadas em JavaScript, não em JSON puro.
    # Procuramos primeiro dentro de janelas próximas de campos claramente de vídeo.
    for match in re.finditer(
        r'(?i)(mainMvUrls|playUrl|playUrls|videoUrl|videoUrls|srcNoMark|srcNoWatermark)',
        page,
    ):
        window = page[match.start():match.start() + 6000]
        for raw_url in re.findall(r'https?:\\?/\\?/[^"\'<>\s]+', window):
            candidate = _kwai_clean_url(raw_url)
            if candidate and not any(ext in candidate.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                videos.append(candidate)

    # Fallback seguro para MP4 explícito no HTML.
    for raw_url in re.findall(r'https?:\\?/\\?/[^"\'<>\s]+?\.mp4(?:\?[^"\'<>\s]*)?', page, re.IGNORECASE):
        candidate = _kwai_clean_url(raw_url)
        if candidate:
            videos.append(candidate)

    # Imagens explícitas em estruturas de photo/image; não usamos og:image sozinho
    # como mídia principal porque em posts de vídeo ele é apenas a capa.
    for match in re.finditer(r'(?i)(imageUrls|photoUrls|images)', page):
        window = page[match.start():match.start() + 5000]
        for raw_url in re.findall(r'https?:\\?/\\?/[^"\'<>\s]+', window):
            candidate = _kwai_clean_url(raw_url)
            if candidate and any(ext in candidate.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                images.append(candidate)

    def unique(values):
        seen = set()
        result = []
        for value in values:
            clean = _kwai_clean_url(value)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    videos = unique(videos)
    images = unique(images)
    if titles and (not title or title == "Mídia do Kwai"):
        title = titles[0][:180]

    # Para vídeo, priorizamos sempre a mídia em vez da capa.
    if videos:
        video_url = videos[0]
        extension = _kwai_media_extension(video_url, "mp4")
        if extension not in {"mp4", "mov", "m4v", "webm"}:
            extension = "mp4"
        item = {
            "status": "ready",
            "source": "kwai",
            "type": "video",
            "url": video_url,
            "title": title,
            "thumbnail": thumbnail or (images[0] if images else None),
            "filename": f"{safe_filename(title)}.{extension}",
            "media_count": 1,
            "http_headers": {
                "User-Agent": request_headers["User-Agent"],
                "Referer": resolved_url or "https://www.kwai.com/",
            },
        }
        primary = dict(item)
        primary["items"] = [item]
        return primary

    # Photo posts: só retornamos URLs encontradas em campos explícitos de imagem.
    if images:
        items = []
        for index, image_url in enumerate(images[:20], start=1):
            extension = _kwai_media_extension(image_url, "jpg")
            item_title = title if len(images) == 1 else f"{title} {index}"
            items.append({
                "status": "ready",
                "source": "kwai",
                "type": "image",
                "url": image_url,
                "title": item_title,
                "thumbnail": image_url,
                "filename": f"{safe_filename(item_title)}.{extension}",
                "media_count": 1,
                "http_headers": {
                    "User-Agent": request_headers["User-Agent"],
                    "Referer": resolved_url or "https://www.kwai.com/",
                },
            })
        primary = dict(items[0])
        primary["items"] = items
        primary["media_count"] = len(items)
        primary["title"] = title
        return primary

    return None


def _reddit_post_id(source_url):
    import re

    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]

    if host == "redd.it" and parts:
        candidate = parts[0]
        if re.fullmatch(r"[A-Za-z0-9]+", candidate):
            return candidate

    for index, part in enumerate(parts[:-1]):
        if part.lower() in ("comments", "gallery"):
            candidate = parts[index + 1]
            if re.fullmatch(r"[A-Za-z0-9]+", candidate):
                return candidate
    return None


def _reddit_clean_url(value):
    from html import unescape as html_unescape

    if not isinstance(value, str):
        return None
    value = html_unescape(value).replace("&amp;", "&").strip()
    return value if value.startswith("https://") else None


def _reddit_allowed_media_url(media_url):
    if not media_url:
        return False
    host = (urlparse(media_url).hostname or "").lower()
    return (
        host == "i.redd.it" or host.endswith(".i.redd.it") or
        host == "preview.redd.it" or host.endswith(".preview.redd.it") or
        host == "external-preview.redd.it" or host.endswith(".external-preview.redd.it") or
        host == "v.redd.it" or host.endswith(".v.redd.it") or
        host == "redditmedia.com" or host.endswith(".redditmedia.com")
    )


def _reddit_resolve_url(source_url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    if curl_requests is not None:
        response = curl_requests.get(
            source_url,
            headers=headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=15,
        )
        response.raise_for_status()
        return str(response.url)

    with urlopen(Request(source_url, headers=headers), timeout=15) as response:
        return response.geturl()


def _reddit_fetch_post(source_url):
    post_id = _reddit_post_id(source_url)
    canonical_url = source_url
    if not post_id:
        canonical_url = _reddit_resolve_url(source_url)
        post_id = _reddit_post_id(canonical_url)
    if not post_id:
        return None, canonical_url, None

    endpoint = f"https://www.reddit.com/comments/{post_id}.json?raw_json=1&limit=1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://www.reddit.com/",
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
        payload = response.json()
    else:
        with urlopen(Request(endpoint, headers=headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))

    try:
        post = payload[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None, canonical_url, post_id
    return post if isinstance(post, dict) else None, canonical_url, post_id



def _reddit_decode_html_blob(value):
    """Normaliza URLs que o Reddit deixa escapadas no HTML/JSON embutido."""
    from html import unescape as html_unescape

    if not isinstance(value, str):
        return ""
    value = html_unescape(value)
    replacements = {
        r"\/": "/",
        r"\u0026": "&",
        r"\u003d": "=",
        r"\u003D": "=",
        r"\u002f": "/",
        r"\u002F": "/",
        r"\u003a": ":",
        r"\u003A": ":",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _reddit_html_meta(html_text):
    import re
    from html import unescape as html_unescape

    result = {}
    for tag in re.findall(r"<meta\b[^>]*>", html_text or "", flags=re.I):
        attrs = {}
        for name, quote_char, value in re.findall(
            r"([A-Za-z_:.-]+)\s*=\s*([\"'])(.*?)\2",
            tag,
            flags=re.I | re.S,
        ):
            attrs[name.lower()] = html_unescape(value).strip()
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key and content and key not in result:
            result[key] = content
    return result


def _reddit_probe_url(candidate, referer):
    """Confirma rapidamente se uma faixa direta do CDN do Reddit existe."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer or "https://www.reddit.com/",
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }
    try:
        if curl_requests is not None:
            response = curl_requests.get(
                candidate,
                headers=headers,
                impersonate="chrome",
                default_headers=True,
                allow_redirects=True,
                stream=True,
                timeout=10,
            )
            ok = response.status_code in (200, 206)
            try:
                response.close()
            except Exception:
                pass
            return ok
        with urlopen(Request(candidate, headers=headers), timeout=10) as response:
            return getattr(response, "status", 200) in (200, 206)
    except Exception:
        return False


def _reddit_direct_video_from_blob(blob, canonical_url):
    """Encontra uma faixa MP4 pública em HTML do Reddit sem usar a API JSON."""
    import re

    text = _reddit_decode_html_blob(blob)
    direct = []
    for match in re.finditer(r"https://v\.redd\.it/[^\s\"'<>]+", text, flags=re.I):
        candidate = match.group(0).rstrip("),.;]")
        if ".mp4" in candidate.lower():
            direct.append(candidate)

    # Preferimos a maior faixa DASH mencionada diretamente na página.
    def score(url):
        found = re.search(r"DASH_(\d+)\.mp4", url, flags=re.I)
        return int(found.group(1)) if found else 0

    if direct:
        direct = sorted(dict.fromkeys(direct), key=score, reverse=True)
        for candidate in direct:
            if _reddit_probe_url(candidate, canonical_url):
                return candidate

    # Se o HTML expuser apenas o identificador/base v.redd.it, testamos as
    # resoluções progressivas mais comuns do próprio CDN do Reddit.
    bases = []
    for match in re.finditer(r"https://v\.redd\.it/([A-Za-z0-9]+)", text, flags=re.I):
        base = f"https://v.redd.it/{match.group(1)}"
        if base not in bases:
            bases.append(base)

    for base in bases:
        for height in (1080, 720, 480, 360, 240, 96):
            candidate = f"{base}/DASH_{height}.mp4"
            if _reddit_probe_url(candidate, canonical_url):
                return candidate
    return None


def extract_reddit_public_html_media(source_url):
    """Fallback público via HTML para quando Reddit bloqueia o endpoint .json."""
    import re

    canonical_url = source_url
    if not _reddit_post_id(canonical_url):
        canonical_url = _reddit_resolve_url(source_url)
    post_id = _reddit_post_id(canonical_url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    if curl_requests is not None:
        response = curl_requests.get(
            canonical_url,
            headers=headers,
            impersonate="chrome",
            default_headers=True,
            allow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        canonical_url = str(response.url)
        html_text = response.text
    else:
        with urlopen(Request(canonical_url, headers=headers), timeout=20) as response:
            canonical_url = response.geturl()
            html_text = response.read().decode("utf-8", "ignore")

    meta = _reddit_html_meta(html_text)
    title = str(meta.get("og:title") or meta.get("twitter:title") or "Mídia do Reddit").strip()
    if not title:
        title = "Mídia do Reddit"

    # Vídeo tem prioridade absoluta sobre a capa/og:image.
    video_url = None
    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        candidate = _reddit_clean_url(meta.get(key))
        if candidate and _reddit_allowed_media_url(candidate) and ".mp4" in urlparse(candidate).path.lower():
            video_url = candidate
            break
    if not video_url:
        video_url = _reddit_direct_video_from_blob(html_text, canonical_url)

    image_url = None
    for key in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        candidate = _reddit_clean_url(meta.get(key))
        if candidate and _reddit_allowed_media_url(candidate):
            image_url = candidate
            break

    # Também aceitamos URLs diretas de imagem presentes no HTML quando o meta
    # aponta para um redirecionador /media do Reddit.
    if not image_url:
        decoded = _reddit_decode_html_blob(html_text)
        matches = re.findall(
            r"https://(?:i|preview|external-preview)\.redd\.it/[^\s\"'<>]+",
            decoded,
            flags=re.I,
        )
        for candidate in matches:
            candidate = candidate.rstrip("),.;]")
            if _reddit_allowed_media_url(candidate):
                image_url = candidate
                break

    if video_url:
        item = _reddit_make_item("video", video_url, title, post_id, 1, image_url)
        item["_reddit_page_url"] = canonical_url
        return item

    if image_url:
        item = _reddit_make_item("image", image_url, title, post_id, 1, image_url)
        item["_reddit_page_url"] = canonical_url
        return item
    return None


def _reddit_extension(media_url, default="jpg"):
    extension = Path(urlparse(media_url).path).suffix.lower().lstrip(".")
    if extension == "jpeg":
        return "jpg"
    if extension in ("jpg", "png", "webp", "gif", "avif", "mp4", "webm"):
        return extension
    return default


def _reddit_preview_image(post):
    preview = post.get("preview") if isinstance(post.get("preview"), dict) else {}
    images = preview.get("images") if isinstance(preview.get("images"), list) else []
    if images and isinstance(images[0], dict):
        source = images[0].get("source") if isinstance(images[0].get("source"), dict) else {}
        candidate = _reddit_clean_url(source.get("url"))
        if _reddit_allowed_media_url(candidate):
            return candidate

    thumbnail = _reddit_clean_url(post.get("thumbnail"))
    if _reddit_allowed_media_url(thumbnail):
        return thumbnail
    return None


def _reddit_make_item(kind, media_url, title, post_id, index, thumbnail=None):
    extension = _reddit_extension(media_url, "mp4" if kind == "video" else "jpg")
    if kind == "video" and extension not in ("mp4", "webm"):
        extension = "mp4"
    return {
        "status": "ready",
        "source": "reddit",
        "type": kind,
        "url": media_url,
        "title": title,
        "thumbnail": thumbnail or (media_url if kind == "image" else None),
        "filename": f"reddit-{post_id or 'post'}-{index}.{extension}",
        "media_count": 1,
    }


def _reddit_items_from_post(post, post_id):
    if not isinstance(post, dict):
        return []

    title = str(post.get("title") or "Mídia do Reddit").strip() or "Mídia do Reddit"
    poster = _reddit_preview_image(post)
    items = []

    gallery = post.get("gallery_data") if isinstance(post.get("gallery_data"), dict) else {}
    gallery_items = gallery.get("items") if isinstance(gallery.get("items"), list) else []
    metadata = post.get("media_metadata") if isinstance(post.get("media_metadata"), dict) else {}

    for index, gallery_item in enumerate(gallery_items, start=1):
        if not isinstance(gallery_item, dict):
            continue
        media_id = str(gallery_item.get("media_id") or "")
        node = metadata.get(media_id) if isinstance(metadata.get(media_id), dict) else {}
        source = node.get("s") if isinstance(node.get("s"), dict) else {}

        video_url = _reddit_clean_url(source.get("mp4"))
        gif_url = _reddit_clean_url(source.get("gif"))
        image_url = _reddit_clean_url(source.get("u"))

        if _reddit_allowed_media_url(video_url):
            items.append(_reddit_make_item("video", video_url, title, post_id, index, image_url or poster))
        elif _reddit_allowed_media_url(gif_url):
            items.append(_reddit_make_item("image", gif_url, title, post_id, index, gif_url))
        elif _reddit_allowed_media_url(image_url):
            items.append(_reddit_make_item("image", image_url, title, post_id, index, image_url))

    if items:
        return items

    media = post.get("secure_media") if isinstance(post.get("secure_media"), dict) else {}
    if not media:
        media = post.get("media") if isinstance(post.get("media"), dict) else {}
    reddit_video = media.get("reddit_video") if isinstance(media.get("reddit_video"), dict) else {}

    if not reddit_video:
        preview = post.get("preview") if isinstance(post.get("preview"), dict) else {}
        reddit_video = preview.get("reddit_video_preview") if isinstance(preview.get("reddit_video_preview"), dict) else {}

    video_url = _reddit_clean_url(reddit_video.get("fallback_url"))
    if _reddit_allowed_media_url(video_url):
        return [_reddit_make_item("video", video_url, title, post_id, 1, poster)]

    destination = _reddit_clean_url(post.get("url_overridden_by_dest") or post.get("url"))
    if _reddit_allowed_media_url(destination):
        extension = _reddit_extension(destination, "")
        if extension in ("jpg", "png", "webp", "gif", "avif"):
            return [_reddit_make_item("image", destination, title, post_id, 1, destination)]
        if extension in ("mp4", "webm"):
            return [_reddit_make_item("video", destination, title, post_id, 1, poster)]

    # Crossposts mantêm a mídia original dentro deste bloco.
    crossposts = post.get("crosspost_parent_list") if isinstance(post.get("crosspost_parent_list"), list) else []
    for crosspost in crossposts:
        nested = _reddit_items_from_post(crosspost, post_id)
        if nested:
            return nested
    return []


def extract_reddit_public_media(source_url):
    """Extrai mídia pública do Reddit sem depender de login do usuário.

    Tenta primeiro o JSON legado (melhor para galerias). Se o Reddit responder
    403/autenticação, cai para o HTML público da publicação.
    """
    try:
        post, canonical_url, post_id = _reddit_fetch_post(source_url)
    except Exception as error:
        print(f"JSON público do Reddit indisponível, usando HTML: {type(error).__name__}: {error}")
        post, canonical_url, post_id = None, source_url, _reddit_post_id(source_url)

    if post:
        items = _reddit_items_from_post(post, post_id)
        if items:
            primary = dict(items[0])
            primary["items"] = items
            primary["media_count"] = len(items)
            return primary

    html_media = extract_reddit_public_html_media(canonical_url or source_url)
    if not html_media:
        return None
    primary = dict(html_media)
    primary.pop("_reddit_page_url", None)
    primary["items"] = [dict(primary)]
    primary["media_count"] = 1
    return primary


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

    if host == "vimeo.com" or host.endswith(".vimeo.com"):
        item = normalize_vimeo_media(info, host)
        if not item:
            return None
        primary = dict(item)
        primary["items"] = [item]
        primary["media_count"] = 1
        return primary

    if host == "dailymotion.com" or host.endswith(".dailymotion.com") or host == "dai.ly":
        item = normalize_dailymotion_media(info, host)
        if not item:
            return None
        primary = dict(item)
        primary["items"] = [item]
        primary["media_count"] = 1
        return primary

    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        item = normalize_soundcloud_media(info, host)
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
                "reddit" if "redd.it" in host or "redditmedia" in host else
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

        if source == "vimeo" and cached and download_requested:
            return self.proxy_vimeo_with_ytdlp(cached)

        if source == "dailymotion" and cached:
            return self.proxy_dailymotion_with_ytdlp(cached, download_requested)

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

        # Reddit costuma separar vídeo e áudio em faixas DASH. No download
        # final, o yt-dlp + FFmpeg monta um MP4 completo quando houver áudio.
        if (
            source == "reddit"
            and cached
            and download_requested
            and cached.get("media_type") == "video"
        ):
            return self.proxy_reddit_with_ytdlp(cached)

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

    def proxy_reddit_with_ytdlp(self, cached):
        page_url = cached.get("page_url")
        direct_url = cached.get("url")
        filename = cached.get("filename") or "reddit-video.mp4"
        filename = str(Path(filename).with_suffix(".mp4"))

        if not page_url and not direct_url:
            return self.respond(410, {"error": "Este link expirou. Analise a publicação novamente."})

        format_selector = (
            "best[ext=mp4][vcodec!=none][acodec!=none]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best"
        )

        with tempfile.TemporaryDirectory(prefix="soft-reddit-") as temp_dir:
            temp_path = Path(temp_dir)
            output_template = str(temp_path / "download.%(ext)s")

            # Mantemos yt-dlp como primeira opção quando o Reddit permitir. Em
            # 2026 vários posts públicos já respondem "authentication required",
            # então a falha aqui não encerra mais o download.
            if page_url:
                command = [
                    sys.executable, "-m", "yt_dlp",
                    "--quiet", "--no-warnings", "--no-progress",
                    "-f", format_selector,
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                    page_url,
                ]
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode == 0:
                    files = [
                        path for path in temp_path.iterdir()
                        if path.is_file() and path.suffix.lower() in (".mp4", ".m4v", ".mov")
                    ]
                    if files:
                        final_path = max(files, key=lambda path: path.stat().st_size)
                        return self.send_local_download(final_path, filename, "video/mp4")
                else:
                    detail = (result.stderr or "").strip()
                    print(f"Reddit bloqueou yt-dlp; usando CDN direto: {detail[-1000:]}")

            # Fallback sem cookies: baixa a faixa pública v.redd.it encontrada
            # na análise. Quando há áudio DASH separado, tenta uni-lo com FFmpeg.
            if not direct_url:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Reddit."})

            video_path = temp_path / "video.mp4"
            headers = default_proxy_headers("reddit")
            try:
                if curl_requests is not None:
                    response = curl_requests.get(
                        direct_url,
                        headers=headers,
                        impersonate="chrome",
                        default_headers=True,
                        allow_redirects=True,
                        stream=True,
                        timeout=45,
                    )
                    response.raise_for_status()
                    with video_path.open("wb") as stream:
                        for chunk in response.iter_content():
                            if chunk:
                                stream.write(chunk)
                    try:
                        response.close()
                    except Exception:
                        pass
                else:
                    with urlopen(Request(direct_url, headers=headers), timeout=45) as response, video_path.open("wb") as stream:
                        while chunk := response.read(64 * 1024):
                            stream.write(chunk)
            except Exception as error:
                print(f"Falha ao baixar faixa direta Reddit: {type(error).__name__}: {error}")
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Reddit."})

            base_match = __import__("re").match(r"(https://v\.redd\.it/[A-Za-z0-9]+)", direct_url or "", flags=__import__("re").I)
            audio_url = None
            if base_match:
                base = base_match.group(1)
                for name in ("DASH_AUDIO_128.mp4", "DASH_AUDIO_64.mp4", "DASH_AUDIO.mp4"):
                    candidate = f"{base}/{name}"
                    if _reddit_probe_url(candidate, page_url or "https://www.reddit.com/"):
                        audio_url = candidate
                        break

            if audio_url and shutil.which("ffmpeg"):
                final_path = temp_path / "final.mp4"
                merge = subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(video_path), "-i", audio_url,
                        "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart",
                        str(final_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if merge.returncode == 0 and final_path.is_file() and final_path.stat().st_size > 0:
                    return self.send_local_download(final_path, filename, "video/mp4")
                if merge.stderr:
                    print(f"Falha ao unir áudio Reddit; entregando vídeo: {merge.stderr[-800:]}")

            return self.send_local_download(video_path, filename, "video/mp4")

    def send_local_download(self, file_path, filename, content_type):
        self.send_response(200)
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Content-Disposition", content_disposition(filename))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with file_path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def stream_dailymotion_download(self, page_url, filename):
        """Entrega Dailymotion em fluxo contínuo, sem pré-baixar o arquivo todo.

        Vídeos HLS do Dailymotion podem ter centenas de MB. Antes o worker
        baixava o vídeo inteiro para um temporário e só depois respondia ao
        navegador; isso parecia um download travado. Aqui o yt-dlp resolve a
        faixa e o FFmpeg remuxa para MP4 fragmentado enquanto envia os bytes.
        """
        media_url, headers = resolve_dailymotion_stream_cli(page_url)
        if not media_url:
            return self.respond(502, {"error": "Não foi possível preparar este vídeo do Dailymotion."})

        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]

        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        referer = headers.get("Referer") or headers.get("referer") or "https://www.dailymotion.com/"
        if user_agent:
            command.extend(["-user_agent", user_agent])
        if referer:
            command.extend(["-referer", referer])

        extra_headers = []
        for name, value in headers.items():
            if name.lower() in ("user-agent", "referer"):
                continue
            extra_headers.append(f"{name}: {value}\r\n")
        if extra_headers:
            command.extend(["-headers", "".join(extra_headers)])

        command.extend([
            "-i", media_url,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "pipe:1",
        ])

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        try:
            first_chunk = process.stdout.read(64 * 1024) if process.stdout else b""
            if not first_chunk:
                stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
                process.wait(timeout=5)
                if stderr:
                    print(f"Falha no streaming Dailymotion via FFmpeg: {stderr[-1600:]}")
                return self.respond(502, {"error": "Não foi possível iniciar o download do Dailymotion."})

            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            # Sem Content-Length: o arquivo ainda está sendo produzido. O fim da
            # conexão marca o fim do download e permite começar imediatamente.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            self.wfile.write(first_chunk)
            self.wfile.flush()
            if process.stdout:
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()

            return_code = process.wait()
            if return_code != 0:
                stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
                if stderr:
                    print(f"Streaming Dailymotion terminou com erro: {stderr[-1600:]}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def proxy_dailymotion_with_ytdlp(self, cached, download_requested=False):
        page_url = cached.get("page_url") or cached.get("url")
        info_path = cached.get("info_path")
        filename = cached.get("filename") or "dailymotion-video.mp4"
        filename = str(Path(filename).with_suffix(".mp4"))

        if not page_url and not (info_path and Path(info_path).is_file()):
            return self.respond(410, {"error": "Este link expirou. Analise o vídeo novamente."})

        # No download final, não esperamos centenas de MB serem baixados antes
        # de responder. O fluxo começa assim que o FFmpeg produz o primeiro
        # fragmento MP4.
        if download_requested and page_url:
            return self.stream_dailymotion_download(page_url, filename)

        # A prévia continua curta e leve, pois é pequena e pode ser preparada em
        # arquivo temporário sem atrasar o usuário.
        preview_selector = "best[height<=480]/worst"

        with tempfile.TemporaryDirectory(prefix="soft-dailymotion-") as temp_dir:
            output_template = str(Path(temp_dir) / "download.%(ext)s")

            def run_download(use_info_json):
                command = [
                    sys.executable, "-m", "yt_dlp",
                    "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
                    "--impersonate", "chrome",
                    "-f", preview_selector,
                    "--merge-output-format", "mp4",
                    "--remux-video", "mp4",
                    "-o", output_template,
                    "--download-sections", "*0-12",
                ]
                if use_info_json and info_path and Path(info_path).is_file():
                    command.extend(["--load-info-json", info_path])
                elif page_url:
                    command.append(page_url)
                else:
                    return None
                return subprocess.run(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
                )

            result = run_download(True)
            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                if detail:
                    print(f"Falha no preview Dailymotion via info-json: {detail[-1600:]}")
                result = run_download(False)

            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                print(f"Falha no preview Dailymotion via yt-dlp: {detail[-1600:]}")
                return self.respond(502, {
                    "error": "Não foi possível preparar este vídeo do Dailymotion."
                })

            files = [
                path for path in Path(temp_dir).iterdir()
                if path.is_file() and path.suffix.lower() in (".mp4", ".m4v", ".mov", ".webm")
            ]
            if not files:
                files = [path for path in Path(temp_dir).iterdir() if path.is_file()]
            if not files:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Dailymotion."})

            media_file = max(files, key=lambda path: path.stat().st_size)
            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(media_file.stat().st_size))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            try:
                with media_file.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def proxy_vimeo_with_ytdlp(self, cached):
        page_url = cached.get("page_url") or cached.get("url")
        info_path = cached.get("info_path")
        filename = cached.get("filename") or "vimeo-video.mp4"

        if not page_url and not (info_path and Path(info_path).is_file()):
            return self.respond(410, {"error": "Este link expirou. Analise o vídeo novamente."})

        format_selector = (
            "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=1080]/best[ext=mp4]/best"
        )

        with tempfile.TemporaryDirectory(prefix="soft-vimeo-") as temp_dir:
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
                return subprocess.run(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
                )

            result = run_download(True)
            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                if detail:
                    print(f"Falha no download Vimeo via info-json: {detail[-1600:]}")
                result = run_download(False)

            if result is None or result.returncode != 0:
                detail = (result.stderr if result else "").strip()
                print(f"Falha no download Vimeo via yt-dlp: {detail[-1600:]}")
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Vimeo."})

            files = [path for path in Path(temp_dir).iterdir() if path.is_file()]
            if not files:
                return self.respond(502, {"error": "Não foi possível preparar este vídeo do Vimeo."})
            media_file = max(files, key=lambda path: path.stat().st_size)
            return self.send_local_download(media_file, filename, "video/mp4")

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
