const form = document.querySelector('#downloadForm');
const input = document.querySelector('#mediaUrl');
const note = document.querySelector('#formNote');
const clearButton = document.querySelector('#clearButton');
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

input.addEventListener('input', syncClearButton);

document.querySelector('#pasteButton').addEventListener('click', async () => {
  try {
    input.value = await navigator.clipboard.readText();
    syncClearButton();
    input.focus();
  } catch {
    input.focus();
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

syncClearButton();

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
  result.classList.remove('is-carousel');
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

function createPreview(data) {
  // Todo item identificado como vídeo usa a mesma prévia inline: autoplay,
  // sem controles, silenciosa e em loop. A thumbnail fica somente como
  // poster/fallback enquanto o vídeo carrega ou se a prévia falhar.
  const element = document.createElement(
    data.type === 'image' ? 'img' : data.type === 'video' ? 'video' : 'audio'
  );

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
  } else if (data.type === 'audio') {
    element.controls = true;
    element.preload = 'metadata';
    element.addEventListener('error', () => showPreviewFallback(data, element));
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

  if (button) {
    button.disabled = true;
    button.textContent = button === downloadLink ? 'Preparando download…' : '…';
  }
  showDownloadStatus('Preparando seu download… aguarde alguns segundos.');

  try {
    const parsed = new URL(mediaUrl, window.location.href);
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
      window.setTimeout(() => transferFrame.remove(), 120000);

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

function proxyMediaUrl(url, source, proxyId, download = false) {
  if (!source || source === 'direct') return url;
  const suffix = download ? '&dl=1' : '';
  if (proxyId) return `https://${WORKER_MEDIA_HOST}/media?id=${encodeURIComponent(proxyId)}${suffix}`;
  if (source === 'tiktok' || source === 'youtube') return '';
  return `https://${WORKER_MEDIA_HOST}/media?url=${encodeURIComponent(url)}${suffix}`;
}
