"""工具执行器 — 通过 Node.js 执行 Hanako 插件工具

每个工具是 JS 模块，导出 name/description/parameters/execute。
执行器生成临时脚本，import 工具模块并调用 execute()。

用法:
    executor = ToolExecutor()
    result = executor.execute("play", {"source": "music.mp3"})
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .tool_registry import ToolDef

logger = logging.getLogger(__name__)

HANAKO_PLUGINS = Path.home() / ".hanako" / "plugins"
HANAKO_DATA = Path.home() / ".hanako"


class ToolExecutor:
    """通过 Node.js 执行插件工具"""

    def execute(self, tool: ToolDef, arguments: dict) -> str:
        """执行工具，返回结果文本

        Args:
            tool: 工具定义
            arguments: LLM 传入的参数

        Returns:
            工具执行结果文本
        """
        if not tool.source_path:
            return f"工具 {tool.name} 没有可执行的源文件"

        source_path = Path(tool.source_path)
        if not source_path.exists():
            return f"工具文件不存在: {source_path}"

        # 构建执行脚本
        script = self._build_runner_script(source_path, tool.plugin_id, arguments)

        try:
            # 写入临时文件
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.mjs', delete=False, encoding='utf-8'
            ) as f:
                f.write(script)
                tmp_path = f.name

            # 执行
            result = subprocess.run(
                ['node', tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(source_path.parent),
            )

            # 清理
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

            if result.returncode == 0:
                # 解析输出（期望 JSON 格式的 MCP result）
                output = result.stdout.strip()
                return self._parse_output(output)
            else:
                stderr = result.stderr.strip()[:500]
                logger.warning("Tool %s failed: %s", tool.name, stderr)
                return f"工具执行失败: {stderr}"

        except subprocess.TimeoutExpired:
            return f"工具 {tool.name} 执行超时（30秒）"
        except FileNotFoundError:
            return "Node.js 未安装，无法执行插件工具"
        except Exception as e:
            logger.warning("Tool execution error: %s", e)
            return f"工具执行异常: {e}"

    def _build_runner_script(self, source_path: Path, plugin_id: str, arguments: dict) -> str:
        """生成 Node.js 执行脚本"""
        # 计算 dataDir
        data_dir = HANAKO_DATA / "plugin-data" / plugin_id
        data_dir.mkdir(parents=True, exist_ok=True)

        args_json = json.dumps(arguments, ensure_ascii=False)
        source_abs = str(source_path.resolve()).replace('\\', '/')
        data_dir_str = str(data_dir.resolve()).replace('\\', '/')

        return f"""
import {{ createRequire }} from 'module';
const require = createRequire(import.meta.url);

// 加载工具模块
const tool = await import('file:///{source_abs}');

// 执行
const args = {args_json};
const context = {{
  sessionPath: '',
  pluginId: '{plugin_id}',
  dataDir: '{data_dir_str}',
}};

try {{
  const result = await tool.execute(args, context);
  console.log(JSON.stringify(result));
}} catch (e) {{
  console.error(e.message);
  process.exit(1);
}}
"""

    def _parse_output(self, output: str) -> str:
        """解析工具输出（MCP content 格式或纯文本）"""
        if not output:
            return "工具执行完成（无输出）"

        try:
            data = json.loads(output)
            # MCP 格式: {content: [{type: "text", text: "..."}]}
            if isinstance(data, dict) and "content" in data:
                parts = []
                for item in data["content"]:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item["text"])
                    elif isinstance(item, str):
                        parts.append(item)
                return "\n".join(parts) if parts else "工具执行完成"
            # 纯字符串
            elif isinstance(data, str):
                return data
            # 其他 JSON
            else:
                return json.dumps(data, ensure_ascii=False)[:500]
        except json.JSONDecodeError:
            # 纯文本输出
            return output[:1000]
