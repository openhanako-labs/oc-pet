export const name = 'phone_notification';
export const description = '发送一条手机系统通知。只在用户明确要求时使用。';
export const parameters = {
  type: 'object',
  properties: {
    title: { type: 'string', default: '掌心窗提醒', description: '通知标题' },
    message: { type: 'string', default: '看一眼这里。', description: '通知内容' },
    device_id: { type: 'string', default: '', description: '设备 ID' }
  }
};

export async function execute(args, context) {
  const { postCommand, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const result = await postCommand({
    action: 'send_notification',
    device_id: deviceId,
    payload: {
      title: args.title || '掌心窗提醒',
      message: args.message || '看一眼这里。'
    }
  });
  return textResult(JSON.stringify({ ...result, note: '若手机未弹出通知，请在系统设置中允许掌心窗发送通知。' }, null, 2));
}
