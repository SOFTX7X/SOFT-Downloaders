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
  const mediaUrl = downloadLink.dataset.mediaUrl;
  if (!mediaUrl) return;
  event.preventDefault();
  const originalLabel = downloadLink.textContent;
  downloadLink.textContent = 'Preparando arquivo…';
  downloadLink.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(mediaUrl);
    if (!response.ok) throw new Error('Arquivo indisponível. Analise o link novamente.');
    const file = await response.blob();
    const objectUrl = URL.createObjectURL(file);
    const trigger = document.createElement('a');
    trigger.href = objectUrl;
    trigger.download = downloadLink.dataset.filename || 'soft-download';
    document.body.append(trigger);
    trigger.click();
    trigger.remove();
    URL.revokeObjectURL(objectUrl);
    note.className = 'form-note success';
    note.textContent = 'Download iniciado.';
  } catch (error) {
    showError(error.message || 'Não foi possível preparar o arquivo para download.');
  } finally {
    downloadLink.textContent = originalLabel;
    downloadLink.removeAttribute('aria-busy');
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const rawUrl = input.value.trim();
  if (!rawUrl) return showError('Cole um link para analisar.');
  note.className = 'form-note';
  note.textContent = 'Analisando o link…';
  let data;
  try {
    const response = await fetch('/api/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: rawUrl }) });
    data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível analisar o link.');
  } catch (error) { return showError(error.message || 'Não foi possível analisar o link.'); }
  if (data.status === 'pending') {
    note.className = 'form-note';
    note.textContent = `Lendo publicação do ${data.source}…`;
    try {
      const extraction = await fetch('/api/extract', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: rawUrl }) });
      data = await extraction.json();
      if (!extraction.ok) throw new Error(data.error || 'Não foi possível extrair a mídia.');
    } catch (error) { return showError(error.message || 'Não foi possível extrair a mídia.'); }
  }
  if (data.status !== 'ready') return showError(data.message || 'Este link ainda não é compatível.');

  const items = Array.isArray(data.items) && data.items.length ? data.items : [data];
  renderMedia(items[0]);
  renderCarousel(items);
  document.querySelector('#resultTitle').textContent = data.title || 'Mídia pronta para baixar';
  document.querySelector('#resultDescription').textContent = items.length > 1
    ? `Carrossel com ${items.length} mídias. Selecione uma para pré-visualizar e baixar.`
    : 'A prévia foi carregada a partir do link informado. Confira antes de salvar.';
  note.className = 'form-note success';
  note.textContent = items.length > 1 ? `${items.length} mídias encontradas no carrossel.` : 'Link analisado. A prévia está pronta abaixo.';
  result.hidden = false;
  result.scrollIntoView({ behavior:'smooth', block:'nearest' });
});

function renderMedia(data) {
  preview.replaceChildren();
  const element = document.createElement(data.type === 'image' ? 'img' : data.type === 'video' ? 'video' : 'audio');
  element.src = data.url;
  if (data.type === 'video') {
    element.autoplay = true;
    element.muted = true;
    element.loop = true;
    element.playsInline = true;
    element.controls = false;
    element.disablePictureInPicture = true;
    element.setAttribute('controlsList', 'nodownload nofullscreen noremoteplayback');
    element.setAttribute('aria-label', 'Prévia automática do vídeo');
  }
  if (data.type === 'audio') element.controls = true;
  element.addEventListener('error', () => showError('Não foi possível carregar esta mídia. Confirme se o link é público e direto.'));
  preview.append(element);
  downloadLink.href = '#';
  downloadLink.dataset.mediaUrl = data.url;
  downloadLink.dataset.filename = data.filename || 'soft-download';
  document.querySelector('#resultType').textContent = data.type === 'image' ? 'IMAGEM ENCONTRADA' : data.type === 'video' ? 'VÍDEO ENCONTRADO' : 'ÁUDIO ENCONTRADO';
}

function renderCarousel(items) {
  carouselItems.replaceChildren();
  if (items.length < 2) { carouselItems.hidden = true; return; }
  carouselItems.hidden = false;
  items.forEach((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `carousel-item${index === 0 ? ' active' : ''}`;
    button.textContent = `${index + 1}. ${item.type === 'image' ? 'Imagem' : item.type === 'video' ? 'Vídeo' : 'Áudio'}`;
    button.addEventListener('click', () => {
      renderMedia(item);
      carouselItems.querySelectorAll('.carousel-item').forEach((entry) => entry.classList.remove('active'));
      button.classList.add('active');
    });
    carouselItems.append(button);
  });
}

function showError(message) { note.className = 'form-note error'; note.textContent = message; result.hidden = true; }
