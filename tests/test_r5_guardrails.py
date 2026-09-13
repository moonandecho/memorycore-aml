#!/usr/bin/env python3
"""R5 收尾回归护栏 (FIX3-REVIEW P3)。

三条都直接消费发布树里的真实实现/真实 fixture, 不靠字符串或硬编码结论:
  1. relative_drop 由 fixture 内复算 baseline 驱动, 模块常量改坏输出也不变;
  2. PLUGIN_PATH 必须在仓库根内, 且 HOME 指到 tmp 后插件仍可加载;
  3. silver fixture 全部 200 条查询经插件 F1 闸门 0 条被挡 (数据驱动)。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "fault_replay_silver.json"


def _load_file_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_replay_mod():
    return _load_file_module("r5_replay_guard",
                             ROOT / "tools" / "replay_fault_rate.py")


def _load_plugin_mod():
    import conftest  # pytest 注入 tests/ 目录后可用; 它已安装 agent mock
    return _load_file_module("r5_plugin_guard",
                             Path(conftest.PLUGIN_PATH).resolve())


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_r5_relative_drop_driven_by_replayed_baseline(monkeypatch):
    """P3-1: BASELINE_REFERENCE 只准展示, 改坏后回放输出逐字段不变。"""
    replay = _load_replay_mod()
    fixture = _fixture()
    before = replay.evaluate_fixture(fixture)
    imp = before["implementation"]

    # 独立复算 fixture baseline, 证明 baseline_k3_048_fault_rate 不是硬编码。
    n = len(fixture["queries"])
    baseline_hits = 0
    for qitem in fixture["queries"]:
        dense = [float(x) for x in qitem["dense"]]
        targets = set(int(x) for x in qitem["target_rule_indices"])
        order = sorted(range(len(dense)), key=lambda j: dense[j], reverse=True)
        if any(j in targets for j in order[:3] if dense[j] >= 0.48):
            baseline_hits += 1
    expected_baseline = (n - baseline_hits) / n
    assert before["silver"]["baseline_k3_048_fault_rate"] == round(expected_baseline, 4)
    assert imp["relative_drop_vs_baseline"] == round(
        (expected_baseline - imp["fault_rate"]) / expected_baseline, 4)
    assert imp["relative_drop_formula"] == "replayed_baseline_k3_048"

    monkeypatch.setattr(replay, "BASELINE_REFERENCE", -999.0)
    after = replay.evaluate_fixture(fixture)
    assert after == before, "BASELINE_REFERENCE 只准展示, 改坏不得影响任何输出字段"


def test_r5_plugin_path_self_contained_and_loadable_with_tmp_home(
        monkeypatch, tmp_path):
    """P3-2: 插件绝对路径必须落在仓库根内; HOME 改 tmp 后仍能加载。"""
    import conftest

    plugin_path = Path(conftest.PLUGIN_PATH).resolve()
    assert plugin_path.is_relative_to(REPO_ROOT), (
        f"PLUGIN_PATH 必须自包含在仓库根 {REPO_ROOT} 内, got {plugin_path}")
    assert plugin_path.is_file()

    fake_home = tmp_path / "isolated-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    plugin = _load_plugin_mod()
    assert plugin.MemoryCorePrefetchProvider.name == "memorycore-prefetch"
    provider = plugin.MemoryCorePrefetchProvider()
    assert provider.is_available() is True


def test_r5_silver_fixture_zero_queries_f1_gate_blocked():
    """P3-3: 用插件闸门逐条验证 silver 200 条查询 0 条被 F1 挡住。"""
    plugin = _load_plugin_mod()
    gate = plugin.MemoryCorePrefetchProvider._is_low_information_query
    # 先证明本测试调用的闸门确实会挡低信息输入 (防恒真断言)。
    assert gate("好的，收到，明白了") is True
    assert gate("在吗") is True

    fixture = _fixture()
    blocked = []
    for i, qitem in enumerate(fixture["queries"]):
        q = str(qitem["query"])
        if gate(q):
            blocked.append((i, q))
    assert blocked == [], f"silver 中 {len(blocked)} 条查询会被 F1 闸门挡住: {blocked[:5]}"
    assert len(fixture["queries"]) == 200, "silver fixture 条数必须保持 200"
