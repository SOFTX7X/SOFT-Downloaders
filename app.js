const form = document.querySelector('#downloadForm');
const input = document.querySelector('#mediaUrl');
const note = document.querySelector('#formNote');
const clearButton = document.querySelector('#clearButton');
const mp3Form = document.querySelector('#mp3Form');
const mp3Input = document.querySelector('#mp3Url');
const mp3Note = document.querySelector('#mp3FormNote');
const mp3ClearButton = document.querySelector('#mp3ClearButton');
const thumbnailForm = document.querySelector('#thumbnailForm');
const thumbnailInput = document.querySelector('#thumbnailUrl');
const thumbnailNote = document.querySelector('#thumbnailFormNote');
const thumbnailClearButton = document.querySelector('#thumbnailClearButton');
const mediaTab = document.querySelector('#mediaTab');
const mp3Tab = document.querySelector('#mp3Tab');
const thumbnailTab = document.querySelector('#thumbnailTab');
const mediaToolPanel = document.querySelector('#mediaToolPanel');
const mp3ToolPanel = document.querySelector('#mp3ToolPanel');
const thumbnailToolPanel = document.querySelector('#thumbnailToolPanel');
const resultScreen = document.querySelector('#resultScreen');
const resultBack = document.querySelector('#resultBack');
const resultLoading = document.querySelector('#resultLoading');
const resultLoadingText = document.querySelector('#resultLoadingText');
const resultError = document.querySelector('#resultError');
const resultErrorText = document.querySelector('#resultErrorText');
const result = document.querySelector('#result');
const resultStage = document.querySelector('.result-stage');
const preview = document.querySelector('#resultPreview');
const downloadLink = document.querySelector('#downloadLink');
const carouselItems = document.querySelector('#carouselItems');
const downloadStatus = document.querySelector('#downloadStatus');

const DEFAULT_NOTE = 'Aceita links diretos para arquivos públicos: MP4, WebM, MP3, JPG, PNG e WebP.';
const DEFAULT_MP3_NOTE = 'A conversão é feita somente quando você usa esta área.';
const DEFAULT_THUMBNAIL_NOTE = 'Disponível para YouTube, Twitch, Kick e Dailymotion.';
const THUMBNAIL_SOURCES = new Set(['youtube', 'twitch', 'kick', 'dailymotion']);

const WORKER_MEDIA_HOST = 'api.forgeaioficial.online';
let activeResultItems = [];

function hasWorkerProxy(data) {
  if (!data || !['tiktok', 'youtube'].includes(data.source)) return true;
  if (data.proxy_id) return true;
  const items = Array.isArray(data.items) ? data.items : [];
  return items.length > 0 && items.every((item) => item && item.proxy_id);
}

document.querySelector('#year').textContent = new Date().getFullYear();
function syncClearButton() {
  if (clearButton) clearButton.hidden = !input.value.trim();
}

function syncMp3ClearButton() {
  if (mp3ClearButton) mp3ClearButton.hidden = !mp3Input.value.trim();
}

function syncThumbnailClearButton() {
  if (thumbnailClearButton) thumbnailClearButton.hidden = !thumbnailInput.value.trim();
}

