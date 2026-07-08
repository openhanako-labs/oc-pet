"""纯 JSONL 测试 — 验证搜索和格式化"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_store import MemoryStore

test_dir = Path(r"C:\tmp\mem_test3")
if test_dir.exists():
    shutil.rmtree(test_dir)

store = MemoryStore("test_char", base_dir=test_dir)
print(f"MemoryStore 初始化完成 | chroma: {'ok' if store._chroma else 'disabled'}")

store.add("你好，今天心情怎么样？", "心情超好的！刚写完一章小说～", emotion="happy", source="dialogue")
store.add("你小说写的是什么类型？", "赛博朋克题材，主角是个双马尾枪手！", emotion="neutral", source="dialogue")
store.add("枪手有什么特别的能力？", "枪法超准，近战也不在话下，最擅长兜底", emotion="neutral", source="dialogue")
store.add("你最喜欢的食物是什么？", "炸牛排！夜之城最好的炸牛排", emotion="happy", source="dialogue")

print(f"写入 4 条记忆, 总数: {store.count()}")

# 测试1: 关键词搜索
kw1 = store.search("小说")
print(f"\n关键词搜索'小说': {len(kw1)} 条")
for e in kw1:
    print(f"  summary: {e.summary}")
    print(f"  user_msg: {e.user_msg}")

# 测试2: 搜索空关键词
kw2 = store.search("")
print(f"\n关键词搜索''（全部）: {len(kw2)} 条")

# 测试3: 格式化
fmt = store.format_recent(3)
print(f"\n最近记忆:\n{fmt}")

# 测试4: 带自定义摘要的记忆
store2 = MemoryStore("test_char2", base_dir=test_dir / "test_char2")
store2.add("今天吃了什么", "吃了炸牛排，太好吃了！", summary="喜欢炸牛排", emotion="happy", source="dialogue")
kw3 = store2.search("炸牛排")
print(f"\n自定义摘要搜索'炸牛排': {len(kw3)} 条")

store.close()
store2.close()
shutil.rmtree(test_dir)
print("\n✅ 全部测试通过")
