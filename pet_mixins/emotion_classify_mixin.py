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
- ``_on_engine_reply`` 经 ``_real_on_reply`` 也在主线程

之前踩过的坑：``expression_director.decide()`` 同步阻塞跑在主线程，
引擎卡死时每次回复卡满 8 秒。**同类错误不再犯第二次。**

## 时序问题（2026-09-20 真机发现）

第一版接线是「提交任务 + 主线程稍后读结果」，**实测不工作**：

```
_on_engine_reply（主线程）
  ├─ 提交分类任务 → QThreadPool（要 100-300ms 走网络）
  └─ emit engine_reply_signal → 主线程事件队列
                                    ↓
                              _do_engine_reply 立刻执行
                                    ↓
                              读 _pending_reply_emotion → 还是 None ✗
```

**信号投递是即时的，而分类要 100-300ms。** 主线程读的时候结果永远还没好。

**修法**：双路。

1. **快路径**：主线程读 `_resolved_reply_emotion()`——若结果已就绪
   （如上一轮的缓存、或分类很快），无延迟应用。
2. **慢路径**：分类任务完成后经 `emotion_classified_signal` 把结果送回主线程，
   `_apply_classified_emotion` 直接驱动表情（滞后 100-300ms，但保证能到）。

这样“结果已就绪”和“结果稍后才到”两种情况都能覆盖。

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
import time

try:  # Qt 可选：无 Qt 环境下模块仍可导入（单测用）
    from PySide6.QtCore import Signal as _QtSignal
except Exception:  # noqa: BLE001
    _QtSignal = None


def _make_signal(*types):
    """构造 Qt 信号；无 Qt 时返回 None。

    为什么信号定义在 mixin 里：`tests/test_architecture_boundary.py`
    有一条护栏「pet.py 的 Qt 引用只降不升」（基线 115）。
    把信号放 mixin，pet.py 不必新增 Qt 引用，护栏不必放宽。
    """
    if _QtSignal is None:
        return None
    try:
        return _QtSignal(*types)
    except Exception:  # noqa: BLE001
        return None

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