function setActiveTool(tool) {
  const useMedia = tool === 'media';
  const useMp3 = tool === 'mp3';
  const useThumbnail = tool === 'thumbnail';
  mediaTab.classList.toggle('is-active', useMedia);
  mp3Tab.classList.toggle('is-active', useMp3);
  thumbnailTab.classList.toggle('is-active', useThumbnail);
  mediaTab.setAttribute('aria-selected', String(useMedia));
  mp3Tab.setAttribute('aria-selected', String(useMp3));
  thumbnailTab.setAttribute('aria-selected', String(useThumbnail));
  mediaToolPanel.hidden = !useMedia;
  mp3ToolPanel.hidden = !useMp3;
  thumbnailToolPanel.hidden = !useThumbnail;
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

mediaTab.addEventListener('click', () => setActiveTool('media'));
mp3Tab.addEventListener('click', () => setActiveTool('mp3'));
thumbnailTab.addEventListener('click', () => setActiveTool('thumbnail'));

input.addEventListener('input', syncClearButton);
mp3Input.addEventListener('input', syncMp3ClearButton);
thumbnailInput.addEventListener('input', syncThumbnailClearButton);

document.querySelector('#pasteButton').addEventListener('click', async () => {
  try {
    input.value = await navigator.clipboard.readText();
    syncClearButton();
    input.focus();
  } catch {
    input.focus();
  }
});

document.querySelector('#mp3PasteButton').addEventListener('click', async () => {
  try {
    mp3Input.value = await navigator.clipboard.readText();
    syncMp3ClearButton();
    mp3Input.focus();
  } catch {
    mp3Input.focus();
  }
});

document.querySelector('#thumbnailPasteButton').addEventListener('click', async () => {
  try {
    thumbnailInput.value = await navigator.clipboard.readText();
    syncThumbnailClearButton();
    thumbnailInput.focus();
  } catch {
    thumbnailInput.focus();
  }
});

if (clearButton) {
  clearButton.addEventListener('click', (event) => {
    event.preventDefault();
    input.value = '';
    syncClearButton();
    input.blur();
    clearButton.blur();
  });
}

if (mp3ClearButton) {
  mp3ClearButton.addEventListener('click', (event) => {
    event.preventDefault();
    mp3Input.value = '';
    syncMp3ClearButton();
    mp3Input.blur();
    mp3ClearButton.blur();
  });
}

if (thumbnailClearButton) {
  thumbnailClearButton.addEventListener('click', (event) => {
    event.preventDefault();
    thumbnailInput.value = '';
    syncThumbnailClearButton();
    thumbnailInput.blur();
    thumbnailClearButton.blur();
  });
}

syncClearButton();
syncMp3ClearButton();
syncThumbnailClearButton();

resultBack.addEventListener('click', closeResultScreen);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !resultScreen.hidden) closeResultScreen();
});

downloadLink.addEventListener('click', async (event) => {
  event.preventDefault();

  if (downloadLink.dataset.mode === 'all') {
    await downloadAllMedia(activeResultItems, downloadLink);
    return;
  }

  if (downloadLink.dataset.mediaUrl) {
    await downloadMedia(downloadLink.dataset.mediaUrl, downloadLink.dataset.filename, downloadLink);
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const rawUrl = input.value.trim();
  if (!rawUrl) return showFormError('Cole um link para analisar.');

  input.blur();
  openResultScreen('Analisando o link…');
  note.className = 'form-note';
  note.textContent = DEFAULT_NOTE;

  let data;
  try {
    const response = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível analisar o link.');
  } catch (error) {
    return showResultError(error.message || 'Não foi possível analisar o link.');
  }

  if (data.status === 'pending') {
    setResultLoading(`Lendo publicação do ${data.source}…`);
    try {
      const extraction = await fetch('/api/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: rawUrl }),
      });
      data = await extraction.json();
      if (!extraction.ok) throw new Error(data.error || 'Não foi possível extrair a mídia.');
    } catch (error) {
      return showResultError(error.message || 'Não foi possível extrair a mídia.');
    }
  }

  if (data.status !== 'ready') {
    return showResultError(data.message || 'Este link ainda não é compatível.');
  }

  // TikTok precisa sair do worker com um proxy_id. Sem esse token, a URL
  // temporária do CDN pode até abrir a prévia, mas costuma devolver 403/502
  // no download. Não deixamos o navegador cair nesse fluxo antigo.
  if (!hasWorkerProxy(data)) {
    return showResultError('Não foi possível preparar este vídeo agora. Tente analisar novamente.');
  }

  const items = Array.isArray(data.items) && data.items.length ? data.items : [data];
  const isCarousel = items.length > 1;
  activeResultItems = items;

  result.classList.toggle('is-carousel', isCarousel);
  resultStage.classList.toggle('carousel-mode', isCarousel);
  if (isCarousel) renderCarousel(items);
  else renderMedia(items[0]);
  resultLoading.hidden = true;
  resultError.hidden = true;
  result.hidden = false;
});

