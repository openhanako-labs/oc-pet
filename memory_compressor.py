"""对话记忆压缩引擎 — 阈值触发 + summarizer 管线

当 raw 记忆条目数超过阈值时自动触发压缩：
  1. 读取最近 N 条未压缩的对话
  2. 用 summarizer（优先 LLM，降级 extractive）生成摘要
  3. 写入 compressed.jsonl
  4. 标记已压缩

检索时使用：compressed 摘要 + 最近 5 条 raw 对话
"""
from __future__ import annotations

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from memory_store import MemoryEntry

logger = logging.getLogger(__name__)

# ── 默认阈值 ───────────────────────────────────────────

DEFAULT_THRESHOLD = 50   # 触发压缩的未压缩条目数
KEEP_RECENT_RAW = 5       # 压缩后保留的最近 raw 条目数


# ── 压缩条目结构 ───────────────────────────────────────

@dataclass
class CompressedEntry:
    """单条压缩记忆"""
    compressed_id: str           # cmp_001
    character: str               # 角色 ID
    source_range: list[str]      # 源时间范围 ["start_iso", "end_iso"]
    summary: str                 # LLM 生成摘要
    key_points: list[str]        # 核心要点
    dialogue_count: int          # 源对话条数
    compressed_at: str           # 压缩时间
    confidence: float            # 压缩置信度 0~1


# ── 压缩引擎 ────────────────────────────────────────────

