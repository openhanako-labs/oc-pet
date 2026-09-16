"""角色包管理器 — M5 模块

负责将角色打包为 .pet 文件（zip 格式），以及从 .pet 文件安装角色。

参考 docs/architecture-5-modules.md 中 M5 设计：
- 文件格式: .pet (zip)
- manifest.json 必须包含: name, agent_id, version, description
- 身份文件: model.json（外观定义必须；identity/awareness 由 Hanako agent 提供，模型包不再伪造）
- 可选: sprites/ 精灵图, memory/ 记忆
"""

import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

# 模型包只包含外观定义(model.json 指向 .model3.json);人设(identity/awareness)
# 一律由对应 Hanako agent 提供,导入纯 Live2D zip 时不再伪造占位人设文件。
REQUIRED_IDENTITY_FILES = ["model.json"]
OPTIONAL_DIRS = ["sprites", "memory"]
MANIFEST_NAME = "manifest.json"
PET_EXTENSION = ".pet"


class PackageManifest:
    """角色包清单数据类"""

    def __init__(
        self,
        name: str,
        agent_id: str,
        version: str = "1.0.0",
        description: str = "",
        required_hanako_version: str = "",
        author: str = "",
    ):
        self.name = name
        self.agent_id = agent_id
        self.version = version
        self.description = description
        self.required_hanako_version = required_hanako_version
        self.author = author

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "version": self.version,
            "description": self.description,
            "required_hanako_version": self.required_hanako_version,
            "author": self.author,
        }

    @staticmethod
    def from_dict(data: dict) -> "PackageManifest":
        return PackageManifest(
            name=data["name"],
            agent_id=data["agent_id"],
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            required_hanako_version=data.get("required_hanako_version", ""),
            author=data.get("author", ""),
        )

    @staticmethod
    def from_file(path: Path) -> "PackageManifest":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return PackageManifest.from_dict(data)


# ── 异常 ──────────────────────────────────────────────


class PackageError(Exception):
    """角色包操作基础异常"""


class PackageCreateError(PackageError):
    """创建包失败"""


class PackageInstallError(PackageError):
    """安装包失败"""


class PackageNotFoundError(PackageError):
    """找不到包"""


class PackageValidationError(PackageError):
    """包内容校验失败"""


# ── 管理器 ────────────────────────────────────────────


