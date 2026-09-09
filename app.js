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

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const rawUrl = input.value.trim();
  let url;
  try { url = new URL(rawUrl); } catch { return showError('Cole um link válido, começando com https://.'); }
  if (!/^https?:$/.test(url.protocol)) return showError('Use um link http ou https.');

  const extension = url.pathname.split('.').pop().toLowerCase();
  const types = { mp4:'video', webm:'video', mov:'video', mp3:'audio', wav:'audio', ogg:'audio', jpg:'image', jpeg:'image', png:'image', webp:'image', gif:'image' };
  const type = types[extension];
  if (!type) return showError('Este link ainda não parece ser um arquivo direto. Cole um URL de mídia pública, como um .mp4 ou .jpg.');

  preview.replaceChildren();
  const element = document.createElement(type === 'image' ? 'img' : type === 'video' ? 'video' : 'audio');
  element.src = url.href;
  if (type === 'video') element.controls = true;
  if (type === 'audio') element.controls = true;
  element.addEventListener('error', () => showError('Não foi possível carregar esta mídia. Confirme se o link é público e direto.'));
  preview.append(element);
  downloadLink.href = url.href;
  downloadLink.download = '';
  document.querySelector('#resultType').textContent = type === 'image' ? 'IMAGEM ENCONTRADA' : type === 'video' ? 'VÍDEO ENCONTRADO' : 'ÁUDIO ENCONTRADO';
  document.querySelector('#resultDescription').textContent = 'A prévia foi carregada a partir do link informado. Confira antes de salvar.';
  note.className = 'form-note success';
  note.textContent = 'Link analisado. A prévia está pronta abaixo.';
  result.hidden = false;
  result.scrollIntoView({ behavior:'smooth', block:'nearest' });
});

function showError(message) { note.className = 'form-note error'; note.textContent = message; result.hidden = true; }
