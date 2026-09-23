"""tests/test_row_role_label.py — 行内说话人标签 (AML_ROW_ROLE_LABEL)。

对应改动：Add 落库行前缀追加 \"user: \"/\"assistant: \"，让答案模型能把一行归到说话人。
本文件覆盖：
  ① 开关关闭 = 逐字节等同旧行为（项目铁律：默认关必须零影响）；
  ② 开关打开 = 两个合法 role 加标签，其它 role 一律不动（fail-safe）；
  ③ 标签可剥离还原原文（不改变内容，只加前缀）；
  ④ 行长度仍在平台单行上限之内；
  ⑤ 源码级回归护栏：Add 分片循环里确实调用了标签函数（防以后重构时被摘掉）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorycore import aml_server as aml  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "memorycore", "aml_server.py")


def test_off_is_identity(monkeypatch):
    """开关关闭时，前缀必须逐字节不变（含空前缀）。"""
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", False)
    for prefix in ("[2023-05-08 05:56] ", "", "[2023-05-08] "):
        for role in ("user", "assistant", "system", ""):
            assert aml._with_role_label(prefix, role) == prefix


def test_on_labels_both_roles(monkeypatch):
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", True)
    assert aml._with_role_label("[2023-05-08 05:56] ", "user") == "[2023-05-08 05:56] user: "
    assert aml._with_role_label("[2023-05-08 05:56] ", "assistant") == "[2023-05-08 05:56] assistant: "


def test_on_unknown_role_untouched(monkeypatch):
    """未知/空/None role 不产生伪标签。"""
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", True)
    for role in ("system", "tool", "", "   ", None, "USERX"):
        assert aml._with_role_label("[d] ", role) == "[d] "


def test_case_and_whitespace_normalised(monkeypatch):
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", True)
    assert aml._with_role_label("", " USER ") == "user: "
    assert aml._with_role_label("", "Assistant") == "assistant: "


def test_label_strip_roundtrip(monkeypatch):
    """prefix+label+正文 剥掉标签后应还原为 prefix+正文（不改内容）。"""
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", True)
    prefix, body = "[2023-05-08 05:56] ", "Hey Mel! Good to see you!"
    row = aml._with_role_label(prefix, "user") + body
    stripped = row.replace("user: ", "", 1).replace("assistant: ", "", 1)
    assert stripped == prefix + body


def test_row_length_within_platform_cap(monkeypatch):
    """最长行 = 日期前缀 + 标签 + 片段上限(300)，远小于平台单行上限 2000。"""
    monkeypatch.setattr(aml, "_ROW_ROLE_LABEL", True)
    prefix = "[2023-05-08 05:56] "
    long_text = "你好。" * 500
    frags = aml._split_fragments(long_text)
    assert frags, "长消息必须被切成片段"
    rows = [aml._with_role_label(prefix, "assistant") + f for f in frags]
    assert max(len(r) for r in rows) <= 2000
    assert max(len(r) for r in rows) <= len(prefix) + len("assistant: ") + aml._MAX_FRAGMENT_CHARS


def test_add_path_applies_label():
    """源码级护栏：Add 分片循环里确实调用了标签函数。"""
    src = open(SRC, encoding="utf-8").read()
    assert "_with_role_label(_prefix, role)" in src, (
        "Add 路径不再应用行内说话人标签：若为有意的回退，请同步删除本测试与 "
        "AML_ROW_ROLE_LABEL 文档")
    assert 'os.environ.get("AML_ROW_ROLE_LABEL"' not in src  # 统一走 _env_switch
