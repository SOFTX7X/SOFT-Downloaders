const dns = require('node:dns').promises;
const net = require('node:net');

const DIRECT_EXTENSIONS = {
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video',
  mp3: 'audio', wav: 'audio', ogg: 'audio', m4a: 'audio',
  jpg: 'image', jpeg: 'image', png: 'image', webp: 'image', gif: 'image'
};

module.exports = async (req, res) => {
  if (req.method !== 'POST') return res.status(405).json({ error: 'Use POST.' });
  const rawUrl = String(req.body?.url || '').trim();
  let url;
  try { url = new URL(rawUrl); } catch { return res.status(400).json({ error: 'Cole um link válido.' }); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    return res.status(400).json({ error: 'Use um link público http ou https.' });
  }

  try { await assertPublicHost(url.hostname); } catch {
    return res.status(400).json({ error: 'Este endereço não pode ser analisado.' });
  }
  const source = identifySource(url);
  return res.status(200).json(source);
};
function identifySource(url) {
  const host = url.hostname.toLowerCase().replace(/^www\./, '');
  const ext = url.pathname.split('.').pop().toLowerCase();
  if (DIRECT_EXTENSIONS[ext]) {
    return { status: 'ready', source: 'direct', type: DIRECT_EXTENSIONS[ext], url: url.href };
  }
  if (host === 'instagram.com' || host.endsWith('.instagram.com')) {
    return pending('instagram', 'Link do Instagram identificado. O conector público do Instagram será a próxima etapa.');
  }
  if (host === 'tiktok.com' || host.endsWith('.tiktok.com')) {
    return pending('tiktok', 'Link do TikTok identificado. O conector público do TikTok será a próxima etapa.');
  }
  if (host === 'youtube.com' || host.endsWith('.youtube.com') || host === 'youtu.be') {
    return pending('youtube', 'Link do YouTube identificado. Esta fonte não oferece download por API oficial.');
  }
  if (host === 'facebook.com' || host.endsWith('.facebook.com') || host === 'fb.watch') {
    return pending('facebook', 'Link público do Facebook identificado.');
  }
  if (host === 'x.com' || host.endsWith('.x.com') || host === 'twitter.com' || host.endsWith('.twitter.com')) {
    return pending('twitter', 'Link público do X/Twitter identificado.');
  }
  if (host === 'pin.it' || host === 'pinterest.com' || host.endsWith('.pinterest.com')) {
    return pending('pinterest', 'Pin público do Pinterest identificado.');
  }
  if (host === 'redd.it' || host === 'reddit.com' || host.endsWith('.reddit.com')) {
    return pending('reddit', 'Publicação pública do Reddit identificada.');
  }
  if (
    host === 'kwai.com' || host.endsWith('.kwai.com') ||
    host === 'kwai-video.com' || host.endsWith('.kwai-video.com') ||
    host === 'kw.ai' || host.endsWith('.kw.ai')
  ) {
    return pending('kwai', 'Publicação pública do Kwai identificada.');
  }
  if (host === 'dailymotion.com' || host.endsWith('.dailymotion.com') || host === 'dai.ly') {
    return pending('dailymotion', 'Vídeo público do Dailymotion identificado.');
  }
  if (host === 'soundcloud.com' || host.endsWith('.soundcloud.com')) {
    return pending('soundcloud', 'Faixa pública do SoundCloud identificada.');
  }
  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) {
    return pending('linkedin', 'Publicação pública do LinkedIn identificada.');
  }
  if (host === 'twitch.tv' || host.endsWith('.twitch.tv')) {
    return pending('twitch', 'Vídeo ou clip público da Twitch identificado.');
  }
  return { status: 'unsupported', source: 'other', message: 'Este link não parece ser uma mídia direta nem uma plataforma compatível.' };
}
function pending(source, message) { return { status: 'pending', source, message }; }

async function assertPublicHost(hostname) {
  if (hostname === 'localhost' || hostname.endsWith('.local')) throw new Error('local host');
  const addresses = await dns.lookup(hostname, { all: true });
  if (!addresses.length || addresses.some(({ address }) => isPrivateAddress(address))) throw new Error('private address');
}
function isPrivateAddress(address) {
  if (net.isIP(address) === 4) {
    const [a, b] = address.split('.').map(Number);
    return a === 10 || a === 127 || a === 0 || a === 169 && b === 254 || a === 192 && b === 168 || a === 172 && b >= 16 && b <= 31;
  }
  const value = address.toLowerCase();
  return value === '::1' || value.startsWith('fc') || value.startsWith('fd') || value.startsWith('fe80:');
}
