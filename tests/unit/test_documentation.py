"""文档准确性测试。

只检查「文件存在」的文档测试没有价值：文档最容易出现的问题不是缺失，而是
宣传了没实现的能力、或者指向已经改名的路径与命令。这里针对这两类漂移做断言。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS = ["README.md", "docs/faq.md", "docs/operations.md", "docs/development.md"]

# 这些能力目前不可用，提到它们的那一行必须同时说明「未实现/未接入/默认关闭/需安装」
# OTIO 不在列：它的产物已由官方读取器验证并放开（见 ADR-003）
GATED_CAPABILITIES = ("WhisperX", "whisperx", "Qwen ASR", "HeyGen", "D-ID", "数字人")
HONESTY_MARKERS = ("未实现", "尚未实现", "未接入", "不可用", "默认关闭", "需安装", "不受影响")

# 历史文档里出现过、实际不存在的目录
STALE_PATHS = (
    r"C:\Program Files\auto_clip_video",
    "%USERPROFILE%/Videos/auto_clip_video",
    "cache/preview",
    "auto_clip_video_sidecar",
)


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.unit
class TestCapabilityHonesty:
    @pytest.mark.parametrize("doc", DOCS)
    def test_gated_capabilities_are_qualified(self, doc: str) -> None:
        offenders = []
        for number, line in enumerate(_read(doc).splitlines(), start=1):
            stripped = line.strip()
            # 标题只负责分组，说明写在正文里
            if stripped.startswith("#"):
                continue
            mentions = any(name in stripped for name in GATED_CAPABILITIES)
            qualified = any(marker in stripped for marker in HONESTY_MARKERS)
            if mentions and not qualified:
                offenders.append(f"{doc}:{number}: {stripped[:80]}")
        assert offenders == [], "提到未实现能力却没说明的行：\n" + "\n".join(offenders)

    @pytest.mark.parametrize("doc", DOCS)
    def test_no_stale_paths(self, doc: str) -> None:
        text = _read(doc)
        for stale in STALE_PATHS:
            assert stale not in text, f"{doc} 仍在引用已不存在的 {stale!r}"


@pytest.mark.unit
class TestReleaseMetadata:
    def test_versions_agree_across_manifests(self) -> None:
        pyproject = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
        cargo = tomllib.loads(_read("src-tauri/Cargo.toml"))["package"]["version"]
        tauri = json.loads(_read("src-tauri/tauri.conf.json"))["version"]
        frontend = json.loads(_read("frontend/package.json"))["version"]
        assert pyproject == cargo == tauri == frontend

    def test_documented_env_vars_exist_in_settings(self) -> None:
        from app.config import Settings

        fields = set(Settings.model_fields)
        documented: set[str] = set()
        for doc in DOCS:
            documented.update(re.findall(r"\bACV_([A-Z][A-Z_0-9]*)\b", _read(doc)))
        assert documented, "文档里应当出现配置项"
        unknown = {name for name in documented if name.lower() not in fields}
        assert not unknown, f"文档提到但 Settings 里不存在的环境变量: {sorted(unknown)}"

    def test_readme_commands_match_entrypoints(self) -> None:
        readme = _read("README.md")
        assert "uvicorn app.main:app" in readme
        assert "npm run dev" in readme
        assert "npm run typecheck" in readme

    def test_operations_manual_describes_real_layout(self) -> None:
        text = _read("docs/operations.md")
        for required in (
            "auto-clip-video-server",
            "preview_cache",
            "exports",
            "runtime.port",
            "api.token",
            "logs/app.log",
            "build_sidecar.ps1",
        ):
            assert required in text, f"运维手册缺少 {required!r} 的说明"


@pytest.mark.unit
class TestRequiredDocumentsExist:
    @pytest.mark.parametrize("doc", [*DOCS, "docs/adr.md", "THIRD_PARTY_NOTICES.md", "SPEC.md"])
    def test_exists_and_non_trivial(self, doc: str) -> None:
        path = PROJECT_ROOT / doc
        assert path.is_file(), doc
        assert len(path.read_text(encoding="utf-8").strip()) > 200, f"{doc} 内容过短"
