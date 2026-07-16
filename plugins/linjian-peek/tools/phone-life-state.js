export const name = 'phone_life_state';
export const description = '读取手机生活状态：电量、充电状态、网络、当前 App、今日屏幕时间、解锁次数、城市/天气等。默认不截图。';
export const parameters = {
  type: 'object',
  properties: {
    device_id: { type: 'string', default: '', description: '设备 ID，默认使用配置的默认设备' }
  }
};

export async function execute(args, context) {
  const { linjianFetch, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const deviceId = args.device_id || DEFAULT_DEVICE;
  const res = await linjianFetch(`/api/life_state?device_id=${encodeURIComponent(deviceId)}`);
  const data = await res.json();
  return textResult(JSON.stringify(data, null, 2));
}
