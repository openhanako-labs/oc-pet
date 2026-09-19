"""场景记忆 — 场景表 CRUD + D 检索（混合召回）+ E 跨场景联想

（P0-3 升级：config memory.hybrid_bm25=True 时 find_matching 走 HybridMemoryRecall
的 CJK 2/3-gram BM25 + RRF 混合召回，中文/繁简友好；无 embedding 自动退化为
BM25-only；关闭/失败时回退旧精确标签匹配，行为不变）

把 EventStream 聚类出的 Scene 持久化到 `~/.oc-pet/memory/<agent_id>_scenes.json`，
供：
- D 场景回忆台词：find_matching 命中历史场景 → proactive 出带记忆的台词
- E 跨场景联想：associate 按标签交集（情绪/时段/相邻分类）找关联场景
- C 收盘聚类：rebuild 全量重聚 + 合并旧场景计数（幂等）

文件格式：
    {"version": 1, "agent_id": "...", "scenes": [ {Scene 字段...}, ... ]}

兼容性：
- 文件不存在 → load() 空档，零行为
- 文件损坏 → load() 空档 + warning（防御式编程）
- 场景表是**新文件**，不写旧 `<agent_id>.json`
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .perception.scene_cluster import Scene, cluster_events

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = Path.home() / ".oc-pet" / "memory"

SCENES_FILE_VERSION = 1
DEFAULT_MAX_DAYS = 90        # 场景保留天数上限
DEFAULT_MAX_SCENES = 500     # 场景条数上限

# E 联想：分类"相邻"关系（跨场景联想的保守规则之一）
CATEGORY_NEIGHBORS: dict[str, set[str]] = {
    "gaming": {"video_watching", "entertainment"},
    "video_watching": {"gaming", "entertainment"},
    "entertainment": {"gaming", "video_watching"},
    "development": {"writing", "work", "learn"},
    "writing": {"development", "work"},
    "work": {"development", "writing", "learn"},
    "learn": {"development", "work"},
    "communication": {"chat_idle"},
    "chat_idle": {"communication"},
}


def _scene_from_dict(data: dict) -> Scene | None:
    """从 dict 安全构造 Scene（缺字段兜底，损坏跳过）。"""
    try:
        return Scene(
            scene_id=str(data.get("scene_id", "")),
            label=str(data.get("label", "") or ""),
            category=str(data.get("category", "") or ""),
            scenario=str(data.get("scenario", "") or ""),
            tags=list(data.get("tags", []) or []),
            first_ts=float(data.get("first_ts", 0.0) or 0.0),
            last_ts=float(data.get("last_ts", 0.0) or 0.0),
            count=int(data.get("count", 0) or 0),
            duration_min=float(data.get("duration_min", 0.0) or 0.0),
            emotion_summary=str(data.get("emotion_summary", "neutral") or "neutral"),
            topics=list(data.get("topics", []) or []),
        )
    except Exception as e:
        logger.warning("SceneMemory 坏场景跳过: %s", e)
        return None


class SceneMemory:
    """场景表管理：load/save/rebuild/find_matching/recent_scenes/associate/prune"""

    def __init__(self, agent_id: str, memory_dir: str | Path | None = None):
        self._agent_id = agent_id
        self._dir = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
        self._path = self._dir / f"{agent_id}_scenes.json"
        self._scenes: list[Scene] = []
        self._lock = threading.Lock()
        # P0-3 混合召回（懒加载；可被 set_hybrid_recall 注入/替换）
        self._hybrid = None
        self._hybrid_failed = False
        self.load()

    # ── 属性 ──

    @property
    def path(self) -> Path:
        return self._path

    @property
    def scenes(self) -> list[Scene]:
        """当前场景列表（读时复制，线程安全）。"""
        with self._lock:
            return list(self._scenes)

    # ── 持久化 ──

    def load(self) -> None:
        """从磁盘加载场景；文件不存在/损坏用空档。"""
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_scenes = data.get("scenes", []) if isinstance(data, dict) else []
            scenes = []
            for raw in raw_scenes:
                s = _scene_from_dict(raw)
                if s is not None:
                    scenes.append(s)
            with self._lock:
                self._scenes = scenes
        except Exception as e:
            logger.warning("SceneMemory 加载失败（用空档）: %s", e)
            with self._lock:
                self._scenes = []

    def save(self) -> None:
        """写回磁盘（幂等）。"""
        try:
            with self._lock:
                scenes = list(self._scenes)
            data = {
                "version": SCENES_FILE_VERSION,
                "agent_id": self._agent_id,
                "scenes": [asdict(s) for s in scenes],
            }
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("SceneMemory 保存失败: %s", e)

    # ── C：重建 ──

    def rebuild(self, events: list[dict]) -> int:
        """全量重聚 + 合并旧场景计数（幂等）。

        幂等性：scene_id 由 category|scenario|first_date 决定，同一批事件
        重复 rebuild 得到相同场景；同 scene_id 已存在时合并话题（去重取并集）、
        保留较大 count（聚合事件数），不会重复累加。

        Args:
            events: 事件流（read_events(30) 的输出）

        Returns:
            重建后的场景条数
        """
        new_scenes = cluster_events(events or [])
        with self._lock:
            old = {s.scene_id: s for s in self._scenes}
        merged: list[Scene] = []
        seen_ids: set[str] = set()
        for s in new_scenes:
            old_scene = old.get(s.scene_id)
            if old_scene is not None:
                # 合并话题（旧 + 新，去重，最多 5 条）；count 取较大值（幂等）
                topics = list(dict.fromkeys((old_scene.topics or []) + (s.topics or [])))[:5]
                merged.append(Scene(
                    scene_id=s.scene_id,
                    label=s.label,
                    category=s.category,
                    scenario=s.scenario,
                    tags=s.tags,
                    first_ts=min(s.first_ts, old_scene.first_ts),
                    last_ts=max(s.last_ts, old_scene.last_ts),
                    count=max(s.count, old_scene.count),
                    duration_min=max(s.duration_min, old_scene.duration_min),
                    emotion_summary=s.emotion_summary,
                    topics=topics,
                ))
            else:
                merged.append(s)
            seen_ids.add(s.scene_id)
        # 保留不在本次事件窗口内的旧场景（如 30 天窗口外的历史，由 prune 清理）
        for sid, s in old.items():
            if sid not in seen_ids:
                merged.append(s)
        merged.sort(key=lambda x: (x.first_ts, x.scene_id))
        with self._lock:
            self._scenes = merged
        self.save()
        logger.info("SceneMemory rebuild: %d scenes (agent=%s)", len(merged), self._agent_id)
        return len(merged)

    # ── D：检索 ──

    # ── P0-3 混合召回接入 ──

    def set_hybrid_recall(self, hybrid) -> None:
        """注入 HybridMemoryRecall 实例（测试/外部配置用）。

        Args:
            hybrid: core.memory_hybrid.HybridMemoryRecall 或 None（恢复默认懒加载）。
        """
        self._hybrid = hybrid
        self._hybrid_failed = False

    def _get_hybrid(self):
        """懒加载 HybridMemoryRecall（读 config memory.hybrid_bm25）。

        Returns:
            HybridMemoryRecall 实例；初始化失败返回 None（调用方回退精确匹配）。
        """
        if self._hybrid is not None or self._hybrid_failed:
            return self._hybrid
        try:
            from .memory_hybrid import HybridMemoryRecall, default_score_patch
            self._hybrid = HybridMemoryRecall(score_patch=default_score_patch())
        except Exception as e:
            logger.warning("SceneMemory 混合召回初始化失败（用精确匹配）: %s", e)
            self._hybrid_failed = True
            self._hybrid = None
        return self._hybrid

    @staticmethod
    def _scene_to_doc(s: Scene) -> dict:
        """把 Scene 转成混合召回候选 doc。

        text 聚合 label（中文标签）/scenario/category/tags/topics——这样
        "深夜加班"（中文 label）与 "late_night_work"（scenario 键）都能命中。
        """
        parts = [s.label, s.scenario, s.category]
        parts.extend(s.tags or [])
        parts.extend(s.topics or [])
        return {
            "id": s.scene_id,
            "text": " ".join(str(p) for p in parts if p),
            "label": s.label,
            "scenario": s.scenario,
            "category": s.category,
        }

    @staticmethod
    def _exact_score(s: Scene, category: str, scenario: str,
                     tag_set: set[str]) -> int:
        """旧精确匹配评分（3/2/2/1 规则，保持不变）。"""
        score = 0
        if category and s.category == category:
            score += 2
        if scenario and s.scenario == scenario:
            score += 2
        if category and scenario and s.category == category and s.scenario == scenario:
            score += 1
        if tag_set and (tag_set & set(s.tags)):
            score += 1
        return score

    def _exact_ranked(self, scenes: list[Scene], category: str, scenario: str,
                      tag_set: set[str]) -> list[Scene]:
        """旧精确匹配路径：按 (评分降序, last_ts 降序) 排序，只保留有分场景。"""
        scored: list[tuple[int, Scene]] = []
        for s in scenes:
            score = self._exact_score(s, category, scenario, tag_set)
            if score <= 0:
                continue
            scored.append((score, s))
        scored.sort(key=lambda x: (-x[0], -x[1].last_ts))
        return [s for _, s in scored]

    def find_matching(self, category: str, scenario: str, tags: list[str],
                      max_results: int = 3) -> list[Scene]:
        """按当前感知状态检索历史场景（D 回忆用）。

        评分规则：
          - **混合路径（P0-3）**：config memory.hybrid_bm25=True 时，
            用 CJK 2/3-gram BM25 + RRF 对"label+scenario+category+tags+topics"
            文本召回，按 RRF 得分排序；混合命中不足时用精确匹配补齐。
          - **精确路径（回退）**：category+scenario 都命中 → 3 分；
            category 命中 → 2 分；scenario 命中 → 2 分；标签交集 ≥1 → 1 分。
        两种路径都排除 last_ts 距今 < 5 分钟的"进行中"场景（避免回忆刚发生）。

        Args:
            category: 当前前台分类
            scenario: 当前意图场景名（可空）
            tags: 当前标签列表 [category, scenario, period, emotion]
            max_results: 返回条数上限

        Returns:
            按评分/时间排序的 Scene 列表（最多 max_results 条）
        """
        if max_results <= 0:
            return []
        now = time.time()
        category = category or ""
        scenario = scenario or ""
        tag_set = set(tags or [])
        # 排除进行中场景（5 分钟内）——两条路径共用同一过滤
        eligible = [s for s in self.scenes if s.last_ts <= now - 300]
        if not eligible:
            return []

        hybrid = self._get_hybrid()
        if hybrid is not None and hybrid.enabled:
            query = " ".join([category, scenario, " ".join(tags or [])]).strip()
            if query:
                pool = [self._scene_to_doc(s) for s in eligible]
                try:
                    hits = hybrid.recall(query, pool)
                except Exception as e:
                    logger.warning("SceneMemory 混合召回失败（回退精确匹配）: %s", e)
                    hits = []
                by_id = {d["id"]: d for d in hits}
                scene_by_id = {s.scene_id: s for s in eligible}
                scored: list[tuple[float, int, Scene]] = []
                for d in hits:
                    s = scene_by_id.get(d.get("id") or "")
                    if s is None:
                        continue
                    # 混合 RRF 得分主序，精确得分作次级（同分时标签命中优先）
                    scored.append((
                        float(d.get("_rrf_score", 0.0) or 0.0),
                        self._exact_score(s, category, scenario, tag_set),
                        s,
                    ))
                scored.sort(key=lambda x: (-x[0], -x[1], -x[2].last_ts))
                results = [s for _, _, s in scored[:max_results]]
                # 混合命中不足 → 精确匹配补齐（不重复返回）
                if len(results) < max_results:
                    seen = {s.scene_id for s in results}
                    for s in self._exact_ranked(eligible, category, scenario, tag_set):
                        if s.scene_id in seen:
                            continue
                        results.append(s)
                        seen.add(s.scene_id)
                        if len(results) >= max_results:
                            break
                return results

        # 旧精确匹配路径（hybrid 关闭/失败/空 query）
        return self._exact_ranked(eligible, category, scenario, tag_set)[:max_results]

    def recent_scenes(self, n: int = 5) -> list[Scene]:
        """最近 n 条场景（按 last_ts 降序）。"""
        scenes = sorted(self.scenes, key=lambda s: s.last_ts, reverse=True)
        return scenes[:max(0, n)]

    # ── E：跨场景联想 ──

    def associate(self, current: dict, scenes: list[Scene]) -> Scene | None:
        """跨场景联想：标签交集（情绪/时段/相邻分类）保守规则。

        规则：
          1. 候选 = 与 current 有标签交集的场景（emotion 相同 OR period 相同
             OR category 相邻）
          2. 排除同 scene_id 直匹配（那是 D 的活）
          3. 取最近一条返回；无候选返回 None

        Args:
            current: {"category", "scenario", "emotion", "period"}
            scenes: 候选场景池（通常 recent_scenes(20)）

        Returns:
            关联到的 Scene 或 None
        """
        if not scenes or not current:
            return None
        category = current.get("category", "") or ""
        scenario = current.get("scenario", "") or ""
        emotion = current.get("emotion", "") or ""
        period = current.get("period", "") or ""
        neighbors = CATEGORY_NEIGHBORS.get(category, set())
        # 直接匹配的 scene_id（D 的活）：排除
        direct_id = f"{category}|{scenario or 'unknown'}"
        candidates: list[Scene] = []
        for s in scenes:
            # 排除同场景直匹配
            if direct_id and s.scene_id.startswith(direct_id):
                continue
            # 排除"当前正在发生"（5 分钟内）
            if s.last_ts > time.time() - 300:
                continue
            hit = False
            if emotion and emotion != "neutral" and emotion == s.emotion_summary:
                hit = True
            if period and period != "other" and period in s.tags:
                hit = True
            if category and s.category in neighbors:
                hit = True
            if hit:
                candidates.append(s)
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.last_ts, reverse=True)
        return candidates[0]

    # ── 裁剪 ──

    def prune(self, max_days: int = DEFAULT_MAX_DAYS,
              max_scenes: int = DEFAULT_MAX_SCENES) -> int:
        """裁剪超限场景：超过 max_days 天或超过 max_scenes 条。

        Returns:
            删除条数
        """
        cutoff = time.time() - float(max_days) * 86400.0
        with self._lock:
            kept = [s for s in self._scenes if s.last_ts >= cutoff]
            removed = len(self._scenes) - len(kept)
            if len(kept) > max_scenes:
                kept = sorted(kept, key=lambda s: s.last_ts, reverse=True)[:max_scenes]
                removed += len(self._scenes) - len(kept) - removed
            self._scenes = kept
        if removed > 0:
            self.save()
            logger.info("SceneMemory prune: removed=%d kept=%d (%s)",
                        removed, len(kept), self._path.name)
        return removed


__all__ = ["SceneMemory", "CATEGORY_NEIGHBORS"]
