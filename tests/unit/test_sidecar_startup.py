"""本地服务启动：端口被占用时退回空闲端口，实际端口交给外壳。

起因是安装版 sidecar 固定监听 8765。端口被残留的开发服务（或第二个实例）占用时，
uvicorn 绑定失败会先 `await lifespan.shutdown()` 再 `sys.exit(1)`：runtime.port 被顺手
删掉，windowed 产物又没有控制台，用户只会看到「装好后打开没用」。
"""

from __future__ import annotations

import json
import logging
import socket
from contextlib import closing

import pytest

from app import main as main_module
from app.config import get_settings, reset_settings
from app.main import resolve_bind_port

_HOST = "127.0.0.1"


def _listen_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((_HOST, port))
    sock.listen(1)
    return sock


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind((_HOST, 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> object:
    """run() 会把文件日志挂到 root，不摘掉就一直占着 tmp 里 app.log 的句柄。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved


def _log_events() -> list[dict[str, object]]:
    log_file = get_settings().log_file
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


@pytest.mark.unit
class TestResolveBindPort:
    def test_preferred_port_is_used_when_it_is_free(self) -> None:
        preferred = _free_port()
        assert resolve_bind_port(_HOST, preferred) == preferred

    def test_occupied_preferred_port_falls_back(self) -> None:
        with closing(_listen_socket(0)) as busy:
            taken = int(busy.getsockname()[1])
            assert resolve_bind_port(_HOST, taken) != taken

    def test_fallback_port_can_actually_be_bound(self) -> None:
        with closing(_listen_socket(0)) as busy:
            taken = int(busy.getsockname()[1])
            resolved = resolve_bind_port(_HOST, taken)
        # 退回的端口必须真空闲，否则只是把失败推后一步
        with closing(_listen_socket(resolved)):
            pass


@pytest.mark.unit
class TestRunHandsOverTheResolvedPort:
    """run() 必须把实际端口写回 settings：lifespan 落盘的 runtime.port 读的就是它。"""

    def _start_on_busy_port(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        recorded: dict[str, object] = {}

        def fake_run(target: str, **kwargs: object) -> None:
            recorded.update(target=target, **kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)
        with closing(_listen_socket(0)) as busy:
            taken = int(busy.getsockname()[1])
            monkeypatch.setenv("ACV_PORT", str(taken))
            # conftest 已经建过 Settings，不重建就读不到这个端口
            reset_settings()
            main_module.run()
        recorded["taken"] = taken
        return recorded

    def test_uvicorn_is_given_the_resolved_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = self._start_on_busy_port(monkeypatch)
        settings = get_settings()
        assert settings.port != recorded["taken"]
        assert recorded["port"] == settings.port
        assert recorded["host"] == _HOST

    def test_fallback_is_recorded_in_the_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = self._start_on_busy_port(monkeypatch)
        events = [e for e in _log_events() if e.get("event") == "preferred_port_busy"]
        assert len(events) == 1
        assert events[0]["requested"] == recorded["taken"]
        assert events[0]["fallback"] == get_settings().port

    def test_free_preferred_port_logs_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        preferred = _free_port()
        monkeypatch.setenv("ACV_PORT", str(preferred))
        reset_settings()
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
        main_module.run()
        assert get_settings().port == preferred
        assert [e for e in _log_events() if e.get("event") == "preferred_port_busy"] == []
