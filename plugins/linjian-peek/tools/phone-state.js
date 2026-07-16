export const name = 'phone_state';
export const description = '读取手机当前状态：前台应用包名、屏幕文字、无障碍服务就绪状态。';
export const parameters = {
  type: 'object',
  properties: {
    device_id: { type: 'string', default: '', description: '设备 ID' }
  }
};

export async function execute(args, context) {
  const { linjianFetch, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const res = await linjianFetch(`/api/device/state?device_id=${encodeURIComponent(deviceId)}`);
  const data = await res.json();
  return textResult(JSON.stringify(data, null, 2));
}