mp3Form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const rawUrl = mp3Input.value.trim();
  if (!rawUrl) return showMp3FormError('Cole um link de vídeo para converter.');

  mp3Input.blur();
  openResultScreen('Analisando o vídeo…');
  mp3Note.className = 'form-note';
  mp3Note.textContent = DEFAULT_MP3_NOTE;

  let resolved;
  try {
    const response = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    resolved = await response.json();
    if (!response.ok) throw new Error(resolved.error || 'Não foi possível analisar o link.');
  } catch (error) {
    return showResultError(error.message || 'Não foi possível analisar o link.');
  }

  if (resolved.status === 'unsupported') {
    return showResultError(resolved.message || 'Este link não é compatível com a conversão para MP3.');
  }
  if (resolved.status === 'ready' && resolved.type !== 'video') {
    return showResultError('Esta área aceita links de vídeo. Para fotos ou áudios, use Baixar mídia.');
  }

  setResultLoading('Preparando o vídeo para MP3…');
  let data;
  try {
    const extraction = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    data = await extraction.json();
    if (!extraction.ok) throw new Error(data.error || 'Não foi possível preparar o vídeo.');
  } catch (error) {
    return showResultError(error.message || 'Não foi possível preparar o vídeo.');
  }

  if (data.status !== 'ready') {
    return showResultError(data.message || 'Este vídeo ainda não é compatível com a conversão para MP3.');
  }

  const items = Array.isArray(data.items) && data.items.length ? data.items : [data];
  const video = items.find((item) => mediaTypeForDownload(item) === 'video' && item.proxy_id);
  if (!video) {
    return showResultError('Não encontramos um vídeo com áudio disponível neste link.');
  }

  renderMp3Result(video);
  resultLoading.hidden = true;
  resultError.hidden = true;
  result.hidden = false;
});

thumbnailForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const rawUrl = thumbnailInput.value.trim();
  if (!rawUrl) return showThumbnailFormError('Cole um link para buscar a thumbnail.');

  thumbnailInput.blur();
  openResultScreen('Buscando a thumbnail…');
  thumbnailNote.className = 'form-note';
  thumbnailNote.textContent = DEFAULT_THUMBNAIL_NOTE;

  let resolved;
  try {
    const response = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    resolved = await response.json();
    if (!response.ok) throw new Error(resolved.error || 'Não foi possível analisar o link.');
  } catch (error) {
    return showResultError(error.message || 'Não foi possível analisar o link.');
  }

  if (!THUMBNAIL_SOURCES.has(resolved.source)) {
    return showResultError('Baixar Thumbnail está disponível somente para YouTube, Twitch, Kick e Dailymotion.');
  }
  if (resolved.status === 'unsupported') {
    return showResultError(resolved.message || 'Este link não é compatível com Baixar Thumbnail.');
  }

  setResultLoading(`Buscando capa do ${resolved.source}…`);
  let data;
  try {
    const extraction = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    data = await extraction.json();
    if (!extraction.ok) throw new Error(data.error || 'Não foi possível buscar a thumbnail.');
  } catch (error) {
    return showResultError(error.message || 'Não foi possível buscar a thumbnail.');
  }

  if (data.status !== 'ready' || !THUMBNAIL_SOURCES.has(data.source)) {
    return showResultError(data.message || 'Não foi possível obter a thumbnail deste link.');
  }

  const items = Array.isArray(data.items) && data.items.length ? data.items : [data];
  const item = items.find((value) => value && value.thumbnail && value.proxy_id) ||
    (data.thumbnail && data.proxy_id ? data : null);
  if (!item) {
    return showResultError('Este conteúdo não disponibilizou uma thumbnail para download.');
  }

  renderThumbnailResult(item);
  resultLoading.hidden = true;
  resultError.hidden = true;
  result.hidden = false;
});

function openResultScreen(message) {
  resetResult();
  resultScreen.hidden = false;
  document.body.classList.add('result-screen-open');
  setResultLoading(message);
  requestAnimationFrame(() => resultBack.focus({ preventScroll: true }));
}

function closeResultScreen() {
  resultScreen.hidden = true;
  document.body.classList.remove('result-screen-open');
  resetResult();
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}


function resetResult() {
  result.hidden = true;
  result.classList.remove('is-carousel', 'is-mp3', 'is-thumbnail');
  resultStage.classList.remove('carousel-mode');
  resultLoading.hidden = true;
  resultError.hidden = true;
  preview.replaceChildren();
  delete preview.dataset.type;
  carouselItems.replaceChildren();
  carouselItems.hidden = true;
  activeResultItems = [];
  downloadLink.hidden = true;
  downloadLink.href = '#';
  downloadLink.textContent = 'BAIXAR ARQUIVO';
  delete downloadLink.dataset.mode;
  delete downloadLink.dataset.mediaUrl;
  delete downloadLink.dataset.filename;
  if (downloadStatus) {
    downloadStatus.hidden = true;
    downloadStatus.textContent = '';
    downloadStatus.classList.remove('is-error');
  }
}