# 置信度低于此值不采纳（宁可不动，不能乱动）。
#
# 2026-09-20 阈值扫描定标（tools/verify_emotion_classifier.py 的测试集）：
#     阈值    pet 命中    user 命中
#     0.35    12/18      12/14
#     0.40    12/18      12/14   ← 选它
#     0.45    11/18      12/14   （原值，偏严）
#     0.60    10/18      12/14
# 0.40 与 0.35 同分但更保守；0.45 会误杀 pet 视角 1 条。
# 注：扫到 0.00 命中也不变——说明本测试集里没有「低置信但错」的样本，
# 阈值保护的真实收益在这个小样本上看不出来。不要因此取消阈值。
_MIN_CONFIDENCE = 0.40


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

    # 2026-09-20：分类结果回主线程（后台任务完成 → 驱动表情）。
    # 为什么需要：分类要 100-300ms 走网络，而 engine_reply_signal 是**即时**投递的
    # ——主线程读 _pending_reply_emotion 时结果永远还没好（真机实测到的时序 bug）。
    # 定义在 mixin 而非 pet.py：避免 pet.py 新增 Qt 引用（护栏只降不升）。
    # 签名 (emotion, confidence, vad, intensity)。
    emotion_classified_signal = _make_signal(str, float, object, float)

    # ── 初始化 ──────────────────────────────────────────────
    def _init_emotion_classifier(self) -> None:
        """构造两个视角的分类器（延迟 prepare，不在启动时打网络）。"""
        self._emotion_classifiers: dict[str, object] = {}
        self._last_user_emotion: str = ""
        self._last_user_confidence: float = 0.0
        self._pending_reply_emotion = None
        self._pending_reply_vad_value = None
        self._last_applied_classified = ""
        self._last_applied_classified_at = 0.0
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
        self._pending_reply_vad_value = None

    def _pending_reply_vad(self) -> tuple[float, float, float] | None:
        """取分类器给桌宠回复判定的连续 VAD（没就绪返回 None）。

        ## 为什么单独一个入口

        渲染器只需**连续 VA 坐标**（`_va_target` → 每帧指数平滑 → 参数）。
        分类器正好产出连续 VAD —— 不用查 `_EMOTION_VA` 表
        （那张表只有 7 个情绪，而分类器有 14 类）。

        这是 VA 坐标的**正确来源**：不是把离散情绪名查表成坐标
        （那是「二维承载不了细粒度」的老路），而是从文本直接分类出的连续值。

        Returns:
            ``(valence, arousal, dominance)``；不可用/低置信返回 None。
        """
        if not getattr(self, "_classify_enabled", False):
            return None
        cached = getattr(self, "_pending_reply_emotion", None)
        if not cached:
            return None
        _emotion, conf = cached
        if conf < getattr(self, "_classify_min_conf", _MIN_CONFIDENCE):
            return None
        vad = getattr(self, "_pending_reply_vad_value", None)
        return vad if vad else None

    # ── 后台分类（供调用方在主线程外跑）────────────────────
    def _classify_reply_async(self, reply_text: str) -> None:
        """后台分类桌宠回复；结果**就绪时经信号回主线程驱动**。

        ## 为什么要信号，不是只存字段

        第一版只把结果存 `_pending_reply_emotion`，等主线程读。真机实测
        **不工作**：`engine_reply_signal` 是即时投递的，而分类要 100-300ms
        走网络 —— 主线程读的时候结果永远还没好。

        现在双路：
          1. 存字段（快路径：若结果已就绪，主线程立即用）
          2. 发信号（慢路径：就绪时回主线程，直接驱动表情）
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
                if r is None or not r.emotion:
                    return
                vad = tuple(r.vad) if r.vad else None
                owner._pending_reply_emotion = (r.emotion, r.confidence)
                owner._pending_reply_vad_value = vad
                # 慢路径：就绪时回主线程驱动（信号 emit 是线程安全的）
                try:
                    sig = getattr(owner, "emotion_classified_signal", None)
                    if sig is not None:
                        sig.emit(r.emotion, float(r.confidence), vad,
                                 float(getattr(r, "intensity", 0.5)))
                except Exception:  # noqa: BLE001
                    pass

        try:
            QThreadPool.globalInstance().start(_Task())
        except Exception as exc:  # noqa: BLE001
            logger.debug("提交桌宠情绪分类失败：%s", exc)

    def _apply_classified_emotion(self, emotion: str, confidence: float,
                                  vad=None, intensity: float = 0.0) -> None:
        """分类结果到达主线程 → 驱动表情（信号槽，**必在主线程**）。

        这是慢路径的落点：分类晚 100-300ms 到达，直接补一次表情驱动。
        快路径（`_resolved_reply_emotion`）已经应用过时，这里会**重复驱动一次**
        —— 无害（同一个表情目标，渲染器平滑插值不会跳），但为了减少重复，
        若同一情绪刚应用过就跳过。
        """
        if not getattr(self, "_classify_enabled", False):
            return
        if not emotion or confidence < getattr(self, "_classify_min_conf", _MIN_CONFIDENCE):
            return
        word = _CLASSIFIER_TO_DIRECTOR.get(emotion, "")
        if not word or word == "neutral":
            return
        # 去重：同一情绪在短时间内不重复驱动
        now = time.time()
        if (getattr(self, "_last_applied_classified", None) == emotion
                and now - getattr(self, "_last_applied_classified_at", 0.0) < 2.0):
            return
        self._last_applied_classified = emotion
        self._last_applied_classified_at = now
        logger.info("情绪分类（桌宠视角）→ %s → %s（置信 %.2f）",
                    emotion, word, confidence)

        # 顺序关键：先同步 master emotion（它可能用 _EMOTION_VA 表覆盖 _va_target），
        # 再写连续 VAD。反过来会被表覆盖掉。
        try:
            self._current_emotion = word
            self._emotion_source = "dialog_classified"
            self._emotion_entered_at = time.monotonic()
            sync = getattr(self, "_sync_renderer_master_emotion", None)
            if callable(sync):
                sync(word)
        except Exception:
            logger.debug("同步分类情绪到渲染器失败", exc_info=True)

        # 驱动表情（不依赖决策器：决策器是另一层）
        try:
            self._direct_expression(word, "对话回复(分类)")
        except Exception:
            logger.debug("应用分类情绪失败", exc_info=True)

        # 连续 VAD → 渲染器（比离散情绪名更准的连续信号）
        if vad:
            try:
                r = getattr(self, "_renderer", None)
                setter = getattr(r, "set_va_target", None) if r is not None else None
                if callable(setter):
                    setter(vad[0], vad[1], hold_sec=3.0)
                    logger.info("分类 VAD → 渲染器 VA(%.2f, %.2f)", vad[0], vad[1])
            except Exception:
                logger.debug("写分类 VAD 失败", exc_info=True)

        # 2026-09-20（A 项）：强度 → 渲染器
        #
        # `_emotion_intensity` 早就存在（每帧读它缩放参数幅度），
        # 但**从未有人写过** —— 所有调用方都走默认 1.0。
        # 于是「我有点累」和「我累死了」表现完全一样。
        # 分类器算出的 intensity 正好填这个空。
        if intensity and intensity > 0:
            try:
                r = getattr(self, "_renderer", None)
                setter = getattr(r, "set_emotion_intensity", None) if r is not None else None
                if callable(setter):
                    setter(float(intensity))
                    logger.info("分类强度 → 渲染器 %.2f", intensity)
            except Exception:
                logger.debug("写分类强度失败", exc_info=True)

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
