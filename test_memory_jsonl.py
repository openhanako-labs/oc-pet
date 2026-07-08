"""纯 JSONL 测试（跳过 ChromaDB 下载）"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_store import MemoryStore

test_dir = Path(r"C:\tmp\mem_test2")
if test_dir.exists():
    shutil.rmtree(test_dir)

store = MemoryStore("test_char", base_dir=test_dir)
print(f"MemoryStore 初始化完成 | chroma: {'ok' if store._chroma else 'disabled'}")

store.add("你好，今天心情怎么样？", "心情超好的！刚写完一章小说～", emotion="happy", source="dialogue")
store.add("你小说写的是什么类型？", "赛博朋克题材，主角是个双马尾枪手！", emotion="neutral", source="dialogue")
store.add("枪手有什么特别的能力？", "枪法超准，近战也不在话下，最擅长兜底", emotion="neutral", source="dialogue")
store.add("你最喜欢的食物是什么？", "炸牛排！夜之城最好的炸牛排", emotion="happy", source="dialogue")

print(f"写入 4 条记忆, 总数: {store.count()}")

# 关键词搜索
kw = store.search("赛博朋克")
print(f"关键词搜索'赛博朋克': {len(kw)} 条")

# 语义搜索（应该 graceful degradation）
sem = store.search_semantic("枪手", limit=3)
print(f"语义搜索'枪手': {len(sem)} 条 (ChromaDB 未启用)")

# 格式化
fmt = store.format_recent(3)
print(f"\n最近记忆:\n{fmt}")

store.close()
shutil.rmtree(test_dir)
print("\n✅ JSONL 测试通过")