function setResultLoading(message) {
  result.hidden = true;
  resultError.hidden = true;
  resultLoading.hidden = false;
  resultLoadingText.textContent = message;
}

function showResultError(message) {
  result.hidden = true;
  resultLoading.hidden = true;
  resultError.hidden = false;
  resultErrorText.textContent = message;
}

function formatAudioTime(seconds) {
  const value = Number.isFinite(Number(seconds)) ? Math.max(0, Number(seconds)) : 0;
  const minutes = Math.floor(value / 60);
  const secs = Math.floor(value % 60);
  return `${minutes}:${String(secs).padStart(2, '0')}`;
}

function createAudioPreview(data) {
  const card = document.createElement('article');
  card.className = 'audio-preview-card';

  const cover = document.createElement('div');
  cover.className = 'audio-cover';

  if (data.thumbnail) {
    const image = document.createElement('img');
    image.src = data.thumbnail;
    image.alt = '';
    image.addEventListener('error', () => {
      image.remove();
      cover.classList.add('is-fallback');
      cover.setAttribute('aria-label', 'Capa indisponível');
    });
    cover.append(image);
  } else {
    cover.classList.add('is-fallback');
    cover.setAttribute('aria-label', 'Capa indisponível');
  }

  const details = document.createElement('div');
  details.className = 'audio-details';

  const title = document.createElement('strong');
  title.className = 'audio-title';
  title.textContent = data.track_title || data.title || 'Áudio';

  const artist = document.createElement('span');
  artist.className = 'audio-artist';
  artist.textContent = data.artist || (data.source === 'soundcloud' ? 'SoundCloud' : 'Áudio');

  const player = document.createElement('div');
  player.className = 'audio-player';

  const playButton = document.createElement('button');
  playButton.type = 'button';
  playButton.className = 'audio-play';
  playButton.setAttribute('aria-label', 'Reproduzir áudio');
  playButton.textContent = '▶';

  const timeline = document.createElement('div');
  timeline.className = 'audio-timeline';

  const progress = document.createElement('input');
  progress.className = 'audio-progress';
  progress.type = 'range';
  progress.min = '0';
  progress.max = '1000';
  progress.value = '0';
  progress.step = '1';
  progress.setAttribute('aria-label', 'Progresso do áudio');

  const timeRow = document.createElement('div');
  timeRow.className = 'audio-time-row';
  const currentTime = document.createElement('span');
  currentTime.textContent = '0:00';
  const durationTime = document.createElement('span');
  durationTime.textContent = data.duration ? formatAudioTime(data.duration) : '--:--';
  timeRow.append(currentTime, durationTime);
  timeline.append(progress, timeRow);
  player.append(playButton, timeline);
  details.append(title, artist, player);

  const audio = document.createElement('audio');
  audio.src = proxyMediaUrl(data.url, data.source, data.proxy_id, false);
  audio.preload = 'metadata';
  audio.className = 'audio-engine';
  audio.setAttribute('aria-hidden', 'true');

  const syncPlayer = () => {
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration
      : Number(data.duration || 0);
    if (duration > 0) {
      const ratio = Math.min(1, Math.max(0, audio.currentTime / duration));
      progress.value = String(Math.round(ratio * 1000));
      durationTime.textContent = formatAudioTime(duration);
    }
    currentTime.textContent = formatAudioTime(audio.currentTime);
  };

  playButton.addEventListener('click', async () => {
    if (audio.paused) {
      try {
        await audio.play();
      } catch (_) {
        return;
      }
    } else {
      audio.pause();
    }
  });

  progress.addEventListener('input', () => {
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration
      : Number(data.duration || 0);
    if (duration > 0) {
      audio.currentTime = (Number(progress.value) / 1000) * duration;
      syncPlayer();
    }
  });

  audio.addEventListener('play', () => {
    playButton.textContent = '❚❚';
    playButton.setAttribute('aria-label', 'Pausar áudio');
  });
  audio.addEventListener('pause', () => {
    playButton.textContent = '▶';
    playButton.setAttribute('aria-label', 'Reproduzir áudio');
  });
  audio.addEventListener('ended', () => {
    playButton.textContent = '▶';
    progress.value = '0';
    currentTime.textContent = '0:00';
  });
  audio.addEventListener('loadedmetadata', syncPlayer);
  audio.addEventListener('durationchange', syncPlayer);
  audio.addEventListener('timeupdate', syncPlayer);
  audio.addEventListener('error', () => {
    playButton.disabled = true;
    playButton.textContent = '▶';
    artist.textContent = 'Prévia de áudio indisponível';
  });

  card.append(cover, details, audio);
  return card;
}

