/**
 * routes/api.js — 桌宠通知 API
 * 接收桌宠发送的 HTTP 通知，写入 outbox + 设置待处理标记
 */
import fs from 'node:fs';
import path from 'node:path';

const DATA_DIR = path.join(
  process.env.USERPROFILE || process.env.HOME || 'C:\\Users\\Administrator',
  '.hanako', 'plugins', 'hanako-desktop-companion'
);

const PENDING_FLAG = path.join(DATA_DIR, '.pending');

export default function (app, ctx) {
  app.post('/api/notify', async (c) => {
    try {
      const body = await c.req.json();
      const text = body?.text || '';
      const character = body?.character || null;

      if (!text) {
        return c.json({ ok: false, error: 'text 字段不能为空' }, 400);
      }

      const outboxPath = path.join(DATA_DIR, 'outbox.json');

      let messages = [];
      if (fs.existsSync(outboxPath)) {
        try {
          const raw = fs.readFileSync(outboxPath, 'utf-8').trim();
          if (raw) messages = JSON.parse(raw);
        } catch {}
      }

      if (!Array.isArray(messages)) messages = [];

      messages.push({
        text,
        character,
        time: Date.now(),
      });

      fs.writeFileSync(outboxPath, JSON.stringify(messages, null, 2), 'utf-8');

      // 设置待处理标记 → Agent 在下次回复前会检测到
      fs.writeFileSync(PENDING_FLAG, '1', 'utf-8');

      return c.json({ ok: true, queued: true });
    } catch (e) {
      return c.json({ ok: false, error: e.message }, 500);
    }
  });

  app.get('/api/health', async (c) => {
    return c.json({ ok: true, name: 'hanako-desktop-companion', version: '0.2.0' });
  });
}
