"""MemoryStore 本地测试（不依赖 WS 服务器）"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_store import MemoryStore

TEST_DIR = Path.home() / ".hanako" / "pets" / f"__test__ophelia"

# 清理
if TEST_DIR.exists():
    shutil.rmtree(TEST_DIR)

print("=== MemoryStore 测试 ===\n")

store = MemoryStore("__test_ophelia", base_dir=TEST_DIR)

# 1. 写入记忆
print("1. 写入记忆...")
store.add("你好，今天心情怎么样？", "心情超好的！刚写完一章小说～", emotion="happy", source="dialogue")
store.add("你小说写的是什么类型？", "赛博朋克题材，主角是个双马尾枪手！", emotion="neutral", source="dialogue")
store.add("枪手有什么特别的能力？", "枪法超准，近战也不在话下，最擅长兜底", emotion="neutral", source="dialogue")
store.add("你最喜欢的食物是什么？", "炸牛排！夜之城最好的炸牛排", emotion="happy", source="dialogue")
print(f"   ✓ 写入 4 条，总数: {store.count()}")

# 2. ChromaDB 计数
chroma_count = store.count_chroma()
print(f"\n2. ChromaDB 索引: {chroma_count} 条")

# 3. 关键词搜索
print("\n3. 关键词搜索...")
kw = store.search("赛博朋克")
print(f"   '赛博朋克': {len(kw)} 条")

# 4. 语义搜索
print("\n4. 语义搜索...")
results = store.search_semantic("枪手", limit=3)
print(f"   '枪手': {len(results)} 条")
for r in results:
    print(f"     - {r['text'][:50]} (距离: {r['distance']:.4f})")

# 5. 语义搜索 - 另一关键词
results2 = store.search_semantic("食物", limit=3)
print(f"\n   '食物': {len(results2)} 条")
for r in results2:
    print(f"     - {r['text'][:50]} (距离: {r['distance']:.4f})")

# 6. 格式化
fmt = store.format_recent(3)
print(f"\n5. 格式化最近记忆:\n{fmt}")

# 7. 格式化语义
fmt_sem = store.format_semantic("赛博朋克", limit=3)
print(f"\n6. 语义格式化:\n{fmt_sem}")

store.close()

# 清理
if TEST_DIR.exists():
    shutil.rmtree(TEST_DIR)
    print("\n✓ 清理测试数据")

print("\n✅ 测试完成")
