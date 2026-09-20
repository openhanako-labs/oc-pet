"""端到端实测：真实对话文本 → 情绪 → 决策 → 动作。

这个脚本回答一个问题：**接进真实链路后，桌宠会做什么？**
"""
import sys, os, json
sys.path.insert(0, r'W:\Games\Hanako\Work\projects\oc-pet')
os.environ.setdefault('PYTHONPATH', r'W:\Games\Hanako\Work\projects\oc-pet')

from core.expression_director import ExpressionDirector, get_director

# 模拟主 LLM 的输出（[feel:] 标签 / 情绪词）—— 这是本层真实的上游
CASES = [
    ('用户', '你今天这个反应好快啊，厉害',        '开心', '中', '被夸奖'),
    ('用户', '我太开心了！今天真是太好了！',      '开心', '强', '分享好消息'),
    ('用户', '唉，今天又被领导骂了，烦死了',      '失落', '中', '工作受挫'),
    ('用户', '这个报错我搞了一下午都没解决',      '疑惑', '中', '遇到难题'),
    ('用户', '哇！这个东西居然真的能跑起来！',    '惊讶', '强', '意外成功'),
    ('用户', '在吗',                              '平静', '轻', '日常寒暄'),
    ('用户', '别、别这样看着我啦……',              '害羞', '强', '被盯着看'),
    ('用户', '我先去吃饭了，一会儿回来',          '平静', '轻', '告别'),
    ('屏幕', '用户在看代码',                      '思考', '轻', '观察屏幕'),
    ('时钟', '凌晨三点',                          '困',   '强', '深夜'),
    ('用户', '你怎么又卡了',                      '生气', '中', '被打扰'),
    ('用户', '我最近有点累',                      '失落', '轻', '倾诉'),
]

d = get_director('miku')
print(f'引擎: {"在线" if d.client.is_available() else "离线"}')
print(f'快照: {d.snapshot().summary_line()}')
print('=' * 78)
print(f'{"来源":4s} {"输入":28s} {"情绪/强度":10s} {"→ 决策":16s} {"标签":12s} {"档":4s} {"ms":>5s}')
print('-' * 78)

ok = 0
for src, text, emo, inten, cause in CASES:
    r = d.decide(emo, inten, cause)
    tag = ''
    try:
        p = d.snapshot().preset_by_label(str(r.gesture)) if r.gesture else None
        tag = p.label if p else ''
    except Exception:
        pass
    band = r.bands.get('preset', '-')
    if r.accepted:
        ok += 1
    print(f'{src:4s} {text[:26]:28s} {emo}/{inten:6s} {str(r.gesture):16s} {tag:12s} {band:4s} {r.elapsed_ms:5.0f}')

print('-' * 78)
print(f'采纳 {ok}/{len(CASES)}')
print()
print('状态:', json.dumps(d.status(), ensure_ascii=False))
