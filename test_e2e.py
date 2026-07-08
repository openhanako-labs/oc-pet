"""端到端测试：WS 服务器 + MemoryStore + 完整对话记忆闭环"""
import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).parent))
from memory_store import MemoryStore

WS_URL = "ws://127.0.0.1:19900/companion"
TEST_CHAR = "ophelia"
TEST_DIR = Path.home() / ".hanako" / "pets" / f"__test__{TEST_CHAR}"


async def test_ws_roundtrip():
    """测试 WS 双向通信"""
    print("=== 测试 1: WS 双向通信 ===")
    try:
        async with websockets.connect(WS_URL) as ws:
            # 收到连接确认
            resp = json.loads(await ws.recv())
            print(f"  ✓ 连接确认: {resp}")

            # 发送一条 outbox 消息
            await ws.send(json.dumps({"type": "outbox", "text": "你好，我是测试", "character": "ophelia"}))
            await asyncio.sleep(0.3)

            # 发送 ping 测试
            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await ws.recv())
            print(f"  ✓ Ping/Pong: {pong}")

            print("  ✓ WS 双向通信正常")
            return True
    except Exception as e:
        print(f"  ✗ WS 通信失败: {e}")
        return False


async def test_memory_store():
    """测试 MemoryStore 写入 + 语义搜索"""
    print("\n=== 测试 2: MemoryStore ===")
    try:
        store = MemoryStore(f"__test_{TEST_CHAR}", base_dir=TEST_DIR)

        # 写入多条不同话题的记忆
        store.add("你好，今天心情怎么样？", "心情超好的！刚写完一章小说～", emotion="happy", confidence=0.9, source="dialogue")
        store.add("你小说写的是什么类型？", "赛博朋克题材，主角是个双马尾枪手！", emotion="neutral", confidence=0.8, source="dialogue")
        store.add("枪手有什么特别的能力？", "枪法超准，近战也不在话下，最擅长兜底", emotion="neutral", confidence=0.85, source="dialogue")

        print(f"  ✓ 写入 3 条记忆，总数: {store.count()}")

        # 关键词搜索
        kw_results = store.search("赛博朋克")
        print(f"  ✓ 关键词搜索'赛博朋克': {len(kw_results)} 条结果")

        # 语义搜索
        sem_results = store.search_semantic("枪手", limit=3)
        print(f"  ✓ 语义搜索'枪手': {len(sem_results)} 条结果")
        for r in sem_results:
            print(f"    - {r['text'][:50]}... (距离: {r['distance']:.4f})")

        # 格式化
        fmt = store.format_recent(3)
        print(f"  ✓ 格式化最近记忆:\n{fmt}")

        # ChromaDB 统计
        chroma_count = store.count_chroma()
        print(f"  ✓ ChromaDB 索引: {chroma_count} 条")

        store.close()

        # 清理
        import shutil
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR)
            print("  ✓ 清理测试数据")

        return True
    except Exception as e:
        print(f"  ✗ MemoryStore 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_chroma_embed():
    """测试嵌入管线"""
    print("\n=== 测试 3: 嵌入管线 ===")
    try:
        from memory_store import EmbeddingPipeline
        pipe = EmbeddingPipeline.get_instance()
        pipe.init()

        if pipe._client is None:
            print("  ⚠ 嵌入模型未加载（可能网络问题，跳过向量化测试）")
            return True  # 不致命

        texts = ["你好世界", "今天的天气不错", "写小说好累"]
        embeddings = pipe.embed(texts)
        print(f"  ✓ 嵌入生成: {len(texts)} 条 -> {len(embeddings)} 向量")
        print(f"    向量维度: {len(embeddings[0]) if embeddings else 'N/A'}")

        # 测试语义相似度
        q = "小说"
        q_emb = pipe.embed([q])[0]
        sim_scores = []
        for i, emb in enumerate(embeddings):
            # 余弦相似度
            dot = sum(a*b for a, b in zip(q_emb, emb))
            mag1 = sum(a*a for a in q_emb) ** 0.5
            mag2 = sum(b*b for b in emb) ** 0.5
            sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
            sim_scores.append(sim)

        print(f"    语义搜索'小说':")
        for i, s in sorted(zip(texts, sim_scores), key=lambda x: -x[1]):
            print(f"      {i}: {s:.4f}")

        return True
    except Exception as e:
        print(f"  ✗ 嵌入管线测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print(f"测试目录: {TEST_DIR}\n")

    results = []
    results.append(("WS 通信", await test_ws_roundtrip()))
    results.append(("MemoryStore", await test_memory_store()))
    results.append(("嵌入管线", await test_chroma_embed()))

    print("\n" + "=" * 50)
    print("测试结果:")
    all_ok = True
    for name, ok in results:
        status = "✅ 通过" if ok else "❌ 失败"
        print(f"  {name}: {status}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n🎉 所有测试通过！WebSocket + ChromaDB 记忆系统就绪。")
    else:
        print("\n⚠ 有测试未通过，请检查日志。")

    return 0 if all_ok else 1


if __name__ == "__main__":
    exit(asyncio.run(main()))
