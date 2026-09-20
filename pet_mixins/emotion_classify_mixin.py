"""情绪分类接线：把文本情绪分类接进桌宠的两个位置。

## 两个视角（2026-09-20 决策 C）

| 视角 | 输入 | 用在哪 | 实测命中 |
|---|---|---|---|
| ``user`` | 用户消息原文 | 让桌宠知道用户此刻什么情绪 | 11/14 = 79% |
| ``pet`` | 桌宠回复文本 | 驱动桌宠自己的表情 | 12/18 = 67% |

两者**共用一套算法与语料**，差异只在 ``extra_corpus``：
桌宠视角额外加载 ``core/data/emotion_corpus_pet.json``
（语料是第一人称倾诉，而桌宠说的是第二人称嘱咐，分布错配实测掉到 33%）。

## 为什么必须走后台线程

分类要打 embedding API（实测单次 ~100-300ms，偶发超时）。
而两个接入点都跑在**主线程**：

- ``_send_message`` 是 Qt 按钮/回车回调
- ``_do_engine_reply`` 经 ``engine_reply_signal`` 排到主线程

之前踩过的坑：``expression_director.decide()`` 同步阻塞跑在主线程，
引擎卡死时每次回复卡满 8 秒。**同类错误不再犯第二次。**

所以：分类丢进 ``QThreadPool``，结果经信号回主线程。
分类未就绪 / 失败 → 静默跳过，**不阻塞、不报错**。

## 与既有 VA 路径的关系

不替换、不抢：

- 桌宠回复：分类结果**优先**驱动表情（比 VA 坐标更有语义）；
  分类不可用时回退到既有的 ``_direct_expression_from_va``。
- 用户消息：只写 ``_last_user_emotion`` 供行为层参考，不直接改表情
  （用户的情绪不该立刻改变桌宠的脸——那会让桌宠显得没有自我）。
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 分类器输出（14 类）→ 决策器认的英文键。
#
# ⚠ 映射目标必须是 ``perception_mixin._EMOTION_ZH`` 里**真实存在的键**，
# 否则 ``_direct_expression`` 会静默 return —— 那就是「链路断了但没人发现」。
# 现有键：angry / cute / happy / neutral / sad / shy / surprised / thinking
# （本模块同时给 _EMOTION_ZH 补了 confused / sleepy，见 perception_mixin）
_CLASSIFIER_TO_DIRECTOR: dict[str, str] = {
    "neutral": "neutral",
    "calm": "neutral",
    "happy": "happy",
    "excited": "happy",
    "shy": "shy",
    "affectionate": "happy",
    "curious": "thinking",
    "concerned": "sad",       # 关心 → 柔和的关注（决策器无「关心」组）
    "confused": "confused",
    "surprised": "surprised",
    "tired": "sleepy",
    "sad": "sad",
    "anxiety": "sad",
    "anger": "angry",
    "angry": "angry",         # 别名（语料里两种写法都出现过）
}

# 置信度低于此值不采纳（宁可不动，不能乱动）
_MIN_CONFIDENCE = 0.45


def _load_pet_corpus() -> dict[str, list[str]]:
    """加载桌宠视角补充语料（失败返回空，不阻断）。

    与主语料同在 ``core/emotion_data/``（源码资产，随仓库分发）。
    """
    p = os.path.join(ROOT, "core", "emotion_data", "emotion_corpus_pet.json")
    try:
        data = json.load(open(p, encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:  # noqa: BLE001
        logger.debug("桌宠视角语料加载失败：%s", exc)
        return {}


class EmotionClassifyMixin:
    """情绪分类接线（由 PetWindow 继承）。"""

    # ── 初始化 ──────────────────────────────────────────────
    def _init_emotion_classifier(self) -> None:
        """构造两个视角的分类器（延迟 prepare，不在启动时打网络）。"""
        self._emotion_classifiers: dict[str, object] = {}
        self._last_user_emotion: str = ""
        self._last_user_confidence: float = 0.0
        self._classify_ready = False
        self._classify_prepare_started = False

        cfg = {}
        try:
            cfg = (self.config.get("emotion_classifier") or {})
        except Exception:  # noqa: BLE001
            cfg = {}
        self._classify_enabled = bool(cfg.get("enabled", True))
        self._classify_min_conf = float(cfg.get("min_confidence", _MIN_CONFIDENCE))

        if not self._classify_enabled:
            logger.info("情绪分类器已关闭（config.emotion_classifier.enabled=false）")
            return
        logger.info("情绪分类器已启用（首次使用前会在后台预热）")

    def _ensure_classifier(self, view: str):
        """取（或创建）某视角的分类器。失败返回 None。"""
        if not getattr(self, "_classify_enabled", False):
            return None
        cache = getattr(self, "_emotion_classifiers", None)
        if cache is None:
            return None
        if view in cache:
            return cache[view]
        try:
            from core.emotion_classifier import get_classifier
            extra = _load_pet_corpus() if view == "pet" else None
            clf = get_classifier(view, extra_corpus=extra)
            cache[view] = clf
            return clf
        except Exception as exc:  # noqa: BLE001
            logger.debug("取情绪分类器失败（%s）：%s", view, exc)
            return None

    # ── 后台预热 ────────────────────────────────────────────
    def _warmup_classifier_async(self) -> None:
        """后台预热分类器（首次要嵌 1400+ 条语料，会慢）。

        必须在后台：首次嵌 1400 条实测要 1-3 分钟，主线程等不起。
        只在首次真正需要时触发一次。
        """
        if getattr(self, "_classify_prepare_started", False):
            return
        if not getattr(self, "_classify_enabled", False):
            return
        self._classify_prepare_started = True

        clf = self._ensure_classifier("pet")
        if clf is None:
            return
        if getattr(clf, "_ready", False):
            self._classify_ready = True
            return

        try:
            from PySide6.QtCore import QThreadPool, QRunnable
        except Exception:  # noqa: BLE001
            return

        owner = self

        class _Task(QRunnable):
            def run(self_inner):
                try:
                    ok = clf.prepare()
                    owner._classify_ready = bool(ok)
                    logger.info("情绪分类器预热%s", "完成" if ok else "失败")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("情绪分类器预热异常：%s", exc)

        try:
            QThreadPool.globalInstance().start(_Task())
        except Exception as exc:  # noqa: BLE001
            logger.debug("提交预热任务失败：%s", exc)

    # ── 分类（同步，调用方负责不在主线程跑）───────────────
    def _classify_sync(self, text: str, view: str = "pet"):
        """同步分类（**不要在 Qt 主线程直接调**）。失败返回 None。"""
        clf = self._ensure_classifier(view)
        if clf is None:
            return None
        try:
            return clf.classify(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("情绪分类异常：%s", exc)
            return None

    # ── 桌宠回复：分类 → 情绪词 ─────────────────────────────
    def _resolved_reply_emotion(self) -> str:
        """取分类器给桌宠回复判定的情绪（英文词），没有则返回空串。

        ## 为什么这是接线的主干

        主链路上 ``emotion`` 变量**恒为 "neutral"**（实测：``[emotion:]`` 标签
        只有兜底才补，补的就是 neutral）。而它有多个消费者：

        - ``set_emotion_expression_only(emotion)`` —— 面部表情层
        - ``_set_anim_seq(body_anim, emotion=emotion, ...)`` —— 身体动作过渡风格
        - ``_current_emotion`` —— 情绪驻留状态

        分类器给出的情绪词直接**替换**这个空值，上述三个消费者立即受益，
        **不依赖决策器是否启用**（决策器是另一层，管「从候选里挑具体预设」）。

        Returns:
            英文情绪词（如 ``happy``）；不可用/低置信时返回空串，
            调用方应保留原值不动。
        """
        if not getattr(self, "_classify_enabled", False):
            return ""
        cached = getattr(self, "_pending_reply_emotion", None)
        if not cached:
            return ""
        classifier_emotion, conf = cached
        if not classifier_emotion or conf < getattr(self, "_classify_min_conf", _MIN_CONFIDENCE):
            return ""
        word = _CLASSIFIER_TO_DIRECTOR.get(classifier_emotion, "")
        if not word or word == "neutral":
            return ""
        logger.info("情绪分类（桌宠视角）→ %s（置信 %.2f）", classifier_emotion, conf)
        return word

    def _consume_reply_emotion(self) -> None:
        """消费掉本轮的分类结果（防止下轮误用）。"""
        self._pending_reply_emotion = None

    # ── 后台分类（供调用方在主线程外跑）────────────────────
    def _classify_reply_async(self, reply_text: str) -> None:
        """后台分类桌宠回复，结果存 ``_pending_reply_emotion``。

        **必须在收到回复时就提交**，这样 ``_do_engine_reply`` 跑到主线程时
        结果大概率已就位。未就位就静默跳过——不阻塞、不等。
        """
        if not getattr(self, "_classify_enabled", False) or not reply_text:
            return
        self._warmup_classifier_async()
        try:
            from PySide6.QtCore import QThreadPool, QRunnable
        except Exception:  # noqa: BLE001
            return
        owner = self

        class _Task(QRunnable):
            def run(self_inner):
                r = owner._classify_sync(reply_text, "pet")
                if r is not None and r.emotion:
                    owner._pending_reply_emotion = (r.emotion, r.confidence)

        try:
            QThreadPool.globalInstance().start(_Task())
        except Exception as exc:  # noqa: BLE001
            logger.debug("提交桌宠情绪分类失败：%s", exc)

    # ── 用户消息：分类 → 记状态 ─────────────────────────────
    def _classify_user_async(self, text: str) -> None:
        """后台分类用户消息，结果写 ``_last_user_emotion``。

        为什么不在主线程：要走网络。为什么异步也不改表情：
        用户的情绪不该立刻改变桌宠的脸——那会让桌宠显得没有自我。
        分类结果供行为层/主动搭话参考。
        """
        if not getattr(self, "_classify_enabled", False) or not text:
            return
        try:
            from PySide6.QtCore import QThreadPool, QRunnable
        except Exception:  # noqa: BLE001
            return
        owner = self

        class _Task(QRunnable):
            def run(self_inner):
                r = owner._classify_sync(text, "user")
                if r is None:
                    return
                owner._last_user_emotion = r.emotion
                owner._last_user_confidence = r.confidence
                logger.debug("情绪分类（用户视角）→ %s", r.as_line())

        try:
            QThreadPool.globalInstance().start(_Task())
        except Exception as exc:  # noqa: BLE001
            logger.debug("提交用户情绪分类失败：%s", exc)

    # ── 观测 ────────────────────────────────────────────────
    def emotion_classifier_status(self) -> dict:
        """分类器状态（供 /pet/state 或排障用）。"""
        out = {
            "enabled": getattr(self, "_classify_enabled", False),
            "ready": getattr(self, "_classify_ready", False),
            "last_user_emotion": getattr(self, "_last_user_emotion", ""),
            "last_user_confidence": getattr(self, "_last_user_confidence", 0.0),
            "views": {},
        }
        for view, clf in (getattr(self, "_emotion_classifiers", {}) or {}).items():
            try:
                out["views"][view] = clf.status()
            except Exception:  # noqa: BLE001
                out["views"][view] = {"error": "status() 失败"}
        return out
