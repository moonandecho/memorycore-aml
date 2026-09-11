#!/usr/bin/env python3
"""tests/test_llm_config.py — core.llm_config 单测 (Phase 1 验收清单)

覆盖 (任务书 Phase 1.6):
  1. 四来源逐一路径 (env LLM_API_KEY / env 配置项 / ~/.hermes/.env 白名单 /
     ~/.hermes/config.yaml model.*)
  2. 优先级顺序 + provider 不匹配整组跳过 (必改项 1)
  3. parse_dotenv 解析边界 (export 前缀 / 行内注释 / 空值 / 引号 / CRLF / 含=值)
  4. 惰性解析 (import 后设 env 生效、unset 回落) + TTL 缓存
  5. 防泄漏硬断言 (必改项 4): stdout + logging + 异常 repr 中 key 子串 0 命中
  6. 观测三态 (必改项 5): 未配置/已解析/通路已验证, 已解析≠可用
  7. 安全阀 (评审 C2): 总开关 / 调用上限 / 连续失败退避
  8. chat() 负路径语义分类 (auth_failed/timeout/network_unreachable)
"""
import http.server
import threading
import time

import pytest

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import llm_config  # noqa: E402

SECRET = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每个用例隔离: 文件来源默认关 (测试绝不读生产 ~/.hermes/.env),
    env 清空, 文件路径指向 tmp。"""
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "0")
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
              "MEMCORE_LLM_ENABLED", "MEMCORE_LLM_MAX_CALLS",
              "MEMCORE_LLM_COLD_MAX_CALLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_config, "ENV_FILE", tmp_path / "hermes.env")
    monkeypatch.setattr(llm_config, "CONFIG_YAML", tmp_path / "config.yaml")
    llm_config.invalidate_cache()
    yield


def _write_env(monkeypatch, tmp_path, text: str):
    f = tmp_path / "hermes.env"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "1")
    return f


def _write_yaml(monkeypatch, tmp_path, text: str):
    f = tmp_path / "config.yaml"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "1")
    return f


# ---- ① 四来源逐一 + 优先级 + provider 门控 ---------------------------------

def test_source_env_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    cfg = llm_config.resolve(force=True)
    assert cfg.key == SECRET and cfg.source == "env:LLM_API_KEY"
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-v4-flash"
    assert SECRET not in cfg.source and SECRET not in cfg.tried


def test_source_envfile_llm_key(monkeypatch, tmp_path):
    f = _write_env(monkeypatch, tmp_path, f"LLM_API_KEY={SECRET}\n")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == SECRET and cfg.source == f"file:{f}:LLM_API_KEY"
    assert SECRET not in cfg.source


def test_source_envfile_provider_key(monkeypatch, tmp_path):
    f = _write_env(monkeypatch, tmp_path, f"DEEPSEEK_API_KEY={SECRET}\n")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == SECRET and cfg.source == f"file:{f}:DEEPSEEK_API_KEY"
    # provider key 与自身默认 base_url/model 配对 (B7-1)
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-v4-flash"


def test_source_yaml_model(monkeypatch, tmp_path):
    f = _write_yaml(monkeypatch, tmp_path, (
        "model:\n"
        "  provider: deepseek\n"
        f"  api_key: {SECRET}\n"
        "  base_url: https://api.deepseek.com\n"
        "  default: deepseek-v4-flash\n"))
    cfg = llm_config.resolve(force=True)
    assert cfg.key == SECRET and cfg.source == f"file:{f}:model.api_key"
    assert cfg.base_url == "https://api.deepseek.com"


def test_env_wins_over_files(monkeypatch, tmp_path):
    _write_env(monkeypatch, tmp_path, f"DEEPSEEK_API_KEY={SECRET}\n")
    monkeypatch.setenv("LLM_API_KEY", "sk-env-priority")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == "sk-env-priority" and cfg.source == "env:LLM_API_KEY"


def test_envfile_llm_key_wins_over_provider_key(monkeypatch, tmp_path):
    _write_env(monkeypatch, tmp_path,
               f"DEEPSEEK_API_KEY=sk-provider\nLLM_API_KEY={SECRET}\n")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == SECRET and cfg.source.endswith(":LLM_API_KEY")


def test_provider_mismatch_skips_whole_group(monkeypatch, tmp_path):
    """必改项 1 后半: config.yaml provider ≠ 当前 → key/base_url/model 整组跳过。"""
    _write_yaml(monkeypatch, tmp_path, (
        "model:\n"
        "  provider: xiaomi\n"
        "  api_key: sk-yaml-key\n"
        "  base_url: https://api.xiaomimimo.com/v1\n"
        "  default: mimo-v2.5\n"))
    cfg = llm_config.resolve(force=True)
    assert not cfg.key, "provider 不一致 → 整组跳过 (禁止跨源混搭)"
    assert cfg.source == ""
    assert "config.yaml" in cfg.tried


def test_envfile_provider_selects_matching_key(monkeypatch, tmp_path):
    """LLM_PROVIDER=xiaomi + XIAOMI_API_KEY → 配对 xiaomi 默认 base/model。"""
    _write_env(monkeypatch, tmp_path,
               "XIAOMI_API_KEY=sk-xiaomi-key\nLLM_PROVIDER=xiaomi\n")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == "sk-xiaomi-key"
    assert cfg.source.endswith(":XIAOMI_API_KEY")
    assert cfg.base_url == "https://api.xiaomimimo.com/v1"
    assert cfg.model == "mimo-v2.5"


def test_yaml_provider_gate_follows_env_provider(monkeypatch, tmp_path):
    """.env LLM_PROVIDER=xiaomi 时, yaml provider=deepseek 组整组跳过。"""
    _write_env(monkeypatch, tmp_path, "LLM_PROVIDER=xiaomi\n")
    _write_yaml(monkeypatch, tmp_path, (
        "model:\n"
        "  provider: deepseek\n"
        "  api_key: sk-yaml-key\n"))
    cfg = llm_config.resolve(force=True)
    assert not cfg.key


def test_file_sources_disabled_default(monkeypatch):
    """文件来源关闭 (发布版缺省): 只有 env key 能生效, tried 链明示未启用。"""
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "0")
    cfg = llm_config.resolve(force=True)
    assert not cfg.key
    assert "未启用" in cfg.tried


# ---- ② parse_dotenv 边界 (评审 B3) -----------------------------------------

def test_parse_dotenv_boundaries(tmp_path):
    f = tmp_path / "x.env"
    f.write_bytes(
        b"# comment\n"
        b"\n"
        b"export LLM_API_KEY=sk-exported\n"           # export 前缀精确切
        b"exported=1\n"                               # lstrip 陷阱: 键名无损
        b"LLM_MODEL=deepseek-v4-flash # inline\n"     # 行内注释
        b"LLM_BASE_URL='https://a.example.com/v1' # q\n"  # 引号+尾部注释
        b'LLM_PROVIDER="xiaomi"\r\n'                  # CRLF + 双引号
        b"EMPTY=\n"                                   # 空值不收录
        b"EQ=a=b=c\n"                                 # 值含 =
        b"LLM_API_KEY=\n"                             # 空值不得覆盖缺省
    )
    out = llm_config.parse_dotenv(
        f, {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
            "exported", "EMPTY", "EQ"})
    assert out["LLM_API_KEY"] == "sk-exported"
    assert out["exported"] == "1"
    assert out["LLM_MODEL"] == "deepseek-v4-flash"
    assert out["LLM_BASE_URL"] == "https://a.example.com/v1"
    assert out["LLM_PROVIDER"] == "xiaomi"
    assert "EMPTY" not in out
    assert out["EQ"] == "a=b=c"


def test_parse_dotenv_whitelist_only(tmp_path):
    f = tmp_path / "x.env"
    f.write_text("OTHER_KEY=1\nLLM_MODEL=x\n", encoding="utf-8")
    out = llm_config.parse_dotenv(f, {"LLM_MODEL"})
    assert out == {"LLM_MODEL": "x"}


def test_parse_dotenv_missing_file(tmp_path):
    assert llm_config.parse_dotenv(tmp_path / "nope.env", {"LLM_API_KEY"}) == {}


# ---- ③ 惰性 + TTL ----------------------------------------------------------

def test_lazy_resolve_not_frozen_at_import(monkeypatch, tmp_path):
    """import 后设 env → 生效 (与 config.LLM_API_KEY 冻结语义对照)。"""
    assert config_mod.LLM_API_KEY == "", "config.LLM_API_KEY 冻结语义 (兼容旧代码)"
    monkeypatch.setenv("LLM_API_KEY", "sk-late-key")
    cfg = llm_config.resolve(force=True)
    assert cfg.key == "sk-late-key"
    assert config_mod.LLM_API_KEY == "", "config 常量保持冻结, 勿用于新代码"
    # unset → 回落 (文件源关闭 → 未配置)
    monkeypatch.delenv("LLM_API_KEY")
    cfg2 = llm_config.resolve(force=True)
    assert not cfg2.key
    assert "env:LLM_API_KEY" in cfg2.tried


def test_ttl_cache(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-first")
    c1 = llm_config.resolve(force=True)
    monkeypatch.setenv("LLM_API_KEY", "sk-second")
    c2 = llm_config.resolve()  # 缓存未过期 → 旧值
    assert c2 is c1 and c2.key == "sk-first"
    c3 = llm_config.resolve(force=True)  # force 重解析
    assert c3.key == "sk-second"


def test_invalidate_cache_is_production_entry(monkeypatch):
    """TTL 有意设计 (终审低危): 进程级 30s 缓存防 .env 抖动; 生产失效入口 =
    invalidate_cache (MCP 工具入口调用) / llm_check force。锁定入口语义:
    失效后立即重解析, 长驻进程中配置变更至多 30s 生效。"""
    monkeypatch.setattr(llm_config, "TTL_SECONDS", 30.0)
    llm_config.invalidate_cache()
    monkeypatch.setenv("LLM_API_KEY", "sk-first")
    c1 = llm_config.resolve()
    monkeypatch.setenv("LLM_API_KEY", "sk-second")
    c2 = llm_config.resolve()  # TTL 未过期 → 旧值 (有意设计)
    assert c2.key == "sk-first"
    llm_config.invalidate_cache()  # 生产失效入口 (server.py MCP 工具入口调用)
    c3 = llm_config.resolve()
    assert c3.key == "sk-second"
    assert c3.source == "env:LLM_API_KEY"


def test_fallback_backoff_expires(monkeypatch, caplog):
    """兜底护栏退避过期 (终审低危): 会话外直调 (max_calls=None) 失败 → 退避;
    超过 BACKOFF_EXPIRE_SECONDS → 新轮复位, 不再进程级永续退避。会话内
    guard (start_session 总带上限) 不受过期影响, 退避持续到 close()。"""
    import logging

    caplog.set_level(logging.INFO)
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    llm_config.invalidate_cache()
    monkeypatch.setattr(llm_config, "_fallback_guard", None)
    monkeypatch.setattr(llm_config, "BACKOFF_EXPIRE_SECONDS", 1.0)

    # 会话外直调 → 兜底 guard; 失败 → 立即退避
    assert llm_config.acquire("直调") is not None
    llm_config.note_failure("auth_failed", "HTTP 401")
    assert llm_config.acquire("直调2") is None
    assert llm_config.block_kind() == "backoff"

    # 会话内 guard: 即使过期也持续退避 (不触发过期复位)
    g = llm_config.start_session(stat={}, max_calls=8)
    assert llm_config.acquire("会话") is not None
    llm_config.note_failure("auth_failed", "HTTP 401")
    assert llm_config.acquire("会话2") is None
    assert llm_config.block_kind() == "backoff"
    snap = g.close()
    assert snap["backoff"] is True

    # 兜底 guard 退避过期 → 新轮复位, 恢复尝试
    time.sleep(1.1)
    assert llm_config.acquire("新轮") is not None
    assert llm_config.block_kind() == ""
    assert "新轮复位" in caplog.text
    llm_config._fallback_guard = None  # 清理全局状态, 防污染后续测试


# ---- ④ 防泄漏硬断言 (必改项 4) ----------------------------------------------

def test_no_key_leak_in_outputs(monkeypatch, capsys, caplog):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    llm_config.invalidate_cache()
    cfg = llm_config.resolve(force=True)
    assert SECRET not in cfg.source and SECRET not in cfg.tried
    assert SECRET not in cfg.masked_key
    stat = {}
    with llm_config.guard_session(stat=stat):
        with pytest.raises(llm_config.LLMError) as ei:
            llm_config.chat(cfg, {"model": cfg.model, "messages": []}, timeout=1)
        assert SECRET not in repr(ei.value)
        assert ei.value.category == "network_unreachable"
        llm_config.note_failure(ei.value.category, ei.value.detail)
        llm_config.acquire("压缩")
    captured = capsys.readouterr()
    for blob in (captured.out, captured.err, caplog.text):
        assert SECRET not in blob, "key 明文泄漏!"
        assert SECRET[:6] not in blob, "key 前缀泄漏!"
    # stat 里也只有脱敏字段
    snap_json = str(stat["llm"])
    assert SECRET not in snap_json


def test_mask_function(monkeypatch):
    assert llm_config.mask("") == "<空>"
    assert llm_config.mask("short") == "*****"
    assert llm_config.mask("sk-abcd1234") == "sk-a...1234"


# ---- ⑤ 观测三态 (必改项 5) --------------------------------------------------

def test_three_state_observability(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    with llm_config.guard_session(stat={}) as g:
        assert llm_config.acquire("压缩") is None
        snap = g.snapshot()
    logs = caplog.text
    assert "未配置" in logs and "已尝试" in logs
    assert "通路已验证" not in logs, "禁止把未配置报成可用"
    assert "已解析" not in logs
    assert snap["status"] == "unconfigured"
    assert "压缩已跳过" in logs

    monkeypatch.setenv("LLM_API_KEY", SECRET)
    llm_config.invalidate_cache()
    caplog.clear()
    with llm_config.guard_session(stat={}) as g:
        assert llm_config.acquire("压缩") is not None
        logs = caplog.text
        assert "已解析（来源 env:LLM_API_KEY" in logs
        assert "通路已验证" not in logs, "已解析 ≠ 通路可用"
        llm_config.note_success()
        assert "通路已验证" in caplog.text
        snap = g.snapshot()
    assert snap["status"] == "verified"


# ---- ⑥ 安全阀 (评审 C2) -----------------------------------------------------

def test_guard_total_switch(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("MEMCORE_LLM_ENABLED", "0")
    llm_config.invalidate_cache()
    stat = {}
    with llm_config.guard_session(stat=stat):
        assert llm_config.acquire("压缩") is None
        assert llm_config.block_kind() == "disabled"
    assert stat["llm"]["status"] == "disabled"
    assert "总开关关闭" in caplog.text
    cfg = llm_config.resolve(force=True)
    assert cfg.configured is False


def test_guard_call_cap(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("MEMCORE_LLM_MAX_CALLS", "2")
    llm_config.invalidate_cache()
    stat = {}
    g = llm_config.start_session(stat=stat, max_calls=2)
    assert llm_config.acquire("a") is not None
    llm_config.note_success()
    assert llm_config.acquire("b") is not None
    llm_config.note_success()
    assert llm_config.acquire("c") is None
    assert llm_config.block_kind() == "cap"
    snap = g.close()
    assert stat["llm"]["calls"] == 2
    assert stat["llm"]["skipped_cap"] == 1
    assert snap["status"] == "verified"


def test_guard_backoff_skips_rest(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("MEMCORE_LLM_MAX_CALLS", "8")
    llm_config.invalidate_cache()
    stat = {}
    g = llm_config.start_session(stat=stat)
    assert llm_config.acquire("a") is not None
    llm_config.note_failure("auth_failed", "HTTP 401")
    # 退避: 剩余候选全部跳过 (不再串行等超时)
    assert llm_config.acquire("b") is None
    assert llm_config.block_kind() == "backoff"
    assert llm_config.acquire("c") is None
    snap = g.close()
    assert stat["llm"]["failures"] == 1
    assert stat["llm"]["skipped_backoff"] == 2
    assert stat["llm"]["backoff"] is True
    assert snap["status"] == "degraded"
    assert "本轮退避" in caplog.text


def test_format_status_lines(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    llm_config.invalidate_cache()
    g = llm_config.start_session(stat={})
    llm_config.acquire("压缩")
    llm_config.note_success()
    snap = g.close()
    line = llm_config.format_status(snap)
    assert "verified" in line and "来源 env:LLM_API_KEY" in line
    assert SECRET not in line


# ---- ⑦ chat() 语义分类 ------------------------------------------------------

class _AuthHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(401)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        time.sleep(5)

    def log_message(self, *args):
        pass


class _FastServer(http.server.ThreadingHTTPServer):
    """跳过 HTTPServer.server_bind 的 socket.getfqdn (本机反查 DNS 慢 ~35s)。"""

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)


def _with_server(handler_cls, fn):
    srv = _FastServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True  # shutdown 不等慢 handler (timeout 用例)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return fn(port)
    finally:
        srv.shutdown()
        t.join(timeout=5)


def _cfg(base_url):
    return llm_config.LLMConfig(key="sk-test", base_url=base_url,
                                model="deepseek-v4-flash")


def test_chat_auth_failed():
    def run(port):
        with pytest.raises(llm_config.LLMError) as ei:
            llm_config.chat(_cfg(f"http://127.0.0.1:{port}"),
                            {"model": "m", "messages": []}, timeout=3)
        assert ei.value.category == "auth_failed"
    _with_server(_AuthHandler, run)


def test_chat_timeout():
    def run(port):
        t0 = time.time()
        with pytest.raises(llm_config.LLMError) as ei:
            llm_config.chat(_cfg(f"http://127.0.0.1:{port}"),
                            {"model": "m", "messages": []}, timeout=0.5)
        assert ei.value.category == "timeout"
        assert time.time() - t0 < 3
    _with_server(_SlowHandler, run)


# ---- ⑧ llm_check 自检入口 (Phase 3) ------------------------------------------

def _run_check(argv):
    import io
    from contextlib import redirect_stdout
    from memorycore import llm_check
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = llm_check.main(argv)
        except SystemExit as e:  # argparse --help 等
            raise
    return rc, buf.getvalue()


def test_llm_check_unconfigured_zero_network(monkeypatch, capsys):
    from memorycore import llm_check
    rc = llm_check.main(["--json"])
    out = capsys.readouterr().out
    assert rc == 1
    import json as _json
    rep = _json.loads(out)
    assert rep["status"] == "unconfigured"
    assert SECRET not in out


def test_llm_check_configured_masked(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    llm_config.invalidate_cache()
    rc, out = _run_check([])
    assert rc == 0
    assert "已解析" in out and "来源: env:LLM_API_KEY" in out
    assert SECRET not in out, "llm_check 输出泄漏 key!"
    assert "通路已验证" not in out, "零网络档不得出现通路已验证"


def test_llm_check_live_unreachable(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    llm_config.invalidate_cache()
    rc, out = _run_check(["--live"])
    assert rc == 2
    assert "network_unreachable" in out
    assert SECRET not in out


def test_llm_check_live_auth_failed(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    llm_config.invalidate_cache()

    class _AuthAny(_AuthHandler):
        def do_GET(self):
            self.do_POST()

    def run(port):
        monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{port}")
        llm_config.invalidate_cache()
        rc, out = _run_check(["--live"])
        assert rc == 2
        assert "auth_failed" in out
        assert SECRET not in out
    _with_server(_AuthAny, run)


def test_llm_check_help_has_billing_note(capsys):
    from memorycore import llm_check
    with pytest.raises(SystemExit) as ei:
        llm_check.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--live" in out and "计费提示" in out


def test_chat_network_unreachable():
    """127.0.0.1:未监听端口 + 短超时 → network_unreachable (<2s, 不断网)。"""
    t0 = time.time()
    with pytest.raises(llm_config.LLMError) as ei:
        llm_config.chat(_cfg("http://127.0.0.1:9"),
                        {"model": "m", "messages": []}, timeout=2)
    assert ei.value.category == "network_unreachable"
    assert time.time() - t0 < 2
