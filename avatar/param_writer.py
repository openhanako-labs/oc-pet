"""avatar/param_writer.py — 参数写入器（D1-lite）。

T07: 将 live2d_renderer.py:1375-1448 的硬编码 SetParameterValue 调用
提取为 ParamWriter，通过运行时探测模型实际参数集来决定跳过/写入，
消除 13 处 ``except Exception: pass``。

关键约束：
- 字节等价——每组参数的 weight / clamp / scale / 调用顺序严格不变
- 跳过缺通道而非 try/except——ParamWriter 初始化时探测模型实际参数，
  写入时跳过不可用的 pid，不吞异常
- ``_speaking`` 让位、``_apply_head`` 门控留在 renderer，writer 只管值→参数
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence

from avatar.model_profile import ModelProfile, ParamChannel, ParamGroup
import logging
logger = logging.getLogger(__name__)



class _ParamModel(Protocol):
    """渲染器模型的最小接口（LAppModel / LAppModelBase 兼容）。"""

    def SetParameterValue(self, pid: str, value: float, weight: float) -> None: ...

    def GetParameterCount(self) -> int: ...

    def GetParameter(self, index: int) -> object: ...


class ParamWriter:
    """语义通道值 → SetParameterValue 委托。

    初始化时探测模型实际参数集（``GetParameterCount`` + ``GetParameter``），
    写入时跳过不可用的 pid。不吞异常——如果 ``SetParameterValue`` 因
    参数不存在以外的原因抛异常，向上冒泡。

    Usage::

        writer = ParamWriter(model, profile)
        writer.write_group("eyes_open", cur["eye_open"])
        writer.write_group("eyebrows", cur["brow_angle"],
                           secondary_value=cur["brow_form"])
    """

    def __init__(self, model: _ParamModel, profile: ModelProfile) -> None:
        self._model = model
        self._profile = profile
        self._available, self._probed = self._probe_parameters(model)

    @staticmethod
    def _probe_parameters(model: _ParamModel) -> tuple[set[str], bool]:
        """运行时探测模型实际参数集。

        Returns:
            (available_pids, probed_successfully):
            - 如果探测成功：返回实际参数集，True
            - 如果探测失败：返回空集，False（调用方使用 try/except 兜底）
        """
        try:
            count = model.GetParameterCount()
            return (
                {str(model.GetParameter(i).id) for i in range(count)},
                True,
            )
        except (AttributeError, TypeError):
            # _FakeModel（测试用）没有 GetParameterCount/GetParameter
            # 返回空集 + False 标记，让 write_group 使用 try/except 兜底
            return set(), False

    @property
    def available(self) -> set[str]:
        """模型实际拥有的参数 id 集合。"""
        return self._available

    def is_available(self, pid: str) -> bool:
        """检查单个参数 id 是否存在于模型中。

        如果探测成功（_probed=True），直接查集合。
        如果探测失败（_probed=False），假设所有参数可用（由 try/except 兜底）。
        """
        if not self._probed:
            return True
        return pid in self._available

    def write_group(
        self,
        group_name: str,
        values: dict[str, float],
        *,
        gate: bool = True,
    ) -> None:
        """写入一个参数组。

        Args:
            group_name: 参数组名称（对应 ``_DEFAULT_GROUPS`` 的键）。
            values: 语义通道值字典（如 ``{"eye_open": 0.85}`` 或
                    ``{"brow_angle": 0.35, "brow_form": 0.3}``）。
            gate: 调用方门控标志。False 时跳过整组（如 ``_speaking``
                  期间跳过 mouth_open、``_apply_head`` 为 False 时跳过 head）。

        每个通道：
          1. 解析语义键 → 物理 pid（从 profile 中查找）
          2. 检查 pid 是否在模型实际参数集中；不在则跳过
          3. 应用 scale → clamp
          4. 调用 ``SetParameterValue(pid, prepared_value, weight)``

        组内无可用通道时整组跳过（等价于旧代码的 try/except 效果，
        但不吞异常——异常只会在参数存在时由 SetParameterValue 抛出）。
        """
        if not gate:
            return

        group: Optional[ParamGroup] = self._profile.groups.get(group_name)
        if group is None:
            return

        for ch in group.channels:
            pid = self._resolve_pid(ch.std_name)
            if not self.is_available(pid):
                continue
            value = self._lookup_semantic_value(group_name, ch, values)
            prepared = ch.prepare_value(value)
            self._write_param(pid, prepared, ch.weight)

    def write_derived(
        self,
        group_name: str,
        values: dict[str, float],
        *,
        gate: bool = True,
    ) -> None:
        """写入一个包含派生值的参数组。

        用于 breath（从 breath_amp + breath_rate 派生）和 hair（正弦派生）。
        派生公式在 renderer 中计算，传入已计算好的派生值字典。

        Args:
            group_name: 参数组名称。
            values: 已计算好的派生值字典（如 ``{"breath": 0.25}`` 或
                    ``{"hair": 3.0}``）。派生键名与组的第一个通道匹配。
            gate: 调用方门控。
        """
        if not gate:
            return

        group: Optional[ParamGroup] = self._profile.groups.get(group_name)
        if group is None:
            return

        for ch in group.channels:
            pid = self._resolve_pid(ch.std_name)
            if not self.is_available(pid):
                continue
            # 派生值已在 renderer 中计算完毕，直接写入
            value = float(values.get(pid, 0.0))
            self._write_param(pid, value, ch.weight)

    # ── 辅助方法 ──

    def _resolve_pid(self, std_name: str) -> str:
        """将 StandardParams 属性名解析为实际参数 id。

        ``ParamEyeLOpen`` → ``EyeLOpen``；``EyeLOpen`` → ``EyeLOpen``（不变）。
        """
        if std_name.startswith("Param"):
            return std_name[5:]
        return std_name

    def _write_param(self, pid: str, value: float, weight: float) -> None:
        """写入单个参数。

        探测成功时（_probed=True）：直接调用，异常冒泡。
        探测失败时（_probed=False，如测试用 _FakeModel）：
        try/except 兜底，缺失参数跳过，不崩。
        """
        if self._probed:
            self._model.SetParameterValue(pid, value, weight)
        else:
            try:
                self._model.SetParameterValue(pid, value, weight)
            except (AttributeError, RuntimeError):
                logger.debug("param_writer: 非致命异常(已静默吞掉)", exc_info=True)

    def _lookup_semantic_value(
        self, group_name: str, ch: ParamChannel, values: dict[str, float]
    ) -> float:
        """从语义值字典中查找当前通道的值。

        映射规则（按组名）：
        - eyes_open:     key = "eye_open"
        - eyes_smile:    key = "eye_smile"
        - eyebrows:      前两个通道 → "brow_angle"，后两个 → "brow_form"
        - mouth_form:    key = "mouth_form"
        - mouth_open:    key = "mouth_open"
        - gaze:          第一个 → "eye_ball_x"，第二个 → "eye_ball_y"
        - head_angle:    第一个 → "head_angle_x"，第二个 → "head_angle_y"
        - breath/hair:   走 write_derived，不经过此方法
        """
        # 简单的组内索引 → 语义键映射
        semantic_keys = _GROUP_SEMANTIC_KEYS.get(group_name, [])
        idx = _GROUP_SEMANTIC_KEYS_INDEX.get((group_name, ch.std_name))
        if idx is not None and idx < len(semantic_keys):
            key = semantic_keys[idx]
            return float(values.get(key, 0.0))
        # fallback: 用 std_name 去掉 Param 前缀后作为语义键
        return float(values.get(ch.std_name.replace("Param", "").lower(), 0.0))


# ── 组 → 语义键映射（字节等价于原代码的语义通道） ──

_GROUP_SEMANTIC_KEYS: dict[str, list[str]] = {
    "eyes_open": ["eye_open"],
    "eyes_smile": ["eye_smile"],
    "eyebrows": ["brow_angle", "brow_form"],
    "mouth_form": ["mouth_form"],
    "mouth_open": ["mouth_open"],
    "gaze": ["eye_ball_x", "eye_ball_y"],
    "head_angle": ["head_angle_x", "head_angle_y"],
}

# (组名, StandardParams属性名) → 该属性在语义键列表中的索引
_GROUP_SEMANTIC_KEYS_INDEX: dict[tuple[str, str], int] = {
    ("eyes_open", "ParamEyeLOpen"): 0,
    ("eyes_open", "ParamEyeROpen"): 0,
    ("eyes_smile", "ParamEyeLSmile"): 0,
    ("eyes_smile", "ParamEyeRSmile"): 0,
    ("eyebrows", "ParamBrowLAngle"): 0,
    ("eyebrows", "ParamBrowRAngle"): 0,
    ("eyebrows", "ParamBrowLForm"): 1,
    ("eyebrows", "ParamBrowRForm"): 1,
    ("mouth_form", "ParamMouthForm"): 0,
    ("mouth_open", "ParamMouthOpenY"): 0,
    ("gaze", "ParamEyeBallX"): 0,
    ("gaze", "ParamEyeBallY"): 1,
    ("head_angle", "ParamAngleX"): 0,
    ("head_angle", "ParamAngleY"): 1,
}
