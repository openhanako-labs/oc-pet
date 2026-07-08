"""对话记忆存储 — JSONL 本地持久化 + ChromaDB 向量检索

架构：
  JSONL 是源真理（追加写，不可变）
  ChromaDB 是派生索引（语义向量检索，异步写入）

用法:
    store = MemoryStore("ophelia")
    store.add("你好", "你好呀～今天想聊什么？")
    # 向量检索
    results = store.search_semantic("打招呼", limit=3)
    # 关键词搜索（旧接口）
    results = store.search("打招呼")
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from memory_compressor import CompressionEngine

logger = logging.getLogger(__name__)

# ── ChromaDB 导入 ────────────────────────────────────────────

CHROMA_AVAILABLE = False
try:
    import chromadb
    from chromadb.config import Settings
    CHROMA_AVAILABLE = True
except ImportError:
    pass

# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class MemoryEntry:
    """单条记忆"""
    user_msg: str          # 用户消息原文
    bot_reply: str         # 角色回复原文
    summary: str           # 一句话摘要（可选，用于快速检索）
    timestamp: str         # ISO 格式时间戳
    session_id: str = ""   # 会话 ID（预留，目前留空=全局）
    # 元数据字段
    emotion: str = "neutral"        # 情感标签
    validated_count: int = 1        # 验证次数
    confidence: float = 0.7         # 置信度（0~1）
    source: str = ""              # 来源（对话/观察/系统）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(**d)


# ── 嵌入管线 ──────────────────────────────────────────────

class EmbeddingPipeline:
    """轻量级文本嵌入管线，使用 local 模型。"""

    _instance = None
    _embeddings: list[list[float]] | None = None
    _texts: list[str] | None = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._client = None
        self._model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        self._embeddings = None
        self._texts = None

    def init(self):
        """懒加载嵌入模型"""
        if self._client is not None:
            return
        try:
            import torch
            from sentence_transformers import SentenceTransformer
            # 中文友好的轻量模型
            self._client = SentenceTransformer(self._model_name)
            logger.info("EmbeddingPipeline loaded: %s", self._model_name)
        except Exception as e:
            logger.warning("EmbeddingPipeline failed to load: %s", e)
            self._client = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量生成嵌入向量"""
        if not texts:
            return []
        self.init()
        if self._client is None:
            # 降级：无模型时返回空
            logger.warning("No embedding model available, returning empty vectors")
            return [[] for _ in texts]

        embeddings = self._client.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        return embeddings.tolist()


# ── ChromaDB 索引 ──────────────────────────────────────────────

class ChromaIndex:
    """ChromaDB 向量索引，管理语义搜索。"""

    COLLECTION_NAME = "pet_memory"
    METADATA = {"hnsw:space": "cosine"}  # 余弦距离

    def __init__(self, db_path: str, character_id: str):
        self.character_id = character_id
        self._db_path = db_path
        self._client = None
        self._collection = None
        self._init()

    def _init(self):
        # 临时禁用 ChromaDB 模型下载（等待缓存到位）
        self._client = None
        return


    def add(self, text: str, metadata: dict, embedding: list[float] | None = None):
        """添加一条记忆到 ChromaDB。embedding 参数保留但实际不使用（ChromaDB 自动算）。"""
        if self._client is None:
            return

        try:
            self._collection.add(
                documents=[text],
                metadatas=[metadata],
                ids=[metadata.get("chroma_id", f"mem_{int(time.time())}_{id(self)}")],
            )
        except Exception as e:
            logger.warning("ChromaDB add failed: %s", e)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """语义搜索"""
        if self._client is None:
            return []

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(limit, self._collection.count() or 1),
                include=["documents", "metadatas", "distances"],
            )

            items = []
            for i in range(len(results["ids"][0])):
                items.append({
                    "text": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i],
                })
            return items
        except Exception as e:
            logger.warning("ChromaDB search failed: %s", e)
            return []

    def count(self) -> int:
        if self._client is None:
            return 0
        try:
            return self._collection.count() or 0
        except Exception:
            return 0

    def close(self):
        if self._client:
            try:
                self._client = None
            except Exception:
                pass


