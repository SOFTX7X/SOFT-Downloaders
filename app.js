const form = document.querySelector('#downloadForm');
const input = document.querySelector('#mediaUrl');
const note = document.querySelector('#formNote');
const result = document.querySelector('#result');
const preview = document.querySelector('#resultPreview');
const downloadLink = document.querySelector('#downloadLink');
const carouselItems = document.querySelector('#carouselItems');

document.querySelector('#year').textContent = new Date().getFullYear();
document.querySelector('#pasteButton').addEventListener('click', async () => {
  try { input.value = await navigator.clipboard.readText(); input.focus(); } catch { input.focus(); }
});

downloadLink.addEventListener('click', async (event) => {
  event.preventDefault();
  if (downloadLink.dataset.mediaUrl) await downloadMedia(downloadLink.dataset.mediaUrl, downloadLink.dataset.filename, downloadLink);
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const rawUrl = input.value.trim();
  if (!rawUrl) return showError('Cole um link para analisar.');
  note.className = 'form-note'; note.textContent = 'Analisando o link…';
  let data;
  try {
    const response = await fetch('/api/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: rawUrl }) });
    data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível analisar o link.');
  } catch (error) { return showError(error.message || 'Não foi possível analisar o link.'); }
  if (data.status === 'pending') {
    note.textContent = `Lendo publicação do ${data.source}…`;
    try {
      const extraction = await fetch('/api/extract', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: rawUrl }) });
      data = await extraction.json();
      if (!extraction.ok) throw new Error(data.error || 'Não foi possível extrair a mídia.');
    } catch (error) { return showError(error.message || 'Não foi possível extrair a mídia.'); }
  }
  if (data.status !== 'ready') return showError(data.message || 'Este link ainda não é compatível.');

  const items = Array.isArray(data.items) && data.items.length ? data.items : [data];
  const isCarousel = items.length > 1;
  result.classList.toggle('is-carousel', isCarousel);
  if (isCarousel) renderCarousel(items); else renderMedia(items[0]);
  downloadLink.hidden = isCarousel;
  document.querySelector('#resultType').textContent = isCarousel ? 'CARROSSEL ENCONTRADO' : data.type === 'image' ? 'IMAGEM ENCONTRADA' : data.type === 'video' ? 'VÍDEO ENCONTRADO' : 'ÁUDIO ENCONTRADO';
  document.querySelector('#resultTitle').textContent = data.title || 'Mídia pronta para baixar';
  document.querySelector('#resultDescription').textContent = isCarousel
    ? `${items.length} mídias encontradas. Use o ícone em cada arquivo para baixar.`
    : 'A prévia foi carregada a partir do link informado. Confira antes de salvar.';
  note.className = 'form-note success'; note.textContent = isCarousel ? `${items.length} mídias encontradas no carrossel.` : 'Link analisado. A prévia está pronta abaixo.';
  result.hidden = false;
  result.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});

function createPreview(data) {
  const tiktokPoster = data.source === 'tiktok' && data.type === 'video' && data.thumbnail;
  const element = document.createElement(tiktokPoster || data.type === 'image' ? 'img' : data.type === 'video' ? 'video' : 'audio');
  element.src = tiktokPoster ? data.thumbnail : proxyMediaUrl(data.url, data.source, data.proxy_id);
  if (tiktokPoster) {
    element.alt = 'Prévia do vídeo do TikTok';
    element.addEventListener('error', () => element.remove());
  } else if (data.type === 'video') {
    element.autoplay = true; element.muted = true; element.loop = true; element.playsInline = true;
    element.controls = false; element.disablePictureInPicture = true;
    element.setAttribute('controlsList', 'nodownload nofullscreen noremoteplayback');
  }
  if (data.type === 'audio') element.controls = true;
  if (!tiktokPoster) element.addEventListener('error', () => showError('Não foi possível carregar esta mídia. Confirme se o link é público e direto.'));
  return element;
}

function renderMedia(data) {
  preview.replaceChildren(createPreview(data));
  carouselItems.hidden = true; carouselItems.replaceChildren();
  downloadLink.href = '#'; downloadLink.dataset.mediaUrl = proxyMediaUrl(data.url, data.source, data.proxy_id);
  downloadLink.dataset.filename = data.filename || 'soft-download';
}

function renderCarousel(items) {
  preview.replaceChildren();
  carouselItems.hidden = true; carouselItems.replaceChildren();
  const grid = document.createElement('div'); grid.className = 'carousel-grid';
  items.forEach((item, index) => {
    const tile = document.createElement('article'); tile.className = 'carousel-tile';
    tile.append(createPreview(item));
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tile-download'; button.textContent = '↓';
    button.title = `Baixar mídia ${index + 1}`; button.setAttribute('aria-label', `Baixar mídia ${index + 1}`);
    button.addEventListener('click', () => downloadMedia(proxyMediaUrl(item.url, item.source, item.proxy_id), item.filename || 'soft-download', button));
    tile.append(button); grid.append(tile);
  });
  preview.append(grid);
}

async function downloadMedia(mediaUrl, filename, button, quiet = false) {
  const originalLabel = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '…'; }
  try {
    const response = await fetch(mediaUrl);
    if (!response.ok) throw new Error('Arquivo indisponível. Analise o link novamente.');
    const file = await response.blob();
    const objectUrl = URL.createObjectURL(file);
    const trigger = document.createElement('a');
    trigger.href = objectUrl; trigger.download = filename;
    document.body.append(trigger); trigger.click(); trigger.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    if (!quiet) { note.className = 'form-note success'; note.textContent = 'Download iniciado.'; }
  } finally { if (button) { button.disabled = false; button.textContent = originalLabel; } }
}

function showError(message) { note.className = 'form-note error'; note.textContent = message; result.hidden = true; }

function proxyMediaUrl(url, source, proxyId) {
  if (!source || source === 'direct') return url;
  if (proxyId) return `https://api.forgeaioficial.online/media?id=${encodeURIComponent(proxyId)}`;
  return `https://api.forgeaioficial.online/media?url=${encodeURIComponent(url)}`;
}