class CompressionEngine:
    """压缩引擎，管理阈值检测和压缩执行。

    Args:
        compressed_dir: 存储 compressed.jsonl 的目录
        character_id: 角色 ID
        summarizer: 可选 callable(entries_text: str) -> (summary, key_points, confidence)
                    默认使用 extractive 降级
        threshold: 触发压缩的未压缩条目数
    """

    def __init__(
        self,
        compressed_dir: Path,
        character_id: str,
        summarizer: Callable | None = None,
        threshold: int = DEFAULT_THRESHOLD,
    ):
        self._character_id = character_id
        self._compressed_dir = compressed_dir
        self._compressed_file = compressed_dir / "compressed.jsonl"
        self._threshold = threshold
        self._summarizer = summarizer

        compressed_dir.mkdir(parents=True, exist_ok=True)

        # 已压缩标记集：记录已压缩的原始条目索引
        self._compressed_indices: set[int] = set()
        self._load_compressed_indices()

        logger.info(
            "CompressionEngine ready | char=%s | threshold=%d | file=%s",
            character_id, threshold, self._compressed_file,
        )

    # ── 内部 ──

    def _load_compressed_indices(self):
        """从 compressed.jsonl 读取已压缩标记，重建索引集合"""
        self._compressed_indices.clear()
        if not self._compressed_file.exists():
            return
        for line in self._compressed_file.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                self._compressed_indices.update(d.get("compressed_indices", []))
            except Exception:
                pass

    def _get_next_id(self) -> str:
        """生成下一个压缩 ID"""
        existing = self._load_compressed()
        return f"cmp_{len(existing) + 1:03d}"

    def _load_compressed(self) -> list[CompressedEntry]:
        """加载全部压缩条目"""
        entries: list[CompressedEntry] = []
        if not self._compressed_file.exists():
            return entries
        for line in self._compressed_file.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                entries.append(CompressedEntry(**d))
            except Exception:
                pass
        return entries

    def _append_compressed(self, entry: CompressedEntry, compressed_indices: list[int]):
        """追加一条压缩记录"""
        record = asdict(entry)
        record["compressed_indices"] = compressed_indices
        with open(self._compressed_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._compressed_indices.update(compressed_indices)

    # ── 公开接口 ──

    def check_and_compress(self, entries: list[MemoryEntry]) -> CompressedEntry | None:
        """检查是否需要压缩，如果达到阈值则执行。

        Args:
            entries: MemoryStore 的全部条目列表

        Returns:
            新创建的 CompressedEntry（如果触发了压缩），否则 None
        """
        # 找到未压缩的条目（索引不在 _compressed_indices 中）
        uncompressed = []
        uncompressed_indices = []
        for i, e in enumerate(entries):
            if i not in self._compressed_indices:
                uncompressed.append(e)
                uncompressed_indices.append(i)

        if len(uncompressed) < self._threshold:
            return None

        # 取最近 threshold 条未压缩条目进行压缩
        to_compress = uncompressed[-self._threshold:]
        to_compress_indices = uncompressed_indices[-self._threshold:]

        # 压缩
        summary, key_points, confidence = self._compress_dialogues(to_compress)

        # 时间范围
        timestamps = [
            e.timestamp for e in to_compress if e.timestamp
        ]
        source_range = [
            timestamps[0] if timestamps else "",
            timestamps[-1] if timestamps else "",
        ]

        entry = CompressedEntry(
            compressed_id=self._get_next_id(),
            character=self._character_id,
            source_range=source_range,
            summary=summary,
            key_points=key_points,
            dialogue_count=len(to_compress),
            compressed_at=datetime.now().isoformat(),
            confidence=confidence,
        )

        self._append_compressed(entry, to_compress_indices)
        logger.info(
            "Compressed %d dialogues -> '%s' (confidence=%.2f)",
            len(to_compress), summary[:40], confidence,
        )
        return entry

    def _compress_dialogues(self, entries: list[MemoryEntry]) -> tuple[str, list[str], float]:
        """压缩对话列表为摘要。

        优先使用外部 summarizer（LLM），不可用时降级为 extractive 策略。

        Returns:
            (summary_text, key_points_list, confidence)
        """
        # 拼接对话文本
        dialogue_text = "\n".join(
            f"用户: {e.user_msg}\n角色: {e.bot_reply}"
            for e in entries[-20:]  # 最多取 20 条做摘要（受 token 限制）
        )

        if self._summarizer:
            try:
                result = self._summarizer(dialogue_text)
                if isinstance(result, tuple) and len(result) == 3:
                    return result
                elif isinstance(result, str):
                    return result, [], 0.8
            except Exception as e:
                logger.warning("Summarizer failed, falling back: %s", e)

        # 降级：extractive — 提取高频词 + 最后一条对话概要
        return self._extractive_summarize(entries)

    def _extractive_summarize(self, entries: list[MemoryEntry]) -> tuple[str, list[str], float]:
        """Extractive 降级摘要 — 提取最后 N 条对话概要。

        Returns:
            (summary_text, key_points_list, confidence=0.5)
        """
        if not entries:
            return "", ["无对话记录"], 0.0

        # 提取最后 3 条作为 key points
        recent = entries[-3:] if len(entries) >= 3 else entries
        key_points = []
        for e in recent:
            msg = e.user_msg[:30] + ("…" if len(e.user_msg) > 30 else "")
            key_points.append(msg)

        summary = f"记录了 {len(entries)} 条对话。最近提到：{key_points[-1]}" if key_points else "暂无对话记录"
        return summary, key_points, 0.5

    # ── 检索 ──

    def get_recent_compressed(self, n: int = 3) -> list[CompressedEntry]:
        """返回最近 N 条压缩记录"""
        entries = self._load_compressed()
        return entries[-n:][::-1]

    def format_compressed(self, n: int = 3, max_chars: int = 500) -> str:
        """格式化最近 N 条压缩摘要为可注入 prompt 的文本"""
        entries = self.get_recent_compressed(n)
        if not entries:
            return ""

        lines = ["【长期记忆摘要】"]
        total = 0
        for e in entries:
            text = f"- {e.summary}（共{e.dialogue_count}条对话）"
            if total + len(text) > max_chars:
                break
            lines.append(text)
            total += len(text)

        return "\n".join(lines)

    def count_compressed(self) -> int:
        """压缩记录总数"""
        return len(self._load_compressed())

    @property
    def threshold(self) -> int:
        return self._threshold