function createPreview(data) {
  // Todo item identificado como vídeo usa a mesma prévia inline: autoplay,
  // sem controles, silenciosa e em loop. A thumbnail fica somente como
  // poster/fallback enquanto o vídeo carrega ou se a prévia falhar.
  if (data.type === 'audio') return createAudioPreview(data);

  const element = document.createElement(data.type === 'image' ? 'img' : 'video');
  element.src = proxyMediaUrl(data.url, data.source, data.proxy_id, false);

  if (data.type === 'image') {
    element.alt = '';
    element.addEventListener('error', () => showPreviewFallback(data, element));
  } else if (data.type === 'video') {
    element.autoplay = true;
    element.muted = true;
    element.loop = true;
    element.playsInline = true;
    element.controls = false;
    element.removeAttribute('controls');
    element.preload = 'auto';
    element.disablePictureInPicture = true;
    element.disableRemotePlayback = true;
    if (data.thumbnail) element.poster = data.thumbnail;
    element.setAttribute('controlsList', 'nodownload noremoteplayback nofullscreen');
    element.addEventListener('error', () => showPreviewFallback(data, element));
    element.addEventListener('canplay', () => {
      element.play().catch(() => {});
    }, { once: true });
  }

  return element;
}

function showPreviewFallback(data, element) {
  if (!element.isConnected) return;
  if (data.thumbnail && data.type === 'video') {
    const image = document.createElement('img');
    image.src = data.thumbnail;
    image.alt = '';
    image.addEventListener('error', () => image.replaceWith(createPreviewUnavailable()));
    element.replaceWith(image);
    return;
  }
  element.replaceWith(createPreviewUnavailable());
}

function createPreviewUnavailable() {
  const fallback = document.createElement('div');
  fallback.className = 'preview-unavailable';
  fallback.textContent = 'Prévia indisponível';
  return fallback;
}

function renderMedia(data) {
  preview.dataset.type = data.type || 'media';
  preview.replaceChildren(createPreview(data));
  carouselItems.hidden = true;
  carouselItems.replaceChildren();
  downloadLink.hidden = false;
  downloadLink.href = '#';
  downloadLink.textContent = downloadLabelForMedia(data);
  downloadLink.dataset.mode = 'single';
  downloadLink.dataset.mediaUrl = proxyMediaUrl(data.url, data.source, data.proxy_id, true);
  downloadLink.dataset.filename = data.filename || 'soft-download';
}

