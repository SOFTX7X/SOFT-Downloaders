const form = document.querySelector('#downloadForm');
const input = document.querySelector('#mediaUrl');
const note = document.querySelector('#formNote');
const result = document.querySelector('#result');
const preview = document.querySelector('#resultPreview');
const downloadLink = document.querySelector('#downloadLink');

document.querySelector('#year').textContent = new Date().getFullYear();

document.querySelector('#pasteButton').addEventListener('click', async () => {
  try { input.value = await navigator.clipboard.readText(); input.focus(); } catch { input.focus(); }
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
  if (data.status !== 'ready') return showError(data.message || 'Este link ainda não é compatível.');

  preview.replaceChildren();
  const element = document.createElement(data.type === 'image' ? 'img' : data.type === 'video' ? 'video' : 'audio');
  element.src = data.url;
  if (data.type === 'video') element.controls = true;
  if (data.type === 'audio') element.controls = true;
  element.addEventListener('error', () => showError('Não foi possível carregar esta mídia. Confirme se o link é público e direto.'));
  preview.append(element);
  downloadLink.href = data.url;
  downloadLink.download = '';
  document.querySelector('#resultType').textContent = data.type === 'image' ? 'IMAGEM ENCONTRADA' : data.type === 'video' ? 'VÍDEO ENCONTRADO' : 'ÁUDIO ENCONTRADO';
  document.querySelector('#resultDescription').textContent = 'A prévia foi carregada a partir do link informado. Confira antes de salvar.';
  note.className = 'form-note success';
  note.textContent = 'Link analisado. A prévia está pronta abaixo.';
  result.hidden = false;
  result.scrollIntoView({ behavior:'smooth', block:'nearest' });
});

function showError(message) { note.className = 'form-note error'; note.textContent = message; result.hidden = true; }
