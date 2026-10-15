"""打包配置测试。

「文件存在」的断言挡不住真实的打包事故：产物名对不上、依赖少标一个 extra、
前端引了插件而 Rust 侧没注册，都要到装机后才炸。这里只测这类跨清单一致性。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class TestBuildScripts:
    def test_build_script_exists(self) -> None:
        assert (PROJECT_ROOT / "scripts" / "build_windows.ps1").exists()

    def test_verify_script_exists(self) -> None:
        assert (PROJECT_ROOT / "scripts" / "verify_package.ps1").exists()

    def test_no_fake_reproducibility_gate(self) -> None:
        """安装包哈希每次构建都不同（WiX PackageCode + PyInstaller 时间戳）。

        留一个「比对两次构建哈希」的脚本等于留一个必然误报的门禁。
        """
        assert not (PROJECT_ROOT / "scripts" / "verify_reproducible.ps1").exists()

    def test_pyinstaller_spec_exists(self) -> None:
        assert (PROJECT_ROOT / "pyinstaller.spec").exists()


class TestThirdPartyNotices:
    def test_third_party_notices_exists(self) -> None:
        assert (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").exists()

    def test_third_party_notices_has_content(self) -> None:
        content = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        assert "fastapi" in content.lower()
        assert "MIT" in content
        assert "FFmpeg" in content


class TestTauriConfig:
    def test_tauri_conf_exists(self) -> None:
        assert (PROJECT_ROOT / "src-tauri" / "tauri.conf.json").exists()

    def test_cargo_toml_exists(self) -> None:
        assert (PROJECT_ROOT / "src-tauri" / "Cargo.toml").exists()


def _cargo_deps() -> set[str]:
    text = (PROJECT_ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z0-9_-]+)\s*=", text, re.MULTILINE))


class TestRuntimeDependencies:
    def test_sqlalchemy_asyncio_extra_declared(self) -> None:
        """app/persistence 依赖 sqlalchemy.ext.asyncio，运行时必须有 greenlet。

        只写 `sqlalchemy` 时干净环境不会装 greenlet：开发机因为别的包带而看不出来，
        冻结产物一启动就 ImportError（实测踩过）。
        """
        deps = [
            d
            for d in tomllib.loads(
                (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["dependencies"]
            if d.lower().startswith("sqlalchemy")
        ]
        assert deps, "pyproject 未声明 sqlalchemy"
        assert "[asyncio]" in deps[0], f"需要 sqlalchemy[asyncio]，实际声明为 {deps[0]!r}"

    def test_spec_lists_greenlet_explicitly(self) -> None:
        """SQLAlchemy 延迟 import greenlet，且 PyInstaller 没有 greenlet 钩子。"""
        spec = (PROJECT_ROOT / "pyinstaller.spec").read_text(encoding="utf-8")
        assert re.search(r"hiddenimports[^\n]*greenlet", spec), "pyinstaller.spec 未收集 greenlet"

    def test_runtime_deps_have_license_notices(self) -> None:
        """随 sidecar 分发的每个依赖都要在 THIRD_PARTY_NOTICES 里登记。"""
        deps = [
            re.split(r"[<>=!;\[\s]", d)[0].lower()
            for d in tomllib.loads(
                (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["dependencies"]
        ]
        notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
        missing = [d for d in deps if d not in notices]
        assert not missing, f"随产物分发但未登记许可的依赖: {missing}"


class TestSidecarNamingConsistency:
    """三处名字必须一致：PyInstaller 产物名、bundle.externalBin、Rust 侧 sidecar 常量。"""

    def test_names_agree(self) -> None:
        spec = (PROJECT_ROOT / "pyinstaller.spec").read_text(encoding="utf-8")
        spec_name = re.search(r'SIDECAR_NAME\s*=\s*"([^"]+)"', spec).group(1)

        conf = json.loads(
            (PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
        )
        external = conf["bundle"]["externalBin"]
        assert len(external) == 1, "externalBin 应当只有一个 sidecar"

        lib = (PROJECT_ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
        rust_const = re.search(r'const\s+SIDECAR_NAME[^=]*=\s*"([^"]+)"', lib).group(1)

        assert Path(external[0]).name == spec_name
        # Rust 侧只认裸文件名：安装包把 sidecar 平铺在主程序旁边，
        # shell().sidecar() 按「exe 同级 + 该名字」解析，带上 binaries/ 前缀会找不到而闪退
        assert rust_const == spec_name
        assert "/" not in rust_const and "\\" not in rust_const

    def test_build_script_uses_same_exe_name(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8-sig"
        )
        assert "auto-clip-video-server.exe" in script
        assert "src-tauri/binaries" in script.replace("\\", "/")

    def test_shell_clears_stale_port_before_spawn(self) -> None:
        """runtime.port 必须只描述当前正在运行的本地服务。

        上次退出时 sidecar 是被 kill 掉的，lifespan 的删除不会执行；端口又可能因冲突换过，
        前端一启动就照旧文件去连会连不上。所以外壳 spawn 之前要先删。
        """
        lib = (PROJECT_ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
        spawn_at = lib.index(".spawn()")
        assert 'remove_file(dir.join("runtime.port"))' in lib[:spawn_at]

    def test_checksums_file_is_machine_verifiable(self) -> None:
        """SHA256SUMS.txt 必须能直接 sha256sum -c。

        追加写会让同一文件名对应多个哈希；PowerShell 的 -Encoding utf8 带 BOM、
        [IO.File]::WriteAllLines 带 CRLF，两者都会让 sha256sum 把行尾当成文件名的一部分。
        """
        script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8-sig"
        )
        assert "-Append" not in script, "校验和清单不能追加写"
        assert "Out-File" not in script, "Out-File 会写入 BOM，sha256sum 读不了"
        assert "WriteAllText" in script, "应整体重写并显式用 LF 连接行"


class TestPluginPairing:
    """前端引了的 Tauri 插件，Rust 侧必须注册；capabilities 也只能引已注册的插件。"""

    def test_frontend_plugins_have_rust_crates(self) -> None:
        pkg = json.loads(
            (PROJECT_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        js_deps = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
        deps = _cargo_deps()
        for name in sorted(js_deps):
            match = re.fullmatch(r"@tauri-apps/plugin-([a-z0-9-]+)", name)
            if not match or match.group(1) in ("cli", "api"):
                continue
            crate = f"tauri-plugin-{match.group(1)}"
            assert crate in deps, f"前端引了 {name}，Cargo.toml 缺 {crate}"

    def test_capabilities_permissions_are_registered(self) -> None:
        cargo = (PROJECT_ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        # core:* 由 tauri 本体提供，其余前缀必须对应一个已声明的插件 crate
        available = {m.group(1) for m in re.finditer(r"tauri-plugin-([a-z0-9_-]+)", cargo)}
        available.add("core")
        caps_dir = PROJECT_ROOT / "src-tauri" / "capabilities"
        for cap_file in sorted(caps_dir.glob("*.json")):
            caps = json.loads(cap_file.read_text(encoding="utf-8"))
            for perm in caps.get("permissions", []):
                if ":" not in perm:  # 形如 "fs-all" 的权限集名，交给 tauri-build 校验
                    continue
                owner = perm.split(":")[0]
                assert owner in available, (
                    f"{cap_file.name} 引用权限 {perm!r}，但 tauri-plugin-{owner} 未声明"
                )
