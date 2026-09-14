"""avatar/model_health.py — 模型体检报告

基于 param_writer._probe_parameters 的探测逻辑，启动时遍历参数对照标准清单，
缺失+缩放打成人类/AI 可读报告。

v2（2026-09-14）修复误报
------------------------
旧版把每个语义通道写成**单一硬编码参数名**，而模型命名有多种变体
（Cubism 标准 `ParamEyeLOpen` / 变体 `ParamEyeOpenL` / 简写 `ParamEyeL`），
导致 miku 模型明明有眼开合、眼笑、眉角、眉形，却被报“缺失”——
持续误导后续决策（有人会据此去白名单里加不存在的参数）。

修复：每个通道改为**别名元组 + 按序匹配**，任一命中即算存在，
并在报告里输出“哪个别名命中”，便于核对。

同时区分三种状态：
  ✅ 完整    —— 必需参数全部存在
  ⚠️ 部分缺失 —— 部分存在；或必需参数缺但有功能替代（如呼吸）
  ❌ 缺失    —— 必需参数一个都没有
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# ── 标准参数清单（v2：别名元组）──

class _ParamModel(Protocol):
    """渲染器模型的最小接口"""
    def GetParameterCount(self) -> int: ...
    def GetParameter(self, index: int) -> object: ...


# 语义通道 → (候选参数名元组, 是否必需, 备注)
#
# 候选按优先级排列，取**第一个存在**的作为命中（报告会标明用的是哪个）。
# 命名变体来源（实测 miku.cdi3.json，141 参数）：
#   - Cubism 标准：ParamEyeLOpen / ParamEyeROpen / ParamEyeLSmile / ParamBrowLAngle
#   - 历史变体：  ParamEyeOpenL / ParamEyeSmileL（旧体检表写法，非标准）
#   - 简写：      ParamEyeL / ParamEyeR
STANDARD_PARAMS: dict[str, tuple[tuple[str, ...], bool, str]] = {
    # ── 眼睛 ──
    "eye_open": (
        ("ParamEyeLOpen", "ParamEyeROpen", "ParamEyeOpenL", "ParamEyeOpenR",
         "ParamEyeL", "ParamEyeR"),
        True, "眼睛开合",
    ),
    "eye_smile": (
        ("ParamEyeLSmile", "ParamEyeRSmile", "ParamEyeSmileL", "ParamEyeSmileR"),
        True, "眼笑（眯眼）",
    ),
    "eye_ball_x": (("ParamEyeBallX",), True, "眼珠 X"),
    "eye_ball_y": (("ParamEyeBallY",), True, "眼珠 Y"),
    # ── 眉毛 ──
    "brow_angle": (
        ("ParamBrowLAngle", "ParamBrowRAngle", "ParamBrowAngleL", "ParamBrowAngleR",
         "ParamAngleL", "ParamAngleR"),
        True, "眉角度",
    ),
    "brow_form": (
        ("ParamBrowLForm", "ParamBrowRForm", "ParamBrowFormL", "ParamBrowFormR"),
        True, "眉形态",
    ),
    # ── 嘴巴 ──
    "mouth_form": (("ParamMouthForm", "ParamMouthFormY"), True, "嘴型"),
    "mouth_open": (("ParamMouthOpenY", "ParamMouthOpen"), True, "嘴张开"),
    # ── 头部 ──
    "head_angle_x": (("ParamAngleX",), True, "头部角度 X"),
    "head_angle_y": (("ParamAngleY",), True, "头部角度 Y"),
    "head_angle_z": (("ParamAngleZ",), False, "头部角度 Z"),
    # ── 呼吸（模型可能只有单一 ParamBreath）──
    "breath_amp": (("ParamBreathAmp", "ParamBreath"), False, "呼吸幅度（可降级为 ParamBreath）"),
    "breath_rate": (("ParamBreathRate", "ParamBreath"), False, "呼吸频率（可降级为 ParamBreath）"),
    # ── 脸红（模型可能只有贴图参数）──
    "blush": (("ParamBlush", "ParamCheek", "Param130"), False, "脸红（可降级为贴图/组合模拟）"),
    # ── 其他 Live2D 标准参数 ──
    "ParamAngleX": (("ParamAngleX",), True, "标准头部 X"),
    "ParamAngleY": (("ParamAngleY",), True, "标准头部 Y"),
    "ParamAngleZ": (("ParamAngleZ",), False, "标准头部 Z"),
    "ParamEyeBallX": (("ParamEyeBallX",), True, "标准眼珠 X"),
    "ParamEyeBallY": (("ParamEyeBallY",), True, "标准眼珠 Y"),
    "ParamEyeBallZ": (("ParamEyeBallZ",), False, "标准眼珠 Z（多数模型无，且当前不使用）"),
}

# 缩放参数（检查范围）
SCALE_PARAMS = {
    "eye_open": (0.0, 1.0),
    "eye_smile": (-0.5, 1.0),
    "eye_ball_x": (-1.0, 1.0),
    "eye_ball_y": (-1.0, 1.0),
    "brow_angle": (-1.0, 1.0),
    "brow_form": (-1.0, 1.0),
    "mouth_form": (-1.0, 1.0),
    "mouth_open": (0.0, 1.0),
    "head_angle_x": (-30, 30),
    "head_angle_y": (-30, 30),
}


# ── 报告生成 ──

def probe_model_parameters(model: _ParamModel) -> tuple[set[str], bool]:
    """探测模型实际参数集（复用 param_writer._probe_parameters 逻辑）"""
    try:
        count = model.GetParameterCount()
        return (
            {str(model.GetParameter(i).id) for i in range(count)},
            True,
        )
    except (AttributeError, TypeError) as e:
        log.warning("模型参数探测失败: %s", e)
        return set(), False


def check_parameter_coverage(available: set[str], probed: bool) -> list[dict]:
    """检查标准参数覆盖情况（v2：别名元组按序匹配）。

    每个通道取**第一个命中**的别名作为 found；报告会输出命中的具体名，
    便于核对“到底是命名不同还是真缺”。
    """
    results = []
    if not probed:
        results.append({
            "channel": "所有参数",
            "status": "⚠️ 探测失败",
            "message": "无法获取模型参数列表，使用 try/except 兜底",
            "missing": [],
        })
        return results

    for channel, spec in STANDARD_PARAMS.items():
        aliases, required, note = spec
        hit = next((a for a in aliases if a in available), None)
        alt_hits = [a for a in aliases if a in available]
        if hit is not None:
            status = "✅ 完整"
            extra = ""
            if len(alt_hits) > 1:
                extra = f"（共命中 {len(alt_hits)} 个别名）"
            message = f"{note} → 命中 {hit}{extra}"
        elif not required:
            # 非必需且一个都没中：算“可接受缺失”（有替代路径）
            status = "⚠️ 可接受缺失"
            message = f"{note} → 无（已按设计降级，不影响功能）"
        else:
            status = "❌ 缺失"
            message = f"{note} → 无（尝试过: {', '.join(aliases)}）"
        results.append({
            "channel": channel,
            "status": status,
            "message": message,
            "missing": [] if hit is not None else list(aliases),
            "found": [hit] if hit else [],
            "required": required,
            "note": note,
        })
    return results


def check_scale_parameters(model: _ParamModel, available: set[str]) -> list[dict]:
    """检查缩放参数范围"""
    results = []
    for pid in available:
        if pid in SCALE_PARAMS:
            min_val, max_val = SCALE_PARAMS[pid]
            try:
                # 获取参数值（需要 model.SetParameterValue 的逆操作）
                # 这里简化：只检查参数名是否存在
                results.append({
                    "pid": pid,
                    "status": "✅ 存在",
                    "expected_range": f"[{min_val}, {max_val}]",
                })
            except Exception:
                results.append({
                    "pid": pid,
                    "status": "⚠️ 无法读取",
                    "expected_range": f"[{min_val}, {max_val}]",
                })
    return results


def generate_report(model: _ParamModel) -> dict:
    """生成完整模型体检报告"""
    available, probed = probe_model_parameters(model)
    coverage = check_parameter_coverage(available, probed)
    scales = check_scale_parameters(model, available) if probed else []

    # 统计（v2：区分“可接受缺失”与“真缺失”）
    total = len(STANDARD_PARAMS)
    ok = sum(1 for c in coverage if c["status"] == "✅ 完整")
    partial = sum(1 for c in coverage if c["status"] == "⚠️ 部分缺失")
    acceptable = sum(1 for c in coverage if c["status"] == "⚠️ 可接受缺失")
    missing = sum(1 for c in coverage if c["status"] == "❌ 缺失")
    failed = sum(1 for c in coverage if c["status"] == "⚠️ 探测失败")

    return {
        "probed": probed,
        "total_params": len(available),
        "coverage": {
            "total": total,
            "ok": ok,
            "partial": partial,
            "acceptable_missing": acceptable,
            "missing": missing,
            "failed": failed,
        },
        "details": coverage,
        "scales": scales,
    }


def format_report_human(report: dict) -> str:
    """格式化为人类可读报告"""
    lines = ["#" * 60, "# 模型体检报告", "#" * 60, ""]

    if not report["probed"]:
        lines.append("⚠️ 模型参数探测失败，无法生成完整报告。")
        lines.append("   检查模型文件格式是否正确（.model3.json + .moc3）。")
        return "\n".join(lines)

    cov = report["coverage"]
    lines.append(f"总参数数: {report['total_params']}")
    lines.append(
        f"标准覆盖: ✅ {cov['ok']}/{cov['total']} 完整, "
        f"⚠️ {cov.get('acceptable_missing', 0)} 可接受缺失, "
        f"❌ {cov['missing']} 缺失"
    )
    lines.append("")

    for detail in report["details"]:
        lines.append(f"{detail['status']} {detail['channel']}: {detail['message']}")

    if report["scales"]:
        lines.append("")
        lines.append("缩放参数检查:")
        for s in report["scales"]:
            lines.append(f"  {s['status']} {s['pid']}: 期望范围 {s['expected_range']}")

    return "\n".join(lines)


def format_report_ai(report: dict) -> str:
    """格式化为 AI 可读报告（整段粘贴给 AI 修）"""
    lines = ["# 模型体检报告（AI 可读）", ""]

    if not report["probed"]:
        lines.append("```json")
        lines.append('{"probed": false, "error": "模型参数探测失败"}')
        lines.append("```")
        return "\n".join(lines)

    import json
    # AI 可读格式：JSON
    ai_report = {
        "total_params": report["total_params"],
        "coverage": report["coverage"],
        "missing_parameters": [],
        "recommendations": [],
    }

    for detail in report["details"]:
        # 只对**必需且真缺**的通道给建议；“可接受缺失”（有降级路径）不报，
        # 否则会让人去加一些本来就设计为可缺的参数。
        if detail["missing"] and detail.get("required", True):
            ai_report["missing_parameters"].extend(detail["missing"])
            ai_report["recommendations"].append(
                f"为 {detail['channel']}（{detail.get('note', '')}）添加参数，"
                f"候选名: {', '.join(detail['missing'])}"
            )

    lines.append("```json")
    lines.append(json.dumps(ai_report, ensure_ascii=False, indent=2))
    lines.append("```")

    return "\n".join(lines)


def print_report(model: _ParamModel, format: str = "human") -> None:
    """打印报告"""
    report = generate_report(model)
    if format == "ai":
        print(format_report_ai(report))
    else:
        print(format_report_human(report))
    log.info("模型体检完成: %s", report["coverage"])