function renderMp3Result(data) {
  result.classList.add('is-mp3');
  result.classList.remove('is-carousel');
  resultStage.classList.remove('carousel-mode');
  preview.dataset.type = 'mp3';
  carouselItems.hidden = true;
  carouselItems.replaceChildren();

  const card = document.createElement('article');
  card.className = 'mp3-audio-card';

  const details = document.createElement('div');
  details.className = 'audio-details mp3-audio-details';

  const title = document.createElement('strong');
  title.className = 'audio-title';
  title.textContent = data.title || 'Áudio do vídeo';

  const format = document.createElement('span');
  format.className = 'audio-artist';
  format.textContent = 'MP3 • 192 kbps';

  const player = document.createElement('div');
  player.className = 'audio-player';

  const playButton = document.createElement('button');
  playButton.type = 'button';
  playButton.className = 'audio-play';
  playButton.setAttribute('aria-label', 'Reproduzir áudio');
  playButton.textContent = '▶';

  const timeline = document.createElement('div');
  timeline.className = 'audio-timeline';

  const progress = document.createElement('input');
  progress.className = 'audio-progress';
  progress.type = 'range';
  progress.min = '0';
  progress.max = '1000';
  progress.value = '0';
  progress.step = '1';
  progress.setAttribute('aria-label', 'Progresso do áudio');

  const timeRow = document.createElement('div');
  timeRow.className = 'audio-time-row';
  const currentTime = document.createElement('span');
  currentTime.textContent = '0:00';
  const durationTime = document.createElement('span');
  durationTime.textContent = data.duration ? formatAudioTime(data.duration) : '--:--';
  timeRow.append(currentTime, durationTime);
  timeline.append(progress, timeRow);
  player.append(playButton, timeline);
  details.append(title, format, player);

  const audio = document.createElement('audio');
  audio.src = proxyMp3PreviewUrl(data.proxy_id);
  audio.preload = 'metadata';
  audio.className = 'audio-engine';
  audio.setAttribute('aria-hidden', 'true');

  const syncPlayer = () => {
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration
      : Number(data.duration || 0);
    if (duration > 0) {
      const ratio = Math.min(1, Math.max(0, audio.currentTime / duration));
      progress.value = String(Math.round(ratio * 1000));
      durationTime.textContent = formatAudioTime(duration);
    }
    currentTime.textContent = formatAudioTime(audio.currentTime);
  };

  playButton.addEventListener('click', async () => {
    if (audio.paused) {
      try {
        await audio.play();
      } catch (_) {
        return;
      }
    } else {
      audio.pause();
    }
  });

  progress.addEventListener('input', () => {
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration
      : Number(data.duration || 0);
    if (duration > 0 && Number.isFinite(audio.duration)) {
      audio.currentTime = (Number(progress.value) / 1000) * duration;
      syncPlayer();
    }
  });

  audio.addEventListener('play', () => {
    playButton.textContent = '❚❚';
    playButton.setAttribute('aria-label', 'Pausar áudio');
  });
  audio.addEventListener('pause', () => {
    playButton.textContent = '▶';
    playButton.setAttribute('aria-label', 'Reproduzir áudio');
  });
  audio.addEventListener('ended', () => {
    playButton.textContent = '▶';
    progress.value = '0';
    currentTime.textContent = '0:00';
  });
  audio.addEventListener('loadedmetadata', syncPlayer);
  audio.addEventListener('durationchange', syncPlayer);
  audio.addEventListener('timeupdate', syncPlayer);
  audio.addEventListener('error', () => {
    playButton.disabled = true;
    playButton.textContent = '▶';
    format.textContent = 'Prévia de áudio indisponível';
  });

  card.append(details, audio);
  preview.replaceChildren(card);

  const mp3Filename = String(data.filename || 'soft-download.mp4').replace(/\.[^.]+$/, '') + '.mp3';
  downloadLink.hidden = false;
  downloadLink.href = '#';
  downloadLink.textContent = 'BAIXAR MP3';
  downloadLink.dataset.mode = 'mp3';
  downloadLink.dataset.mediaUrl = proxyMp3Url(data.proxy_id);
  downloadLink.dataset.filename = mp3Filename;
}

function renderThumbnailResult(data) {
  result.classList.add('is-thumbnail');
  result.classList.remove('is-carousel', 'is-mp3');
  resultStage.classList.remove('carousel-mode');
  preview.dataset.type = 'thumbnail';
  carouselItems.hidden = true;
  carouselItems.replaceChildren();

  const image = document.createElement('img');
  image.src = data.thumbnail;
  image.alt = data.title ? `Thumbnail de ${data.title}` : 'Thumbnail do vídeo';
  image.addEventListener('error', () => image.replaceWith(createPreviewUnavailable()));
  preview.replaceChildren(image);

  const thumbnailFilename = thumbnailFilenameFor(data);
  downloadLink.hidden = false;
  downloadLink.href = '#';
  downloadLink.textContent = 'BAIXAR THUMBNAIL';
  downloadLink.dataset.mode = 'thumbnail';
  downloadLink.dataset.mediaUrl = proxyThumbnailUrl(data.proxy_id, true);
  downloadLink.dataset.filename = thumbnailFilename;
}

