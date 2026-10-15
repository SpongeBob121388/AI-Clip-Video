"""安全控制的行为测试。

这里刻意不用「源码里有没有某个字符串」当断言——那只能证明代码写过，证明不了防线拦得住。
每条用例都是：给出恶意输入，要求被拒绝；再给一个正常输入，要求不被误伤。
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from app.utils.log_redaction import is_sensitive_key, redact_dict, redact_value
from app.utils.net_guard import UnsafeURLError, validate_endpoint_url
from app.utils.path_validator import (
    PathValidationError,
    sanitize_filename,
    validate_media_path,
    validate_output_path,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _call_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attribute_name(node.value)}.{node.attr}"
    return ""


def app_sources() -> list[Path]:
    return sorted(PROJECT_ROOT.glob("app/**/*.py"))


@pytest.mark.unit
class TestNoShellExecution:
    """FFmpeg 只能以 argv 列表启动，任何 shell 解释都是注入面。"""

    def test_no_shell_true_anywhere(self) -> None:
        violations = []
        for pyfile in app_sources():
            tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg == "shell"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                ):
                    violations.append(str(pyfile))
        assert violations == [], f"发现 shell=True: {violations}"

    def test_no_posix_system_or_popen(self) -> None:
        banned = {"system", "popen", "spawnl", "execv", "eval", "exec"}
        violations = []
        for pyfile in app_sources():
            tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            for node in _call_nodes(tree):
                name = _attribute_name(node.func)
                tail = name.rsplit(".", 1)[-1]
                if tail in banned and not name.startswith("os.path"):
                    violations.append(f"{pyfile.name}:{name}")
        assert violations == [], f"发现危险调用: {violations}"

    def test_subprocess_first_argument_is_a_sequence(self) -> None:
        violations = []
        for pyfile in app_sources():
            tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            for node in _call_nodes(tree):
                func = _attribute_name(node.func)
                if not func.endswith(("subprocess.Popen", "subprocess.run", "subprocess.call")):
                    continue
                if not node.args:
                    violations.append(f"{pyfile.name}: 缺少 argv")
                    continue
                first = node.args[0]
                if isinstance(first, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                    # 字符串/拼接出的命令串会被当成 shell 命令行解析
                    violations.append(f"{pyfile.name}: argv 是字符串而非列表")
        assert violations == [], f"发现字符串形式的子进程调用: {violations}"


@pytest.mark.unit
class TestMediaPathValidation:
    @pytest.mark.parametrize(
        ("hostile", "reason"),
        [
            ("C:\\Users\\x\\..\\..\\windows\\system32\\config\\sam.mp4", "目录遍历"),
            ("\\\\attacker\\share\\movie.mp4", "UNC 网络路径"),
            ("file:///etc/passwd", "file scheme"),
            ("pipe:0", "裸协议前缀"),
            ("concat:http://evil/x", "concat 协议"),
            ("C:\\media\\PUBLIC~1\\clip.mp4", "8.3 短名"),
            ("C:\\media\\-filter_script.mp4", "以 - 开头的参数注入"),
            ("C:\\media\\clip.mp4\x00.txt", "空字节"),
            ("/srv/media/clip.txt", "不支持的扩展名"),
            ("C:\\media\\missing.mp4", "文件不存在"),
            ("", "空路径"),
            ("   ", "只有空白"),
            ("relative/clip.mp4", "相对路径"),
        ],
    )
    def test_rejects_hostile_paths(self, hostile: str, reason: str, tmp_path: Path) -> None:
        if hostile == "/srv/media/clip.txt":
            real = tmp_path / "clip.txt"
            real.write_text("not media", encoding="utf-8")
            hostile = str(real)
        with pytest.raises(PathValidationError):
            validate_media_path(hostile)

    def test_accepts_a_real_media_file(self, tmp_path: Path) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        assert validate_media_path(clip) == clip.resolve()

    def test_rejects_directory(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            validate_media_path(tmp_path)

    def test_root_containment_blocks_escape(self, tmp_path: Path) -> None:
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        with pytest.raises(PathValidationError):
            validate_media_path(str(outside), root=uploads)

    def test_root_containment_allows_inside(self, tmp_path: Path) -> None:
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        inside = uploads / "a.mp4"
        inside.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        assert validate_media_path(str(inside), root=uploads) == inside.resolve()

    def test_dotted_substring_is_not_traversal(self, tmp_path: Path) -> None:
        clip = tmp_path / "my..vlog.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        assert validate_media_path(clip) == clip.resolve()


@pytest.mark.unit
class TestOutputPathValidation:
    def test_rejects_relative_and_bad_extension(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            validate_output_path("out.mp4")
        with pytest.raises(PathValidationError):
            validate_output_path(str(tmp_path / "payload.exe"))

    def test_refuses_to_overwrite_source(self, tmp_path: Path) -> None:
        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        with pytest.raises(PathValidationError):
            validate_output_path(str(source), source_path=str(source))

    def test_allows_new_file_next_to_source(self, tmp_path: Path) -> None:
        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        target = tmp_path / "out.mp4"
        assert validate_output_path(str(target), source_path=str(source)) == target


@pytest.mark.unit
class TestUploadNameSanitisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd.mp4", "passwd.mp4"),
            ("C:\\fakepath\\..\\secret.mp4", "secret.mp4"),
            ("-filter_script.mp4", "_-filter_script.mp4"),
            ("CON.mp4", "_CON.mp4"),
            ("clip\x00.mp4", "clip.mp4"),
            ("weird:na*me?.mp4", "weirdname.mp4"),
            ("no-extension", "no-extension.mp4"),
            ("", "upload.mp4"),
            (None, "upload.mp4"),
        ],
    )
    def test_sanitises(self, raw: str | None, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_rejects_non_media_extension(self) -> None:
        with pytest.raises(PathValidationError):
            sanitize_filename("payload.exe")

    def test_result_is_never_a_path(self) -> None:
        for raw in ("..\\..\\a.mp4", "/etc/passwd.mp4", "a/b/c.mp4", "\\\\srv\\x.mp4"):
            name = sanitize_filename(raw)
            assert "/" not in name and "\\" not in name and name not in (".", "..", ".mp4")

    def test_long_name_is_truncated(self) -> None:
        name = sanitize_filename("x" * 500 + ".mp4")
        assert len(name) <= 200
        assert name.endswith(".mp4")


@pytest.mark.unit
class TestEndpointGuard:
    """设置页可写入 LLM 端点，这里是 SSRF 的唯一防线。"""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434/v1",
            "http://localhost:1234/v1",
            "http://[::1]:8080/v1",
            "https://api.openai.com/v1",
            "https://8.8.8.8/v1",
        ],
    )
    def test_allows(self, url: str) -> None:
        assert validate_endpoint_url(url).startswith(("http://", "https://"))

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("http://169.254.169.254/latest/meta-data/", "云元数据 link-local"),
            ("http://10.0.0.5/v1", "内网明文"),
            ("https://10.0.0.5/v1", "内网 TLS"),
            ("https://192.168.1.1/v1", "私有段"),
            ("https://172.16.0.1/v1", "私有段"),
            ("http://evil.example.com/v1", "明文指向公网"),
            ("file:///etc/passwd", "非 http 协议"),
            ("gopher://127.0.0.1:6379", "非 http 协议"),
            ("https://user:pass@api.openai.com", "userinfo"),
            ("https://api.openai.com@evil.test/v1", "userinfo 绕过"),
            ("https://[fe80::1]/v1", "link-local IPv6"),
            ("http://127.0.0.1:0/v1", "端口 0"),
            ("", "空 URL"),
            ("   ", "空白 URL"),
            ("https://api.openai.com:99999/v1", "端口越界"),
        ],
    )
    def test_rejects(self, url: str, why: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_endpoint_url(url)

    def test_dns_rebinding_to_private_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_getaddrinfo(host, port, **kwargs):  # noqa: ANN001
            return [(2, 1, 6, "", ("10.1.2.3", port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(UnsafeURLError):
            validate_endpoint_url("https://llm.example.com/v1")

    def test_dns_resolution_is_performed_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_getaddrinfo(host, port, **kwargs):  # noqa: ANN001
            calls.append(host)
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        validate_endpoint_url("https://example.com/v1")
        assert calls == ["example.com"]

    def test_unresolvable_host_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(host, port, **kwargs):  # noqa: ANN001
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(UnsafeURLError):
            validate_endpoint_url("https://does-not-exist.invalid/v1")

    def test_trailing_slash_normalised(self) -> None:
        assert validate_endpoint_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"


@pytest.mark.unit
class TestAuthHostAndOrigin:
    async def test_missing_token_is_401(self, bare_client) -> None:
        response = await bare_client.get("/api/v1/projects")
        assert response.status_code == 401

    async def test_wrong_token_is_401(self, async_client) -> None:
        response = await async_client.get(
            "/api/v1/projects", headers={"Authorization": "Bearer not-the-token"}
        )
        assert response.status_code == 401

    async def test_bare_token_without_bearer_is_rejected(self, async_client, auth_headers) -> None:
        token = auth_headers["Authorization"].split(" ", 1)[1]
        response = await async_client.get("/api/v1/projects", headers={"Authorization": token})
        assert response.status_code == 401

    async def test_custom_header_token_is_accepted(self, async_client, auth_headers) -> None:
        token = auth_headers["Authorization"].split(" ", 1)[1]
        response = await async_client.get("/api/v1/projects", headers={"X-ACV-Token": token})
        assert response.status_code == 200

    async def test_foreign_host_header_is_400(self, async_client, auth_headers) -> None:
        response = await async_client.get(
            "/api/v1/projects", headers={**auth_headers, "Host": "attacker.example"}
        )
        assert response.status_code == 400

    async def test_foreign_origin_is_403(self, async_client, auth_headers) -> None:
        response = await async_client.get(
            "/api/v1/projects", headers={**auth_headers, "Origin": "https://evil.example"}
        )
        assert response.status_code == 403

    async def test_null_origin_is_403(self, async_client, auth_headers) -> None:
        response = await async_client.get(
            "/api/v1/projects", headers={**auth_headers, "Origin": "null"}
        )
        assert response.status_code == 403

    async def test_dev_origin_is_allowed(self, async_client, auth_headers) -> None:
        response = await async_client.get(
            "/api/v1/projects", headers={**auth_headers, "Origin": "http://localhost:5173"}
        )
        assert response.status_code == 200

    async def test_health_needs_no_token(self, bare_client) -> None:
        response = await bare_client.get("/health")
        assert response.status_code == 200

    async def test_websocket_requires_token_and_host(self, auth_headers) -> None:
        from app.api.deps import AuthError, websocket_authenticated

        token = auth_headers["Authorization"].split(" ", 1)[1]

        class _FakeSocket:
            def __init__(self, headers: dict[str, str], query: dict[str, str]) -> None:
                self.headers = headers
                self.query_params = query
                self.closed_code: int | None = None

            async def close(self, code: int | None = None) -> None:
                self.closed_code = code

        evil_host = _FakeSocket({"host": "attacker.example"}, {"token": token})
        with pytest.raises(AuthError):
            await websocket_authenticated(evil_host)

        bad_token = _FakeSocket({"host": "test"}, {"token": "wrong"})
        assert await websocket_authenticated(bad_token) is False
        assert bad_token.closed_code == 1008

        evil_origin = _FakeSocket({"host": "test", "origin": "https://evil.example"}, {"token": token})
        assert await websocket_authenticated(evil_origin) is False
        assert evil_origin.closed_code == 1008

        good = _FakeSocket({"host": "test", "origin": "http://localhost:5173"}, {"token": token})
        assert await websocket_authenticated(good) is True
        assert good.closed_code is None

    async def test_token_never_appears_in_responses(self, async_client, auth_headers) -> None:
        token = auth_headers["Authorization"].split(" ", 1)[1]
        created = await async_client.post("/api/v1/projects", json={"name": "令牌检查"})
        pid = created.json()["project_id"]
        for path in (
            "/api/v1/projects",
            f"/api/v1/projects/{pid}",
            "/api/v1/settings",
            "/api/v1/capabilities",
        ):
            response = await async_client.get(path)
            assert token not in response.text, path

@pytest.mark.unit
class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        ["api_key", "apiKey", "OPENAI_API_KEY", "authorization", "token", "client_secret", "password"],
    )
    def test_sensitive_keys_detected(self, key: str) -> None:
        assert is_sensitive_key(key)

    @pytest.mark.parametrize("key", ["model", "base_url_path", "log_level", "port"])
    def test_ordinary_keys_not_flagged(self, key: str) -> None:
        assert not is_sensitive_key(key)

    def test_url_credentials_and_bearer_are_masked(self) -> None:
        text = redact_value("https://user:hunter2@api.example.com/v1 与 Bearer abcdefghijklmnop")
        assert "hunter2" not in text
        assert "abcdefghijklmnop" not in text

    def test_nested_dict_is_redacted(self) -> None:
        payload = {"llm": {"api_key": "sk-abcdefghijklmnop", "model": "gpt"}, "list": [{"token": "t"}]}
        result = redact_dict(payload)
        assert result["llm"]["model"] == "gpt"
        assert "abcdefghijklmnop" not in str(result)
        assert result["list"][0]["token"] != "t"

    def test_home_and_user_paths_are_masked(self) -> None:
        text = redact_value(r"C:\Users\WJN\Desktop\a.mp4 和 /home/wjn/b.mp4")
        assert "WJN" not in text
        assert "wjn" not in text
