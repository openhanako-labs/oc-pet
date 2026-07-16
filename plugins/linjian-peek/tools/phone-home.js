export const name = 'phone_home';
export const description = '让手机回到桌面（按 Home 键）。';
export const parameters = {
  type: 'object',
  properties: {
    device_id: { type: 'string', default: '', description: '设备 ID' }
  }
};

export async function execute(args, context) {
  const { postCommand, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const result = await postCommand({ action: 'home', device_id: deviceId });
  return textResult(JSON.stringify(result, null, 2));
}
