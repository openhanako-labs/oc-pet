export const name = 'phone_alarm';
export const description = '设置手机系统闹钟。只在用户明确要求时使用。hour 为 0-23，minute 为 0-59。';
export const parameters = {
  type: 'object',
  properties: {
    hour: { type: 'number', description: '小时（0-23）' },
    minute: { type: 'number', description: '分钟（0-59）' },
    message: { type: 'string', default: '掌心窗闹钟', description: '闹钟备注' },
    device_id: { type: 'string', default: '', description: '设备 ID' }
  },
  required: ['hour', 'minute']
};

export async function execute(args, context) {
  const { postCommand, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const result = await postCommand({
    action: 'set_alarm',
    device_id: deviceId,
    payload: {
      hour: args.hour,
      minute: args.minute,
      message: args.message || '掌心窗闹钟',
      vibrate: true,
      skip_ui: true
    }
  });
  return textResult(JSON.stringify({ ...result, note: '部分手机系统可能仍会弹出闹钟确认界面。' }, null, 2));
}
