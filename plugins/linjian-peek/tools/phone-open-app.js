export const name = 'phone_open_app';
export const description = '打开手机上的指定应用。可填应用名（小红书/微信/QQ/抖音/ChatGPT/Speedcat）或包名。';
export const parameters = {
  type: 'object',
  properties: {
    app: { type: 'string', default: '', description: '应用名（如 小红书、微信）' },
    package: { type: 'string', default: '', description: '应用包名（如 com.xingin.xhs）' },
    device_id: { type: 'string', default: '', description: '设备 ID' }
  }
};

export async function execute(args, context) {
  const { postCommand, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const result = await postCommand({
    action: 'open_app',
    app: args.app || '',
    package: args.package || '',
    device_id: deviceId
  });
  return textResult(JSON.stringify(result, null, 2));
}