function thumbnailFilenameFor(data) {
  const source = String(data.source || 'video').replace(/[^a-z0-9_-]+/gi, '-');
  const base = String(data.filename || `${source}-thumbnail`)
    .replace(/\.[^.]+$/, '')
    .replace(/[^a-z0-9 _-]+/gi, '')
    .trim() || `${source}-thumbnail`;
  return `${base}-thumbnail.jpg`;
}

function renderCarousel(items) {
  delete preview.dataset.type;
  preview.replaceChildren();
  carouselItems.hidden = true;
  carouselItems.replaceChildren();

  const grid = document.createElement('div');
  grid.className = 'carousel-grid';

  items.forEach((item, index) => {
    const tile = document.createElement('article');
    tile.className = 'carousel-tile';
    tile.append(createPreview(item));

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tile-download';
    button.textContent = '↓';
    button.title = `Baixar mídia ${index + 1}`;
    button.setAttribute('aria-label', `Baixar mídia ${index + 1}`);
    button.addEventListener('click', () => downloadMedia(
      proxyMediaUrl(item.url, item.source, item.proxy_id, true),
      item.filename || 'soft-download',
      button,
    ));

    tile.append(button);
    grid.append(tile);
  });

  preview.append(grid);

  downloadLink.hidden = false;
  downloadLink.href = '#';
  downloadLink.textContent = 'BAIXAR TODOS';
  downloadLink.dataset.mode = 'all';
  delete downloadLink.dataset.mediaUrl;
  delete downloadLink.dataset.filename;
}

