"""M3 记忆快照管理器 — 导出/导入 Agent 记忆数据

职责：
1. 将 ~/.hanako/agents/<agent_id>/memory/ 下的记忆文件打包为 JSON 快照
2. 从 JSON 快照恢复记忆，支持 overwrite / smart / skip_existing 三种合并策略
3. 附带身份文件（identity.md）和结构化置顶记忆（pinned-memory.json）

依赖：HanakoContext（复用已有的文件读取逻辑）
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .hanako_context import HanakoContext

logger = logging.getLogger(__name__)

# ── 快照中记忆文件的 key → 目标文件名映射 ──

_MEMORY_FILES: dict[str, str] = {
    "memories.recent": "memory.md",
    "memories.today": "today.md",
    "memories.longterm": "longterm.md",
    "memories.week": "week.md",
    "memories.facts": "facts.md",
}

# 额外附带导出的身份/配置文件
_EXTRA_FILES: dict[str, str] = {
    "identity": "identity.md",
    "pinned": "pinned.md",
    "pinned_structured": "pinned-memory.json",
}


# ── 数据模型 ──


@dataclass
class MemorySnapshotMeta:
    """快照元信息"""
    agent_id: str
    version: str = "1.0"
    created_at: str = ""
    created_ts: float = 0.0
    description: str = ""

    def __post_init__(self):
        if not self.created_at:
            now = datetime.now(timezone.utc)
            self.created_at = now.isoformat()
            self.created_ts = now.timestamp()


@dataclass
class MemorySnapshot:
    """完整记忆快照"""
    meta: MemorySnapshotMeta
    memories: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str | None] = field(default_factory=dict)
    extra_json: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 dict（用于 JSON 写入）"""
        return {
            "meta": asdict(self.meta),
            "memories": self.memories,
            "extra": self.extra,
            "extra_json": self.extra_json,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemorySnapshot":
        """从 dict 反序列化"""
        meta_data = data.get("meta", {})
        meta = MemorySnapshotMeta(**meta_data)
        return cls(
            meta=meta,
            memories=data.get("memories", {}),
            extra=data.get("extra", {}),
            extra_json=data.get("extra_json", {}),
        )


# ── 合并策略 ──


def _append_without_duplicates(existing: str, new_content: str) -> str:
    """智能追加：去除重复行后合并"""
    if not existing.strip():
        return new_content
    if not new_content.strip():
        return existing

    existing_lines = set(existing.strip().splitlines())
    new_lines = new_content.strip().splitlines()

    # 保留已有内容，追加新行中不在已有的部分
    appended = [line for line in new_lines if line not in existing_lines]
    if appended:
        separator = "\n" if existing.endswith("\n") or not existing.strip() else "\n\n"
        return existing + separator + "\n".join(appended)
    return existing


# ── 核心类 ──


class MemorySnapshotManager:
    """记忆快照管理器

    用法：
        mgr = MemorySnapshotManager("rebecca")
        mgr.export_snapshot(path="rebecca_snapshot.json")
        mgr.import_snapshot(path="rebecca_snapshot.json", strategy="smart")
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._ctx = HanakoContext(agent_id)
        self._agent_dir = self._ctx._agent_dir  # ~/.hanako/agents/<agent_id>/

    # ── 导出 ──

    def export_snapshot(
        self,
        output_path: str | Path | None = None,
        description: str = "",
    ) -> Path:
        """将 Agent 的记忆打包为 JSON 快照

        Args:
            output_path: 输出文件路径，默认 ~/.hanako/agents/<agent_id>/snapshot_<timestamp>.json
            description: 快照描述

        Returns:
            输出的文件路径
        """
        logger.info("Exporting snapshot for agent '%s'", self.agent_id)

        # 构建快照数据
        snapshot = MemorySnapshot(
            meta=MemorySnapshotMeta(
                agent_id=self.agent_id,
                description=description,
            ),
            extra={},
            extra_json={},
        )

        # 读取记忆文件
        for key, filename in _MEMORY_FILES.items():
            reader_map = {
                "memories.recent": self._ctx.read_memory,
                "memories.today": self._ctx.read_today,
                "memories.longterm": self._ctx.read_longterm,
            }
            reader = reader_map.get(key)
            if reader:
                try:
                    content = reader()
                    if content:
                        snapshot.memories[key] = content
                        logger.debug("  Included %s (%d chars)", key, len(content))
                except Exception as e:
                    logger.warning("  Failed to read %s: %s", filename, e)

        # week.md 需要直接从文件系统读（HanakoContext 没有封装）
        week_path = self._agent_dir / "memory" / "week.md"
        try:
            if week_path.exists():
                content = week_path.read_text("utf-8").strip()
                if content:
                    snapshot.memories["memories.week"] = content
                    logger.debug("  Included memories.week (%d chars)", len(content))
        except Exception as e:
            logger.warning("  Failed to read week.md: %s", e)

        # 读取 facts.db（SQLite 事实库，转为 JSON 文本）
        facts_db = self._agent_dir / "memory" / "facts.db"
        try:
            if facts_db.exists():
                snapshot.extra_json["memories.facts_db"] = self._export_facts_db(facts_db)
        except Exception as e:
            logger.warning("  Failed to export facts.db: %s", e)

        # 读取身份/置顶等附加文件
        for key, filename in _EXTRA_FILES.items():
            filepath = self._agent_dir / filename
            try:
                if filepath.exists():
                    raw = filepath.read_text("utf-8").strip()
                    if raw:
                        if filename == "pinned-memory.json":
                            snapshot.extra_json[f"extra.{key}"] = raw
                        else:
                            snapshot.extra[f"extra.{key}"] = raw
                        logger.debug("  Included %s", key)
            except Exception as e:
                logger.warning("  Failed to read %s: %s", filename, e)

        # 写入 JSON
        if output_path is None:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_path = self._agent_dir / f"snapshot_{ts}.json"
        else:
            output_path = Path(output_path)

        try:
            output_path.write_text(
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Snapshot written to %s", output_path)
            return output_path
        except Exception as e:
            logger.error("Failed to write snapshot: %s", e)
            raise

    def _export_facts_db(self, db_path: Path) -> str:
        """将 SQLite facts.db 导出为 JSON 文本"""
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # 尝试读取 editable_facts 表
            tables = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            def _serialize_row(row: tuple, col_names: list) -> dict:
                """将行数据转为可 JSON 序列化的 dict（处理 bytes 等）"""
                result = {}
                for c, v in zip(col_names, row):
                    if isinstance(v, bytes):
                        try:
                            result[c] = v.decode("utf-8")
                        except UnicodeDecodeError:
                            result[c] = v.hex()
                    elif isinstance(v, (list, dict)):
                        result[c] = json.dumps(v, ensure_ascii=False)
                    else:
                        result[c] = v
                return result

            result_parts = []
            for tbl in table_names:
                rows = cursor.execute(f"SELECT * FROM [{tbl}]").fetchall()
                cols = [desc[0] for desc in cursor.description]
                result_parts.append({
                    "table": tbl,
                    "columns": cols,
                    "rows": [_serialize_row(row, cols) for row in rows],
                })

            conn.close()
            return json.dumps(result_parts, ensure_ascii=False)
        except Exception as e:
            logger.warning("SQLite export failed: %s", e)
            return f"ERROR: {e}"

    # ── 导入 ──

    def import_snapshot(
        self,
        input_path: str | Path,
        strategy: Literal["overwrite", "smart", "skip_existing"] = "smart",
    ) -> dict[str, str]:
        """从 JSON 快照恢复记忆

        Args:
            input_path: 快照 JSON 文件路径
            strategy: 合并策略
                - overwrite: 完全覆盖目标文件
                - smart: 智能合并（去重追加）
                - skip_existing: 跳过已存在的文件

        Returns:
            操作结果统计 {"imported": N, "skipped": N, "errors": N}
        """
        logger.info("Importing snapshot from %s (strategy=%s)", input_path, strategy)
        input_path = Path(input_path)

        if not input_path.exists():
            raise FileNotFoundError(f"Snapshot file not found: {input_path}")

        try:
            data = json.loads(input_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON snapshot: {e}") from e

        snapshot = MemorySnapshot.from_dict(data)
        stats = {"imported": 0, "skipped": 0, "errors": 0}

        memory_dir = self._agent_dir / "memory"

        # 确保 memory 目录存在
        memory_dir.mkdir(parents=True, exist_ok=True)

        # 导入记忆文件
        for key, content in snapshot.memories.items():
            if not content:
                continue

            target_filename = _MEMORY_FILES.get(key)
            if target_filename is None:
                logger.warning("Unknown memory key: %s, skipping", key)
                stats["skipped"] += 1
                continue

            target_path = memory_dir / target_filename

            try:
                if strategy == "overwrite":
                    target_path.write_text(content, encoding="utf-8")
                    logger.info("  Overwrote %s", target_filename)
                    stats["imported"] += 1

                elif strategy == "skip_existing":
                    if target_path.exists():
                        logger.info("  Skipped (exists): %s", target_filename)
                        stats["skipped"] += 1
                    else:
                        target_path.write_text(content, encoding="utf-8")
                        logger.info("  Created %s", target_filename)
                        stats["imported"] += 1

                elif strategy == "smart":
                    if target_path.exists():
                        existing = target_path.read_text(encoding="utf-8")
                        merged = _append_without_duplicates(existing, content)
                        target_path.write_text(merged, encoding="utf-8")
                        logger.info("  Smart-merged %s", target_filename)
                    else:
                        target_path.write_text(content, encoding="utf-8")
                        logger.info("  Created %s", target_filename)
                    stats["imported"] += 1

            except Exception as e:
                logger.error("  Failed to write %s: %s", target_filename, e)
                stats["errors"] += 1

        # 导入附加文件
        for key, content in snapshot.extra.items():
            if not content:
                continue

            file_key = key.split(".", 1)[-1]  # "identity" / "pinned"
            target_filename = _EXTRA_FILES.get(file_key)
            if target_filename is None:
                continue

            target_path = self._agent_dir / target_filename

            try:
                if strategy == "overwrite" or not target_path.exists():
                    target_path.write_text(content, encoding="utf-8")
                    logger.info("  Imported extra: %s", target_filename)
                    stats["imported"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                logger.error("  Failed to write %s: %s", target_filename, e)
                stats["errors"] += 1

        # 导入结构化 JSON 数据
        for key, content in snapshot.extra_json.items():
            if not content:
                continue

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                logger.warning("  Invalid JSON in %s, treating as text", key)
                parsed = content

            # facts_db → 写回 SQLite
            if key == "extra.memories.facts_db" and isinstance(parsed, list):
                try:
                    self._import_facts_db(memory_dir, parsed)
                    stats["imported"] += 1
                    logger.info("  Restored facts.db from snapshot")
                except Exception as e:
                    logger.error("  Failed to restore facts.db: %s", e)
                    stats["errors"] += 1
                continue

            # pinned-memory.json
            if key == "extra.pinned_structured":
                target_path = self._agent_dir / "pinned-memory.json"
                try:
                    if strategy == "overwrite" or not target_path.exists():
                        target_path.write_text(content, encoding="utf-8")
                        stats["imported"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as e:
                    logger.error("  Failed to write pinned-memory.json: %s", e)
                    stats["errors"] += 1
                continue

        logger.info("Import complete: %s", stats)
        return stats

    def _import_facts_db(self, memory_dir: Path, tables_data: list[dict]) -> None:
        """从快照数据恢复 facts.db"""
        import sqlite3

        db_path = memory_dir / "facts.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        for tbl_info in tables_data:
            tbl_name = tbl_info.get("table", "")
            columns = tbl_info.get("columns", [])
            rows = tbl_info.get("rows", [])

            if not tbl_name or not columns:
                continue

            # 创建表
            col_defs = ", ".join(f'[{c}] TEXT' for c in columns)
            cursor.execute(f"DROP TABLE IF EXISTS [{tbl_name}]")
            cursor.execute(f"CREATE TABLE [{tbl_name}] ({col_defs})")

            # 插入数据
            if rows:
                placeholders = ", ".join(["?"] * len(columns))
                for row in rows:
                    values = [row.get(c) for c in columns]
                    cursor.execute(
                        f"INSERT INTO [{tbl_name}] VALUES ({placeholders})",
                        values,
                    )

        conn.commit()
        conn.close()

    # ── 工具方法 ──

    def list_snapshots(self, directory: str | Path | None = None) -> list[dict]:
        """列出指定目录下所有快照文件

        Returns:
            [{"path": ..., "agent_id": ..., "created_at": ...}, ...]
        """
        if directory is None:
            directory = self._agent_dir
        else:
            directory = Path(directory)

        snapshots = []
        try:
            for f in sorted(directory.glob("snapshot_*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    meta = data.get("meta", {})
                    snapshots.append({
                        "path": str(f),
                        "agent_id": meta.get("agent_id", "unknown"),
                        "created_at": meta.get("created_at", ""),
                        "description": meta.get("description", ""),
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Failed to list snapshots in %s: %s", directory, e)

        return snapshots
