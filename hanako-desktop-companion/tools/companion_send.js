import fs from 'node:fs';
import path from 'node:path';

const DATA_DIR = path.join(
  process.env.USERPROFILE || process.env.HOME || 'C:\\Users\\Administrator',
  '.hanako', 'plugins', 'hanako-desktop-companion'
);

export const name = 'companion_send';
export const description = '向桌宠发送消息。写入 response.json，桌宠的 HanakoMonitor 会自动读取并显示。';
export const parameters = {
  type: 'object',
  properties: {
    text: {
      type: 'string',
      description: '要发送给桌宠的文字',
    },
    character: {
      type: 'string',
      description: '可选，指定回复的角色 ID（如 ophelia、yuexiye）',
    },
    anim: {
      type: 'string',
      enum: ['idle', 'extra', 'walk'],
      description: '可选，触发桌宠动画状态',
      default: 'idle',
    },
  },
  required: ['text'],
};

export function execute(input = {}, ctx) {
  const responsePath = path.join(DATA_DIR, 'response.json');

  try {
    const payload = {
      reply: input.text,
      character: input.character || null,
      anim: input.anim || 'idle',
      ts: new Date().toISOString(),
      status: 'ok',
    };

    fs.writeFileSync(responsePath, JSON.stringify(payload, null, 2), 'utf-8');

    const text = `已发送给桌宠：${input.text}${input.character ? ` (角色: ${input.character})` : ''}${input.anim !== 'idle' ? ` [动画: ${input.anim}]` : ''}`;
    return { content: [{ type: 'text', text }], details: { data: { sent: true, ...payload } } };
  } catch (e) {
    return { content: [{ type: 'text', text: `发送失败：${e.message}` }], details: { data: { sent: false, error: e.message } } };
  }
}
