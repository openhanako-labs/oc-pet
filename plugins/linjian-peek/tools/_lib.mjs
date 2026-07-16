// linjian-peek 共享工具库
// 每个工具文件通过 import 引用

const LINJIAN_URL = (process.env.LINJIAN_URL || '').replace(/\/$/, '');
const LINJIAN_TOKEN = process.env.LINJIAN_TOKEN || '';
const DEFAULT_DEVICE = process.env.LINJIAN_DEFAULT_DEVICE || 'my-phone';

export function requireConfig() {
  if (!LINJIAN_URL) throw new Error('Missing env LINJIAN_URL (linjian-peek server address)');
  if (!LINJIAN_TOKEN) throw new Error('Missing env LINJIAN_TOKEN');
}

export async function linjianFetch(path, options = {}) {
  requireConfig();
  const res = await fetch(`${LINJIAN_URL}${path}`, {
    ...options,
    headers: { 'X-Auth-Token': LINJIAN_TOKEN, ...(options.headers || {}) }
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`linjian-peek HTTP ${res.status}: ${text || res.statusText}`);
  }
  return res;
}

export async function postCommand(payload) {
  const res = await linjianFetch('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  return await res.json();
}

export async function latestInfo() {
  const res = await linjianFetch('/api/latest.json');
  return await res.json();
}

export async function fetchLatestImage() {
  const res = await linjianFetch('/api/latest');
  const mimeType = res.headers.get('content-type')?.split(';')[0] || 'image/jpeg';
  const ab = await res.arrayBuffer();
  const buf = Buffer.from(ab);
  return { mimeType, data: buf.toString('base64'), bytes: buf.byteLength };
}

export function textResult(text) {
  return { content: [{ type: 'text', text }] };
}

export function imageResult(text, imageData, mimeType) {
  return { content: [
    { type: 'text', text },
    { type: 'image', data: imageData, mimeType }
  ] };
}

export { LINJIAN_URL, LINJIAN_TOKEN, DEFAULT_DEVICE };
