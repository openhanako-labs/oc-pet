"""角色包管理器 — M5 模块

负责将角色打包为 .pet 文件（zip 格式），以及从 .pet 文件安装角色。

参考 docs/architecture-5-modules.md 中 M5 设计：
- 文件格式: .pet (zip)
- manifest.json 必须包含: name, agent_id, version, description
- 身份文件: identity.md, awareness.md, model.json
- 可选: sprites/ 精灵图, memory/ 记忆
"""

import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

REQUIRED_IDENTITY_FILES = ["identity.md", "awareness.md", "model.json"]
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
                    pass
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

        try:
            with zipfile.ZipFile(pet_path, "r") as zf:
                # 校验 manifest
                if MANIFEST_NAME not in zf.namelist():
                    raise PackageValidationError(
                        f"无效的 .pet 文件: 缺少 {MANIFEST_NAME}"
                    )

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
                return str(install_target)

        except (json.JSONDecodeError, KeyError) as e:
            raise PackageValidationError(f"manifest 解析失败: {e}") from e
        except Exception as e:
            # 清理可能残留的安装目录
            if install_target is not None and install_target.exists():
                try:
                    shutil.rmtree(install_target)
                except OSError:
                    pass
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