# ── 存储引擎 ──────────────────────────────────────────────

class MemoryStore:
    """按角色隔离的记忆存储。

    文件布局:
        ~/.hanako/pets/{character_id}/memory.jsonl  (源真理)
        ~/.hanako/pets/{character_id}/chroma/        (ChromaDB)
    """

    def __init__(self, character_id: str, base_dir: Path | None = None):
        self.character_id = character_id

        if base_dir is None:
            base_dir = Path.home() / ".hanako" / "pets"
        self._dir = base_dir / character_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "memory.jsonl"

        # 内存索引（懒加载）
        self._entries: list[MemoryEntry] | None = None

        # ChromaDB
        self._chroma: ChromaIndex | None = None
        try:
            chroma_db_path = str(base_dir / character_id / "chroma")
            self._chroma = ChromaIndex(chroma_db_path, character_id)
        except Exception:
            pass

        # 压缩引擎（P0）
        self._compressor = CompressionEngine(
            compressed_dir=self._dir,
            character_id=character_id,
            threshold=50,
        )

        logger.debug("MemoryStore ready | char=%s | file=%s | chroma=%s | compressor=%s",
                      character_id, self._file, 'ok' if self._chroma else 'disabled', 'ok')

    # ── 内部 ──

    def _load(self) -> list[MemoryEntry]:
        """惰性加载全部条目"""
        if self._entries is not None:
            return self._entries

        entries: list[MemoryEntry] = []
        if self._file.exists():
            for line in self._file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(MemoryEntry.from_dict(d))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Bad memory line: %s", e)
        self._entries = entries
        return entries

    def _flush(self):
        """将内存条目写回磁盘"""
        if self._entries is None:
            return
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in self._entries]
        self._file.write_text("\n".join(lines) + "\n", "utf-8")

    def _sync_to_chroma(self, entry: MemoryEntry):
        """将新条目同步到 ChromaDB（ChromaDB 自动嵌入）"""
        if self._chroma is None:
            return
        try:
            search_text = entry.summary or entry.user_msg
            metadata = {
                "chroma_id": f"mem_{len(self._entries)}",
                "timestamp": entry.timestamp,
                "emotion": entry.emotion,
                "validated_count": entry.validated_count,
                "confidence": entry.confidence,
                "source": entry.source,
            }
            self._chroma.add(search_text, metadata)
        except Exception as e:
            logger.warning("Chroma sync failed: %s", e)

    # ── 公开接口 ──

    def add(self, user_msg: str, bot_reply: str, summary: str = "",
            emotion: str = "neutral", confidence: float = 0.7,
            source: str = "dialogue") -> MemoryEntry:
        """添加一条记忆。

        Args:
            user_msg: 用户消息原文
            bot_reply: 角色回复原文
            summary: 可选的一句话摘要（不填则用 user_msg 前 20 字）
            emotion: 情感标签
            confidence: 置信度（0~1）
            source: 来源类型（dialogue/observation/system）

        Returns:
            新创建的 MemoryEntry
        """
        if not summary:
            summary = user_msg[:20] + ("…" if len(user_msg) > 20 else "")

        entry = MemoryEntry(
            user_msg=user_msg,
            bot_reply=bot_reply,
            summary=summary,
            timestamp=datetime.now().isoformat(),
            emotion=emotion,
            confidence=confidence,
            source=source,
        )

        entries = self._load()
        entries.append(entry)
        self._flush()

        # 异步同步到 ChromaDB
        self._sync_to_chroma(entry)

        # 自动压缩检查（P0）
        try:
            self._compressor.check_and_compress(entries)
        except Exception as e:
            logger.warning("Auto-compress failed: %s", e)

        return entry

    def get_recent(self, n: int = 5) -> list[MemoryEntry]:
        """返回最近 N 条记忆（按时时间倒序）"""
        entries = self._load()
        return entries[-n:][::-1]

    def search(self, keyword: str, limit: int = 10) -> list[MemoryEntry]:
        """关键词搜索（匹配 user_msg 和 summary）。"""
        kw = keyword.lower()
        entries = self._load()
        matched = [
            e for e in entries
            if kw in e.user_msg.lower() or kw in e.summary.lower()
        ]
        return matched[-limit:][::-1]

    def search_semantic(self, query: str, limit: int = 5) -> list[dict]:
        """语义搜索，返回 ChromaDB 匹配结果。

        Returns:
            [{text, metadata: {...}, distance}, ...]
        """
        if self._chroma is None:
            return []
        return self._chroma.search(query, limit=limit)

    def count(self) -> int:
        """记忆总数"""
        return len(self._load())

    def count_chroma(self) -> int:
        """ChromaDB 中的条目数"""
        if self._chroma is None:
            return 0
        return self._chroma.count()

    def format_recent(self, n: int = 5, max_chars: int = 500) -> str:
        """格式化最近 N 条记忆为可注入 prompt 的文本。"""
        entries = self.get_recent(n)
        if not entries:
            return ""

        lines = ["之前我们聊过："]
        total = 0
        for e in entries:
            line = f"- {e.summary}"
            if total + len(line) > max_chars:
                omitted = len(entries) - len(lines) + 1
                lines.append(f"… 还有 {omitted} 条未显示")
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)

    def format_semantic(self, query: str, limit: int = 5, max_chars: int = 500) -> str:
        """根据查询语义格式化匹配的记忆。"""
        if self._chroma is None:
            return ""
        results = self._chroma.search(query, limit=limit)
        if not results:
            return ""

        lines = [f"关于「{query}」的回忆："]
        total = 0
        for r in results:
            text = r.get("text", "")[:100]
            meta = r.get("metadata", {})
            emotion = meta.get("emotion", "neutral")
            line = f"- {text}"
            if total + len(line) > max_chars:
                lines.append(f"… 还有未显示结果")
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)

    def format_context(self, n_recent: int = 5, n_compressed: int = 3, max_chars: int = 700) -> str:
        """组合上下文：压缩摘要 + 最近对话。优先使用压缩摘要，再补充最近 raw 对话。"""
        parts = []

        # 压缩摘要
        compressed_text = self._compressor.format_compressed(n=n_compressed)
        if compressed_text:
            parts.append(compressed_text)

        # 最近对话
        recent = self.get_recent(n_recent)
        if recent:
            recent_lines = ["【最近对话】"]
            total = sum(len(p) for p in parts)
            for e in recent:
                line = f"- {e.summary}"
                if total + len(line) > max_chars:
                    break
                recent_lines.append(line)
                total += len(line)
            parts.append("\n".join(recent_lines))

        return "\n\n".join(parts)

    def count_compressed(self) -> int:
        """压缩记录总数"""
        return self._compressor.count_compressed()

    def get_compressed(self, n: int = 3) -> list:
        """获取最近 N 条压缩记录"""
        return self._compressor.get_recent_compressed(n)

    def compact(self, keep_recent: int = 100) -> int:
        """压缩记忆：只保留最近 N 条，返回删除的条数。"""
        entries = self._load()
        if len(entries) <= keep_recent:
            return 0

        removed = len(entries) - keep_recent
        self._entries = entries[-keep_recent:]
        self._flush()
        logger.info("Memory compacted: removed %d, kept %d",
                     removed, keep_recent)
        return removed

    def clear(self):
        """清空所有记忆"""
        self._entries = []
        self._flush()

    def close(self):
        """关闭资源"""
        if self._chroma:
            try:
                self._chroma.close()
            except Exception:
                pass