class CharacterPackageManager:
    """角色包管理器

    职责:
    1. 创建角色包 (.pet / zip)
    2. 安装角色包到 agents/ 目录
    3. 列出已安装的角色包
    4. 卸载角色包
    """

    def __init__(self, characters_dir: Optional[Path] = None, install_dir: Optional[Path] = None):
        """
        Args:
            characters_dir: 角色源目录，默认当前项目下的 characters/
            install_dir:    安装目标目录，默认当前项目下的 characters/
        """
        self._base_dir = Path(__file__).resolve().parent.parent
        self.characters_dir = characters_dir or (self._base_dir / "characters")
        self.install_dir = install_dir or self.characters_dir

    # ── 创建 ────────────────────────────────────────

    def create_package(
        self,
        agent_id: str,
        output_path: Optional[str] = None,
        manifest_override: Optional[dict] = None,
    ) -> Path:
        """将指定 agent 打包为 .pet 文件

        Args:
            agent_id:         角色 ID（对应 characters/<agent_id>/）
            output_path:      输出 .pet 文件路径，默认 characters/<agent_id>.pet
            manifest_override: 覆盖 manifest 中的字段

        Returns:
            生成的 .pet 文件路径

        Raises:
            PackageNotFoundError: agent_id 不存在
            PackageCreateError:   打包过程出错
        """
        source_dir = self.characters_dir / agent_id
        if not source_dir.is_dir():
            raise PackageNotFoundError(
                f"角色目录不存在: {source_dir}\n"
                f"可用角色: {[d.name for d in self.characters_dir.iterdir() if d.is_dir()]}"
            )

        # 生成默认 manifest
        manifest = PackageManifest(
            name=agent_id,
            agent_id=agent_id,
            version="1.0.0",
            description=f"角色包: {agent_id}",
        )
        if manifest_override:
            for k, v in manifest_override.items():
                setattr(manifest, k, v)

        # 确定输出路径
        if output_path is None:
            output_path = str(self.characters_dir / f"{agent_id}{PET_EXTENSION}")
        output_path = Path(output_path)

        logger.info("正在打包角色 '%s' → %s", agent_id, output_path)

        try:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # 写入 manifest
                manifest_data = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
                zf.writestr(MANIFEST_NAME, manifest_data)

                # 遍历源目录，收集文件
                file_count = 0
                for root, dirs, files in os.walk(source_dir):
                    rel_root = Path(root).relative_to(source_dir)
                    for fname in sorted(files):
                        src_file = Path(root) / fname
                        arc_name = str(rel_root / fname)
                        zf.write(src_file, arc_name)
                        file_count += 1

                logger.info("打包完成: %d 个文件", file_count)

        except Exception as e:
            # 清理可能残留的不完整文件
            if output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    logger.debug("character_package: 非致命异常(已静默吞掉)", exc_info=True)
            raise PackageCreateError(f"打包失败: {e}") from e

        return output_path

    # ── 安装 ────────────────────────────────────────

    def install_package(
        self,
        pet_path: str,
        overwrite: bool = False,
        target_dir: Optional[Path] = None,
    ) -> str:
        """从 .pet 文件安装角色

        Args:
            pet_path:   .pet 文件路径
            overwrite:  是否覆盖已存在的同名 agent
            target_dir: 安装目标目录，默认 self.install_dir

        Returns:
            安装后的角色目录路径（字符串）

        Raises:
            PackageNotFoundError: 文件不存在
            PackageValidationError: manifest 校验失败
            PackageInstallError:  解压或写入失败
        """
        pet_path = Path(pet_path)
        if not pet_path.exists():
            raise PackageNotFoundError(f".pet 文件不存在: {pet_path}")

        target_dir = target_dir or self.install_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        logger.info("正在安装角色包: %s", pet_path)
        install_target: Optional[Path] = None
        # 2026-09-15: 提前初始化——原实现赋值在 raise 之后，
        # 一旦校验失败进入 except，引用未定义变量会抛 NameError
        # （掩盖真实错误：用户看到的是 NameError 而非"格式不对"）。
        tmp_file_to_cleanup: Optional[Path] = None

        try:
            # 2026-09-08: 检查是否需要预处理（live2d zip 没有 manifest）
            with zipfile.ZipFile(pet_path, "r") as zf:
                has_manifest = MANIFEST_NAME in zf.namelist()

                if not has_manifest:
                    # 尝试识别 live2d 模型 zip。
                    # 2026-09-15: 补上 Cubism 2 的 .model.json（
                    # 此前只认 .model3.json，导致 kurisu 这类旧格式模型
                    # 被误判为"无效压缩包"）。注意排除 .model3.json——
                    # 它同样以 .model.json 结尾。
                    model_files = [
                        f for f in zf.namelist()
                        if f.endswith(".model3.json") or (
                            f.endswith(".model.json") and not f.endswith(".model3.json")
                        )
                    ]
                    if not model_files:
                        raise PackageValidationError(
                            f"无效的压缩包: 缺少 {MANIFEST_NAME} 且未找到 .model3.json / .model.json"
                        )

                    # 2026-09-15: agent_id 优先从**模型文件名**推断，
                    # 而非 zip 文件名——导入的 zip 常带随机后缀
                    # （如 kurisu_mu3j9dca_b794b7db），用它会生成脏目录名。
                    # kurisu.model.json -> kurisu
                    _mbase = os.path.basename(model_files[0])
                    for _suf in (".model3.json", ".model.json"):
                        if _mbase.endswith(_suf):
                            _mbase = _mbase[: -len(_suf)]
                            break
                    zip_name = pet_path.stem  # 原始文件名（仅作回退/描述）
                    agent_id = re.sub(r'[^A-Za-z0-9_-]', '_', _mbase)[:50] or 'live2d_character'

                    # 2026-09-15: 剥掉单层包装目录。live2d zip 常把模型放在
                    # <name>/ 这样的子目录里（kurisu/kurisu.model.json），
                    # 但格式检测（factory.detect_format）与渲染器
                    # （live2d_renderer）只认**角色目录顶层**和 live2d/ 子目录
                    # 下的模型文件——不剥则导入成功也加载不出来。
                    _names = [n for n in zf.namelist() if not n.endswith('/')]
                    _prefix = ""
                    if _names:
                        _parts = _names[0].split('/')
                        if len(_parts) > 1:
                            _cand = _parts[0] + '/'
                            if all(n.startswith(_cand) for n in _names):
                                _prefix = _cand

                    # 创建临时 zip 文件，添加必要的文件
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
                        tmp_zip_path = tmp_zip.name

                    # 复制原 zip 文件到临时 zip（剥包装目录）
                    with zipfile.ZipFile(tmp_zip_path, "a") as tmp_zf:
                        with zipfile.ZipFile(pet_path, "r") as orig_zf:
                            for item in orig_zf.infolist():
                                new_name = item.filename
                                if _prefix and new_name.startswith(_prefix):
                                    new_name = new_name[len(_prefix):]
                                if not new_name:
                                    continue
                                if new_name.endswith('/'):
                                    continue
                                tmp_zf.writestr(new_name, orig_zf.read(item.filename))

                        # 添加 manifest.json
                        manifest_data = {
                            "name": agent_id,
                            "agent_id": agent_id,
                            "version": "1.0.0",
                            "description": f"Live2D model imported from {pet_path.name}",
                            "required_hanako_version": "",
                            "author": ""
                        }
                        tmp_zf.writestr(MANIFEST_NAME, json.dumps(manifest_data, ensure_ascii=False, indent=2))

                        # 仅生成外观定义 model.json(指向模型文件)。
                        # 人设(identity/awareness)一律由对应 Hanako agent 提供,
                        # 此处不再伪造占位人设文件——避免"外观包"与"人格包"被强行绑死。
                        _mp = model_files[0]
                        if _prefix and _mp.startswith(_prefix):
                            _mp = _mp[len(_prefix):]
                        tmp_zf.writestr("model.json", json.dumps({
                            "type": "live2d",
                            "model_path": _mp
                        }, ensure_ascii=False, indent=2))
                    
                    # 使用临时 zip 文件继续安装
                    pet_path = Path(tmp_zip_path)
                
                # 重新打开 zip 文件
                zf.close()
            
            # 从 zip 文件安装
            # 2026-09-15: 若走了 live2d 预处理，pet_path 已换成临时 zip，需清理。
            if not has_manifest:
                tmp_file_to_cleanup = pet_path
            with zipfile.ZipFile(pet_path, "r") as zf:
                # 读取 manifest
                manifest_text = zf.read(MANIFEST_NAME).decode("utf-8")
                manifest_data = json.loads(manifest_text)
                manifest = PackageManifest.from_dict(manifest_data)
                
                # 校验必填字段
                for field in ("name", "agent_id"):
                    if not manifest_data.get(field):
                        raise PackageValidationError(
                            f"manifest 缺少必填字段: {field}"
                        )

                agent_id = manifest.agent_id
                # 防 zip-slip：agent_id 白名单 [A-Za-z0-9_-]，杜绝 "../x" 越界写
                if not agent_id or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
                    raise PackageValidationError(
                        f"非法 agent_id: {agent_id!r}（仅允许字母/数字/下划线/连字符）"
                    )
                install_target = target_dir / agent_id

                # 版本兼容性检查
                if manifest.required_hanako_version:
                    logger.warning(
                        "此角色包要求 Hanako >= %s，当前版本未知",
                        manifest.required_hanako_version,
                    )

                # 检查是否已存在
                if install_target.exists():
                    if not overwrite:
                        raise PackageInstallError(
                            f"角色 '{agent_id}' 已存在于 {install_target}，"
                            f"设置 overwrite=True 以覆盖"
                        )
                    logger.info("覆盖已有角色: %s", agent_id)
                    shutil.rmtree(install_target)

                # 防 zip-slip：解压前校验每个成员路径落在 install_target 内，
                # 拒绝 "../"、绝对路径、符号链接逃逸等越界成员
                target_resolved = install_target.resolve()
                for member in zf.infolist():
                    member_path = (install_target / member.filename).resolve()
                    if not member_path.is_relative_to(target_resolved):
                        raise PackageValidationError(
                            f"角色包包含越界路径: {member.filename}"
                        )

                # 解压所有文件
                zf.extractall(install_target)

                # 校验身份文件完整性
                installed_files = [f.relative_to(install_target) for f in install_target.rglob("*") if f.is_file()]
                missing = []
                for req_file in REQUIRED_IDENTITY_FILES:
                    found = any(str(f).endswith(req_file) for f in installed_files)
                    if not found:
                        missing.append(req_file)

                if missing:
                    logger.warning(
                        "角色 '%s' 缺少身份文件: %s",
                        agent_id,
                        ", ".join(missing),
                    )

                logger.info(
                    "角色包安装成功: %s → %s (v%s)",
                    agent_id,
                    install_target,
                    manifest.version,
                )
                
                # 清理临时 zip 文件
                if tmp_file_to_cleanup and tmp_file_to_cleanup.exists():
                    try:
                        os.unlink(tmp_file_to_cleanup)
                    except OSError:
                        logger.debug("character_package: 非致命异常(已静默吞掉)", exc_info=True)
                
                return str(install_target)

        except (json.JSONDecodeError, KeyError) as e:
            raise PackageValidationError(f"manifest 解析失败: {e}") from e
        except Exception as e:
            # 清理可能残留的安装目录
            if install_target is not None and install_target.exists():
                try:
                    shutil.rmtree(install_target)
                except OSError:
                    logger.debug("character_package: 非致命异常(已静默吞掉)", exc_info=True)
            # 清理临时 zip 文件
            if tmp_file_to_cleanup and tmp_file_to_cleanup.exists():
                try:
                    os.unlink(tmp_file_to_cleanup)
                except OSError:
                    logger.debug("character_package: 非致命异常(已静默吞掉)", exc_info=True)
            if isinstance(e, PackageError):
                raise
            raise PackageInstallError(f"安装失败: {e}") from e

    # ── 列出 ────────────────────────────────────────

    def list_installed_packages(
        self, target_dir: Optional[Path] = None
    ) -> list[PackageManifest]:
        """扫描已安装的角色，返回 manifest 列表

        每个角色目录下只要有 manifest.json 即视为一个有效包；
        否则尝试读取 pet.json 作为 fallback。

        Returns:
            PackageManifest 列表
        """
        target_dir = target_dir or self.install_dir
        if not target_dir.is_dir():
            return []

        packages = []
        for agent_dir in sorted(target_dir.iterdir()):
            if not agent_dir.is_dir():
                continue

            manifest_path = agent_dir / MANIFEST_NAME
            pet_json_path = agent_dir / "pet.json"

            if manifest_path.exists():
                try:
                    manifest = PackageManifest.from_file(manifest_path)
                    packages.append(manifest)
                except Exception as e:
                    logger.warning("读取 %s 的 manifest 失败: %s", agent_dir.name, e)
            elif pet_json_path.exists():
                # fallback: 从 pet.json 构造基本 manifest
                try:
                    with open(pet_json_path, "r", encoding="utf-8") as f:
                        pet_data = json.load(f)
                    manifest = PackageManifest(
                        name=pet_data.get("name", agent_dir.name),
                        agent_id=pet_data.get("id", agent_dir.name),
                        version="1.0.0",
                        description=pet_data.get("description", ""),
                    )
                    packages.append(manifest)
                except Exception as e:
                    logger.warning("读取 %s 的 pet.json 失败: %s", agent_dir.name, e)
            else:
                # 既没有 manifest 也没有 pet.json，仍然列出
                packages.append(
                    PackageManifest(
                        name=agent_dir.name,
                        agent_id=agent_dir.name,
                        version="?",
                        description="(无 manifest)",
                    )
                )

        logger.info("扫描到 %d 个已安装角色", len(packages))
        return packages

    # ── 卸载 ────────────────────────────────────────

    def uninstall_package(
        self, agent_id: str, target_dir: Optional[Path] = None
    ) -> bool:
        """卸载指定角色

        Args:
            agent_id: 角色 ID
            target_dir: 目标目录，默认 self.install_dir

        Returns:
            True 如果成功卸载，False 如果角色不存在
        """
        target_dir = target_dir or self.install_dir
        agent_dir = target_dir / agent_id

        if not agent_dir.is_dir():
            logger.warning("角色不存在，无需卸载: %s", agent_id)
            return False

        logger.info("正在卸载角色: %s", agent_id)
        try:
            shutil.rmtree(agent_dir)
            logger.info("角色已卸载: %s", agent_id)
            return True
        except OSError as e:
            logger.error("卸载失败 %s: %s", agent_id, e)
            return False

    # ── 预览 ────────────────────────────────────────

    def preview_package(self, pet_path: str) -> PackageManifest:
        """预览 .pet 包内容（不解压）

        Args:
            pet_path: .pet 文件路径

        Returns:
            manifest

        Raises:
            PackageNotFoundError: 文件不存在
            PackageValidationError: manifest 缺失或无效
        """
        pet_path = Path(pet_path)
        if not pet_path.exists():
            raise PackageNotFoundError(f".pet 文件不存在: {pet_path}")

        try:
            with zipfile.ZipFile(pet_path, "r") as zf:
                if MANIFEST_NAME not in zf.namelist():
                    raise PackageValidationError(
                        f"无效的 .pet 文件: 缺少 {MANIFEST_NAME}"
                    )
                manifest_text = zf.read(MANIFEST_NAME).decode("utf-8")
                manifest_data = json.loads(manifest_text)
                return PackageManifest.from_dict(manifest_data)
        except (json.JSONDecodeError, KeyError) as e:
            raise PackageValidationError(f"manifest 解析失败: {e}") from e
