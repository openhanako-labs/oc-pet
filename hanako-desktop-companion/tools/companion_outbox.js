import fs from 'node:fs';
import path from 'node:path';

const DATA_DIR = path.join(
  process.env.USERPROFILE || process.env.HOME || 'C:\\Users\\Administrator',
  '.hanako', 'plugins', 'hanako-desktop-companion'
);

const PENDING_FLAG = path.join(DATA_DIR, '.pending');

export const name = 'companion_outbox';
export const description = '读取桌宠发来的待处理消息。Agent 调用此工具检查桌宠是否发送了新消息。';
export const promptGuidelines =
  "When to use companion_outbox:\n" +
  "- AT THE START OF EVERY REPLY: call this before writing your response\n" +
  "- When you see a companion_send call was made in the last response and want to check if the user replied\n" +
  "- When you want to check if the OC desktop pet sent any messages\n" +
  "\n" +
  "This is the primary way the OC desktop pet (桌面宠物) communicates with you.\n" +
  "If the pet says something, you MUST reply using companion_send!";

export const parameters = {
  type: 'object',
  properties: {
    markAsRead: {
      type: 'boolean',
      description: '是否将已返回的消息标记为已读（清空 outbox）',
      default: true,
    },
  },
};

export function execute(input = {}, ctx) {
  const outboxPath = path.join(DATA_DIR, 'outbox.json');

  try {
    if (!fs.existsSync(outboxPath)) {
      return { content: [{ type: 'text', text: '暂无桌宠消息' }], details: { data: { messages: [] } } };
    }

    const raw = fs.readFileSync(outboxPath, 'utf-8').trim();
    if (!raw) {
      return { content: [{ type: 'text', text: '暂无桌宠消息' }], details: { data: { messages: [] } } };
    }

    const messages = JSON.parse(raw);
    if (!Array.isArray(messages) || messages.length === 0) {
      return { content: [{ type: 'text', text: '暂无桌宠消息' }], details: { data: { messages: [] } } };
    }

    messages.sort((a, b) => (a.time || 0) - (b.time || 0));

    const summary = messages.map((m, i) =>
      `[${i + 1}] ${m.text}${m.character ? ` (角色: ${m.character})` : ''}`
    ).join('\n');

    const text = `收到 ${messages.length} 条桌宠消息：\n${summary}`;
    const data = { messages: messages.map(m => ({ text: m.text, time: m.time, character: m.character })) };

    if (input.markAsRead !== false) {
      fs.writeFileSync(outboxPath, '[]', 'utf-8');
      // 清除待处理标记
      try { fs.unlinkSync(PENDING_FLAG); } catch {}
    }

    return { content: [{ type: 'text', text }], details: { data } };
  } catch (e) {
    return { content: [{ type: 'text', text: `读取失败：${e.message}` }], details: { data: { messages: [], error: e.message } } };
  }
}