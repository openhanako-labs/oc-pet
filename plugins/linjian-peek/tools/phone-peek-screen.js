export const name = 'phone_peek_screen';
export const description = '向手机请求一张新截图并返回。手机端必须已启动且无障碍截图权限已开启。';
export const parameters = {
  type: 'object',
  properties: {
    wait_seconds: { type: 'number', default: 25, description: '等待手机上传截图的秒数（3-60）' },
    device_id: { type: 'string', default: '', description: '设备 ID' }
  }
};

export async function execute(args, context) {
  const { linjianFetch, postCommand, latestInfo, fetchLatestImage, imageResult, textResult, DEFAULT_DEVICE } = await import('./_lib.mjs');
  const waitSeconds = Math.max(3, Math.min(60, args.wait_seconds || 25));
  const deviceId = args.device_id || DEFAULT_DEVICE;

  // 记录当前最新截图的 mtime
  let beforeMtime = 0;
  try {
    const info = await latestInfo();
    beforeMtime = Number(info.mtime || 0);
  } catch {}

  // 发送 peek 命令
  await postCommand({ action: 'peek', device_id: deviceId });

  // 轮询等待新截图
  const deadline = Date.now() + waitSeconds * 1000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const info = await latestInfo();
      if (Number(info.mtime || 0) > beforeMtime) {
        const img = await fetchLatestImage();
        return imageResult(
          `掌心窗已收到新截图：${info.filename || 'latest'}，大小约 ${info.size || img.bytes} bytes。`,
          img.data, img.mimeType
        );
      }
    } catch {}
  }

  return textResult(`等待 ${waitSeconds} 秒后未收到新截图。请检查：手机 App 是否启动、无障碍权限是否开启、服务器地址和 Token 是否一致。`);
}