function mediaTypeForDownload(data) {
  if (!data) return 'media';

  // TikTok e YouTube são sempre tratados como vídeo; a imagem mostrada na
  // prévia é apenas a capa e não deve mudar o texto do botão.
  if (data.source === 'tiktok' || data.source === 'youtube') return 'video';

  const filename = String(data.filename || '').toLowerCase();
  const url = String(data.url || '').toLowerCase();

  if (/\.(mp4|webm|mov|m4v)(?:$|[?#])/.test(filename) || /mime_type=video/.test(url)) return 'video';
  if (/\.(mp3|m4a|aac|wav|ogg|opus)(?:$|[?#])/.test(filename)) return 'audio';
  if (/\.(jpe?g|png|webp|gif|avif)(?:$|[?#])/.test(filename)) return 'image';

  if (data.type === 'video' || data.type === 'audio' || data.type === 'image') return data.type;
  return 'media';
}

function downloadLabelForMedia(data) {
  const type = mediaTypeForDownload(data);
  if (type === 'image') return 'BAIXAR FOTO';
  if (type === 'video') return 'BAIXAR VÍDEO';
  if (type === 'audio') return 'BAIXAR ÁUDIO';
  return 'BAIXAR ARQUIVO';
}

async function downloadAllMedia(items, button) {
  if (!Array.isArray(items) || items.length < 2) return;

  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = 'PREPARANDO…';
  showDownloadStatus(`Preparando ${items.length} arquivos…`);

  try {
    let started = 0;
    for (const item of items) {
      const mediaUrl = proxyMediaUrl(item.url, item.source, item.proxy_id, true);
      if (!mediaUrl) continue;

      // Dispara cada arquivo com um pequeno intervalo para não sobrecarregar
      // o worker nem o navegador com todas as requisições no mesmo instante.
      const transferFrame = document.createElement('iframe');
      transferFrame.hidden = true;
      transferFrame.setAttribute('aria-hidden', 'true');
      transferFrame.src = mediaUrl;
      document.body.append(transferFrame);
      window.setTimeout(() => transferFrame.remove(), 120000);
      started += 1;

      await new Promise((resolve) => window.setTimeout(resolve, 650));
    }

    if (!started) throw new Error('Não foi possível preparar os arquivos.');
    showDownloadStatus(started === items.length
      ? 'Downloads enviados ao navegador.'
      : `${started} de ${items.length} downloads foram enviados ao navegador.`);
    window.setTimeout(hideDownloadStatus, 5000);
  } catch (error) {
    showDownloadStatus(error.message || 'Não foi possível iniciar os downloads.', true);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function downloadMedia(mediaUrl, filename, button) {
  const originalLabel = button?.textContent;
  let restoreTimer;

  const parsed = new URL(mediaUrl, window.location.href);
  const isMp3Download = parsed.searchParams.get('mp3') === '1';
  const isThumbnailDownload = parsed.searchParams.get('thumb') === '1';
  if (button) {
    button.disabled = true;
    button.textContent = button === downloadLink
      ? (isMp3Download ? 'Gerando MP3…' : isThumbnailDownload ? 'Baixando capa…' : 'Preparando download…')
      : '…';
  }
  showDownloadStatus(isMp3Download
    ? 'Gerando o MP3… aguarde alguns segundos.'
    : isThumbnailDownload
      ? 'Preparando a thumbnail…'
      : 'Preparando seu download… aguarde alguns segundos.');

  try {
    const isWorkerDownload = parsed.hostname === 'api.forgeaioficial.online' && parsed.pathname === '/media';

    if (isWorkerDownload) {
      // Inicia o arquivo sem navegar a aba principal para o domínio do worker.
      // Se o upstream oscilar, a página do usuário permanece no downloader em
      // vez de ser substituída por uma tela 502 do Cloudflare.
      const transferFrame = document.createElement('iframe');
      transferFrame.hidden = true;
      transferFrame.setAttribute('aria-hidden', 'true');
      transferFrame.src = mediaUrl;
      document.body.append(transferFrame);
      window.setTimeout(
        () => transferFrame.remove(),
        isMp3Download ? 2 * 60 * 60 * 1000 : 120000,
      );

      restoreTimer = window.setTimeout(() => {
        if (button) {
          button.disabled = false;
          button.textContent = originalLabel;
        }
        showDownloadStatus('Download enviado ao navegador.');
        window.setTimeout(hideDownloadStatus, 4500);
      }, 12000);
      return;
    }

    const response = await fetch(mediaUrl);
    if (!response.ok) throw new Error('Arquivo indisponível. Analise o link novamente.');
    const file = await response.blob();
    const objectUrl = URL.createObjectURL(file);
    const trigger = document.createElement('a');
    trigger.href = objectUrl;
    trigger.download = filename;
    document.body.append(trigger);
    trigger.click();
    trigger.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    showDownloadStatus('Download iniciado.');
    window.setTimeout(hideDownloadStatus, 3500);
  } catch (error) {
    showDownloadStatus(error.message || 'Não foi possível iniciar o download.', true);
    throw error;
  } finally {
    if (!restoreTimer && button) {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }
}

function showDownloadStatus(message, isError = false) {
  if (!downloadStatus) return;
  downloadStatus.hidden = false;
  downloadStatus.textContent = message;
  downloadStatus.classList.toggle('is-error', isError);
}

function hideDownloadStatus() {
  if (!downloadStatus) return;
  downloadStatus.hidden = true;
  downloadStatus.textContent = '';
  downloadStatus.classList.remove('is-error');
}

function showFormError(message) {
  note.className = 'form-note error';
  note.textContent = message;
}

function showMp3FormError(message) {
  mp3Note.className = 'form-note error';
  mp3Note.textContent = message;
}

function showThumbnailFormError(message) {
  thumbnailNote.className = 'form-note error';
  thumbnailNote.textContent = message;
}

function proxyThumbnailUrl(proxyId, download = false) {
  if (!proxyId) return '';
  const suffix = download ? '&dl=1' : '';
  return `https://${WORKER_MEDIA_HOST}/media?id=${encodeURIComponent(proxyId)}&thumb=1${suffix}`;
}

function proxyMp3Url(proxyId) {
  if (!proxyId) return '';
  return `https://${WORKER_MEDIA_HOST}/media?id=${encodeURIComponent(proxyId)}&mp3=1&dl=1`;
}

function proxyMp3PreviewUrl(proxyId) {
  if (!proxyId) return '';
  return `https://${WORKER_MEDIA_HOST}/media?id=${encodeURIComponent(proxyId)}&mp3=1`;
}

function proxyMediaUrl(url, source, proxyId, download = false) {
  if (!source || source === 'direct') return url;
  const suffix = download ? '&dl=1' : '';
  if (proxyId) return `https://${WORKER_MEDIA_HOST}/media?id=${encodeURIComponent(proxyId)}${suffix}`;
  if (source === 'tiktok' || source === 'youtube') return '';
  return `https://${WORKER_MEDIA_HOST}/media?url=${encodeURIComponent(url)}${suffix}`;
}
