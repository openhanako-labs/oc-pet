export const name = 'phone_status';
export const description = '检查掌心窗后端是否在线，以及手机是否已连接。';
export const parameters = {
  type: 'object',
  properties: {}
};

export async function execute(args, context) {
  const { LINJIAN_URL, LINJIAN_TOKEN, textResult } = await import('./_lib.mjs');
  if (!LINJIAN_URL) {
    return textResult('未配置 LINJIAN_URL，请在 .env 中设置掌心窗服务地址。');
  }
  try {
    const healthRes = await fetch(`${LINJIAN_URL}/health`);
    const health = await healthRes.json();
    let latest = null;
    try {
      const latestRes = await fetch(`${LINJIAN_URL}/api/latest.json`, {
        headers: { 'X-Auth-Token': LINJIAN_TOKEN }
      });
      latest = await latestRes.json();
    } catch {}
    return textResult(JSON.stringify({
      ok: true,
      linjian_url: LINJIAN_URL,
      health,
      has_latest: Boolean(latest),
      latest
    }, null, 2));
  } catch (e) {
    return textResult(`连接掌心窗服务失败：${e.message}\n请检查 LINJIAN_URL 和网络。`);
  }
}
