"""通过 worker 协议注册 CosyVoice2 说话人。"""
import json
import os
import sys
import subprocess
import time
from pathlib import Path
import logging
logger = logging.getLogger(__name__)


# 路径
_REPO = Path(__file__).parent.parent
_WORKER = _REPO / 'tts_provider' / 'cosyvoice_worker.py'
_MODEL_DIR = _REPO / 'characters' / 'yuexinmiao'
_VOICE_DIR = Path(r'W:\Games\Hanako\Work\通用\助手\语音')
_OUTPUT_PT = _MODEL_DIR.parent / 'CosyVoice2-0.5B' / 'spk2info.pt'

# 音色映射: spk_id -> (ref_audio, ref_text)
SPEAKERS = {
    'luoqixi': ('洛琪希_参考音频.wav', '我是洛琪希，一个旅行者，很高兴认识你。'),
    'ophelia': ('奥菲莉娅_参考音频.wav', '你好，我是奥菲莉娅，未注视者，裂隙的偏折点。'),
    'aimis': ('爱弥斯_参考音频.wav', '我是爱弥斯，电子幽灵，我能感知你的情绪。'),
    'alice': ('艾莉丝_参考音频.wav', '我是艾莉丝，伯雷亚斯家的红发剑士。'),
    'glados': ('glados_参考音频.wav', 'Welcome to Aperture Science.'),
    'rebecca': ('瑞贝卡_参考音频（日配.wav', '夜之城出身，实战专家。'),
}

def main():
    print(f"启动 worker: {_WORKER}")
    
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    
    proc = subprocess.Popen(
        [sys.executable, '-u', str(_WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding='utf-8'
    )
    
    # 读取 hello
    line = proc.stdout.readline()
    print(f"Worker: {line.strip()}")
    
    # 加载模型（需要时间，最多等待 5 分钟）
    print("加载模型...")
    proc.stdin.write(json.dumps({"id": 1, "cmd": "load"}) + "\n")
    proc.stdin.flush()
    
    # 等待响应（模型加载可能需要 2-5 分钟）
    print("等待模型加载...")
    for i in range(300):  # 最多等 5 分钟
        line = proc.stdout.readline()
        if not line:
            print("❌ Worker 无响应")
            return
        line = line.strip()
        if not line:
            continue
        print(f"  [{i+1}s] {line[:100]}")
        try:
            resp = json.loads(line)
            if resp.get('cmd') == 'load' or 'speakers' in resp:
                print(f"加载: {resp.get('ok', False)} | speakers: {resp.get('speakers', [])}")
                break
        except:
            logger.debug("register_speakers: 非致命异常(已静默吞掉)", exc_info=True)
        time.sleep(1)
    
    if not resp.get('ok'):
        print(f"❌ 加载失败: {resp.get('error')}")
        return
    
    # 注册每个说话人
    for spk_id, (audio_file, ref_text) in SPEAKERS.items():
        audio_path = _VOICE_DIR / audio_file
        if not audio_path.exists():
            print(f"  ⚠ {spk_id}: 音频不存在 {audio_path}")
            continue
        
        print(f"  注册 {spk_id}...")
        req = {
            "id": int(time.time() * 1000) % 10000,
            "cmd": "add_spk",
            "text": ref_text,
            "ref_audio": str(audio_path),
            "spk_id": spk_id
        }
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        
        resp = json.loads(proc.stdout.readline())
        if resp.get('ok'):
            print(f"    ✅")
        else:
            print(f"    ❌ {resp.get('error')}")
    
    # 保存
    print("\n保存 spk2info...")
    proc.stdin.write(json.dumps({"id": 999, "cmd": "save_spkinfo"}) + "\n")
    proc.stdin.flush()
    
    resp = json.loads(proc.stdout.readline())
    print(f"保存: {resp}")
    
    # 退出
    proc.stdin.write(json.dumps({"id": 1000, "cmd": "quit"}) + "\n")
    proc.stdin.flush()
    
    print(f"\n✅ 完成! 输出: {_OUTPUT_PT}")

if __name__ == "__main__":
    main()
