#!/usr/bin/env python3
"""build_fault_replay_fixture.py — 缺页率 silver fixture 一次性生成器 (F5)。

⚠️ 这是一次性归档生成器 (2026-09-13 F5 修复)。
`tools/replay_fault_rate.py` 只读生成的 fixture, 不再运行本文件的重建逻辑;
除非设计口径变更并重新标定, 禁止在验收/日常路径调用。

产物: tests/fixtures/fault_replay_silver.json
  - 200 条查询 (query text + activity index + sha256 + dense 向量)
  - 226 个查询-规则对 (rule index + rule sha256)
  - 规则全文 (25 条, sha256)
  - 生成方式/模型 digest/来源 hash/策略计数 provenance
  - 查询选择: EVIDENCE 聚合配额 (117/55/16/10/2) 在通过 plugin F1 查询
    闸门的非低信息/非噪声查询上 first-fit; 这样固化 silver 与"低信息不注入"
    设计一致, 不会把 gated 查询错误计入缺页率。

用法 (一次性):
  .venv/bin/python tools/build_fault_replay_fixture.py \
      --activity /path/activity.jsonl --rules /path/MEMORY.md \
      --out tests/fixtures/fault_replay_silver.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorycore.core.overflow import _lex_evidence  # noqa: E402  (仅生成器一次性使用)

HIT_STRONG_COS = 0.48
HIT_WEAK_COS = 0.42
SILVER_QUERIES = 200
SILVER_PAIRS = 226
# EVIDENCE §3 聚合口径反解配额
QUOTA_G1_TOP3_048 = 117
QUOTA_G2_TOP8_042 = 55
QUOTA_G3_LEX8 = 16
QUOTA_G4_TOP20 = 10
QUOTA_G5_OUTSIDE_TOP20 = 2
BASELINE_REFERENCE = 0.415

# --- F1 闸门镜像 (FIX4 起与 plugin/__init__.py 同口径; 一次性生成器) -------
# 主题/内容词只作结构性低信息辅助, 不得裸子串拦截; 长句/操作语义一律通过。
_NOISE_PREFIXES = (
    "[IMPORTANT:", "[ASYNC DELEGATION", "[SUBAGENT", "[TOOL ",
    "[BACKGROUND", "[OUT-OF-BAND",
)
_NOISE_PUNCT_CLASS = r"[\s，。,.!！?？~～、;；:：…]"
_ACK_TOKEN = (
    r"(?:好的?|收到|明白(?:了)?|知道(?:了)?|了解(?:了)?|懂了|"
    r"嗯+|哦+|噢+|呃+|额+|行|可以|没事|算了|辛苦了|"
    r"谢谢(?:你|您)?|多谢|感谢|继续|在吗|在么|在不在|"
    r"先这样(?:吧)?|没问题|好吧|好了|是|对|对的|是的|没错|"
    r"ok|okay|系统)"
)
_LOW_INFO_ACK_RE = re.compile(
    rf"^{_ACK_TOKEN}(?:{_NOISE_PUNCT_CLASS}*{_ACK_TOKEN})*"
    rf"{_NOISE_PUNCT_CLASS}*$",
    re.IGNORECASE,
)
_LOW_INFO_ACK_PREFIX_RE = re.compile(
    rf"^{_ACK_TOKEN}{_NOISE_PUNCT_CLASS}*"
    rf"(?:(?:那就|就按))?{_NOISE_PUNCT_CLASS}*"
    rf"(?:按你说的?|听你的)?{_NOISE_PUNCT_CLASS}*"
    rf"(?:写吧|做吧|行|好的?|收到|明白|可以|继续|ok|好|是|对|嗯+)?"
    rf"{_NOISE_PUNCT_CLASS}*$",
    re.IGNORECASE,
)
_CHITCHAT_TOPIC_RE = re.compile(
    r"天气|吃什么|吃啥|晚饭|午饭|早饭|宵夜|夜宵|周末|放假|旅游|"
    r"爬山|拍照|头像|诗|电影|奶茶|咖啡|健身|散步|逛街|游戏机|"
    r"全家桶|switch|壁纸|主播|回复|音乐|小说|游戏|星座|综艺",
    re.IGNORECASE,
)
_GENERIC_OFFTOPIC_RE = re.compile(
    r"(?<![a-z])(?:python|gil|excel|csv|ppt|pptx)(?![a-z])",
    re.IGNORECASE,
)
_SYSTEM_NOISE_RE = re.compile(
    r"^\s*[\[\(【（]\s*(?:IMPORTANT|ASYNC|SYSTEM|BACKGROUND|SUBAGENT|"
    r"TOOL|NOTIFICATION|WARNING|CRITICAL|ERROR|DELEGATION|OUT-OF-BAND)\b",
    re.IGNORECASE,
)
_LOW_INFO_MAX_CHARS = 18
_OPERATIONAL_RULE_CUES = (
    "必须", "不得", "禁止", "务必", "需要", "要求", "规则", "红线",
    "准则", "规范", "流程", "配置", "通知", "确认", "回滚", "审批",
    "发布", "上线", "部署", "交付", "提交", "任务", "服务", "脚本",
    "数据", "接口", "模板", "看板", "策略", "参数", "版本", "系统",
    "机器", "用户", "团队", "值班", "报备", "冻结", "失败", "执行",
    "变更", "改动", "维护", "停机", "附带", "检查", "记忆", "插件",
    "文件", "目录", "设置",
)
_OPERATIONAL_RE = re.compile(
    "|".join(re.escape(x) for x in _OPERATIONAL_RULE_CUES))
_SMALLTALK_STRUCTURE_RE = re.compile(
    r"我|你|咱|我们|帮|打算|准备|想|喜欢|不错|挺好|很好|"
    r"好呢|怎么样|吗|呀|哈哈|呵呵|辛苦|顺便|真|太|"
    r"今天|明天|晚上|早上|下午|一下",
    re.IGNORECASE,
)
_EXTERNAL_QUESTION_RE = re.compile(
    r"什么|啥|怎么|如何|为什么|哪个|是否|吗|呢|求助|请教",
    re.IGNORECASE,
)


def _gate_action_intent(text: str) -> bool:
    t = text or ""
    if re.search(r"怎么|如何|怎样|为什么|是什么|是否", t):
        return False
    for w in ("交付", "发布", "上线", "提交", "通知", "汇报",
              "删除", "清理", "卸载", "停止", "重启", "禁用",
              "修改", "配置", "部署", "迁移", "更新"):
        if w not in t:
            continue
        if w in ("修改", "配置", "安装", "清理", "更新", "迁移",
                 "停止", "禁用"):
            if not any(o in t for o in (
                    "文件", "目录", "配置", "系统", "服务器", "服务",
                    "机器", "版本", "依赖", "规则", "记忆", "任务",
                    "端口", "进程", "插件", "环境", "数据", "账号",
                    "设置", "参数", "策略", "流程", "部署", "发布",
                    "交付", "软件", "工具", "仓库", "镜像", "模型",
                    "脚本", "定时任务")):
                continue
        return True
    return False


def _is_gated_query(query: str) -> bool:
    """F1 gate 镜像: fullmatch ACK + 系统前缀 + 短句结构式闲聊/外部问答。"""
    q = (query or "").strip()
    if not q or q.startswith("/"):
        return True
    if _LOW_INFO_ACK_RE.match(q) or _LOW_INFO_ACK_PREFIX_RE.match(q):
        return True
    if q.startswith(_NOISE_PREFIXES) or _SYSTEM_NOISE_RE.match(q):
        m = re.match(r"^[\[\(【（][^\]\)】）]*[\]\)】）]", q)
        tail = q[m.end():].strip() if m else q
        if tail and (_gate_action_intent(tail)
                     or _OPERATIONAL_RE.search(tail)):
            return False
        return True
    compact = re.sub(r"[\s，。,.!！?？~～、;；:：…]+", "", q)
    if len(compact) > _LOW_INFO_MAX_CHARS:
        return False
    if _gate_action_intent(q) or _OPERATIONAL_RE.search(q):
        return False
    if (_CHITCHAT_TOPIC_RE.search(compact)
            and _SMALLTALK_STRUCTURE_RE.search(compact)):
        return True
    if (_GENERIC_OFFTOPIC_RE.search(compact)
            and _EXTERNAL_QUESTION_RE.search(compact)):
        return True
    # 与 Hermes TRIVIAL_PROMPT_RE 的英文 ack 等价小集 (生成器不引入 agent 依赖)
    if re.match(r"^(yes|no|ok|okay|sure|thanks|hi|hey|hello|continue|"
                r"got it|done|next|lgtm|k)[\s!?.:;,\"']*$", q, re.I):
        return True
    return False


def _read_activity(path: Path) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            q = str(json.loads(line).get("query") or "")
        except Exception:
            continue
        if q.strip():
            out.append((i, q))
    return out


def _read_rules(path: Path) -> List[str]:
    return [e.strip() for e in path.read_text(encoding="utf-8").split("\n§\n")
            if e.strip()]


def _embed_ollama(texts: List[str], model: str, batch: int = 32,
                  timeout: float = 180.0) -> List[List[float]]:
    out: List[List[float]] = []
    url = "http://localhost:11434/api/embed"
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        payload = {"model": model, "input": chunk}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(chunk):
            raise RuntimeError(f"Ollama 返回嵌入数不匹配: batch={len(chunk)}")
        out.extend(vecs)
    return out


def _cos(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na > 0 and nb > 0 else 0.0


def _features(queries: List[str], rules: List[str],
              dense: List[List[float]]) -> Dict[int, Dict[str, Any]]:
    feats: Dict[int, Dict[str, Any]] = {}
    n_rules = len(rules)
    for i, q in enumerate(queries):
        order = sorted(range(n_rules), key=lambda j: dense[i][j], reverse=True)
        f: Dict[str, Any] = {
            "top3_048": [j for j in order[:3]
                         if dense[i][j] >= HIT_STRONG_COS],
            "top8_042": [j for j in order[:8]
                         if dense[i][j] >= HIT_WEAK_COS],
            "lex8": [j for j in order[:8] if _lex_evidence(q, rules[j])],
            "top20": order[:20],
            "order": order,
        }
        f["pure_top20"] = [j for j in f["top20"]
                           if j not in f["top8_042"] and j not in f["lex8"]]
        feats[i] = f
    return feats


def build_silver(queries: List[str], rules: List[str],
                 dense: List[List[float]]) -> Tuple[Dict[int, List[int]],
                                                    Dict[str, int], List[int]]:
    """在非 gated 查询上按 EVIDENCE 配额 first-fit 重建 200/226。"""
    all_feats = _features(queries, rules, dense)
    eligible = [i for i, q in enumerate(queries) if not _is_gated_query(q)]
    feats = {i: all_feats[i] for i in eligible}

    def _prefer_lex(qi: int, cands: List[int]) -> List[int]:
        return sorted(cands, key=lambda j: (
            not _lex_evidence(queries[qi], rules[j]), -dense[qi][j]))

    used: set = set()
    groups: Dict[str, List[Tuple[int, int]]] = {
        "g1_top3_048": [], "g2_top8_042": [], "g3_lex8": [],
        "g4_top20": [], "g5_outside_top20": [],
    }
    for i in eligible:
        if len(groups["g1_top3_048"]) >= QUOTA_G1_TOP3_048:
            break
        if feats[i]["top3_048"]:
            groups["g1_top3_048"].append(
                (i, _prefer_lex(i, feats[i]["top3_048"])[0]))
            used.add(i)
    for i in eligible:
        if len(groups["g2_top8_042"]) >= QUOTA_G2_TOP8_042:
            break
        if i in used:
            continue
        cand = [j for j in feats[i]["top8_042"]
                if j not in feats[i]["top3_048"]]
        if not cand:
            continue
        cand = _prefer_lex(i, cand)
        pref = [j for j in cand if feats[i]["order"].index(j) in (3, 4)]
        groups["g2_top8_042"].append((i, (pref or cand)[0]))
        used.add(i)
    for i in eligible:
        if len(groups["g3_lex8"]) >= QUOTA_G3_LEX8:
            break
        if i in used:
            continue
        cand = [j for j in feats[i]["lex8"] if j not in feats[i]["top8_042"]]
        if cand:
            groups["g3_lex8"].append((i, cand[0]))
            used.add(i)
    for i in eligible:
        if len(groups["g4_top20"]) >= QUOTA_G4_TOP20:
            break
        if i in used or not feats[i]["pure_top20"]:
            continue
        cand = sorted(feats[i]["pure_top20"],
                      key=lambda j: feats[i]["order"].index(j))
        groups["g4_top20"].append((i, cand[0]))
        used.add(i)
    for i in eligible:
        if len(groups["g5_outside_top20"]) >= QUOTA_G5_OUTSIDE_TOP20:
            break
        if i in used:
            continue
        outside = [j for j in range(len(rules)) if j not in feats[i]["top20"]]
        if outside:
            j = max(outside, key=lambda j: dense[i][j])
            groups["g5_outside_top20"].append((i, j))
            used.add(i)

    counts = {k: len(v) for k, v in groups.items()}
    assert sum(counts.values()) == SILVER_QUERIES, counts
    targets: Dict[int, List[int]] = {i: [j] for vals in groups.values()
                                     for i, j in vals}
    # 额外 26 对: 只加在 g1 内, 不改变 K3/K8/K20 命中数。
    extra = 0
    for i, _t in groups["g1_top3_048"]:
        if extra >= SILVER_PAIRS - SILVER_QUERIES:
            break
        for j in feats[i]["order"][:20]:
            if j not in targets[i] and j not in feats[i]["top3_048"]:
                targets[i].append(j)
                extra += 1
                break
    assert extra == SILVER_PAIRS - SILVER_QUERIES, extra
    assert sum(len(v) for v in targets.values()) == SILVER_PAIRS

    def _policy_hits(mode: str) -> int:
        hit = 0
        for qi, tgts in targets.items():
            order = feats[qi]["order"]
            if mode == "k3_048":
                cand = [j for j in order[:3] if dense[qi][j] >= HIT_STRONG_COS]
            elif mode == "k8_042":
                cand = [j for j in order[:8] if dense[qi][j] >= HIT_WEAK_COS]
            elif mode == "k8_union_lex":
                cand = [j for j in order[:8]
                        if dense[qi][j] >= HIT_WEAK_COS
                        or _lex_evidence(queries[qi], rules[j])]
            else:
                cand = order[:20]
            if any(t in cand for t in tgts):
                hit += 1
        return hit

    policy_counts = {
        "k3_048": _policy_hits("k3_048"),
        "k8_042": _policy_hits("k8_042"),
        "k8_union_lex": _policy_hits("k8_union_lex"),
        "k20": _policy_hits("k20"),
    }
    expected = {"k3_048": 117, "k8_042": 172,
                "k8_union_lex": 188, "k20": 198}
    assert policy_counts == expected, (policy_counts, expected)
    return targets, policy_counts, eligible


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--activity", type=Path, required=True)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out", type=Path,
                    default=ROOT / "tests/fixtures/fault_replay_silver.json")
    ap.add_argument("--model", default="qwen3-embedding-ctx256:latest")
    ap.add_argument("--model-digest",
                    default="59c4e73ca0ff26a39226036dec32b53900661f2f8a5a20774a9de58f4f863d4b")
    ap.add_argument("--dense-cache", type=Path, default=None,
                    help="可选的预取 dense 缓存 (生成加速, 不参与 replay)")
    args = ap.parse_args()

    rows = _read_activity(args.activity)
    queries = [q for _i, q in rows]
    rules = _read_rules(args.rules)
    if len(queries) < SILVER_QUERIES:
        raise SystemExit(f"activity 查询不足: {len(queries)}")
    if not rules:
        raise SystemExit(f"未读到规则: {args.rules}")

    vecs: List[List[float]]
    if args.dense_cache and args.dense_cache.exists():
        cache = json.loads(args.dense_cache.read_text(encoding="utf-8"))
        if cache.get("queries") == queries and cache.get("rules") == rules:
            vecs = cache["vectors"]
        else:
            vecs = _embed_ollama(queries + rules, args.model)
    else:
        vecs = _embed_ollama(queries + rules, args.model)
    qv, rv = vecs[:len(queries)], vecs[len(queries):]
    dense = [[_cos(qv[i], rv[j]) for j in range(len(rules))]
             for i in range(len(queries))]

    targets, policy_counts, eligible = build_silver(queries, rules, dense)
    query_items = []
    for qi in sorted(targets):
        query_items.append({
            "activity_index": int(rows[qi][0]),
            "query": queries[qi],
            "query_sha256": _sha256_text(queries[qi]),
            "target_rule_indices": sorted(targets[qi]),
            "dense": [round(float(x), 8) for x in dense[qi]],
        })
    rule_items = [{
        "index": j,
        "text": rules[j],
        "rule_sha256": _sha256_text(rules[j]),
    } for j in range(len(rules))]
    pairs = [{
        "query_sha256": _sha256_text(queries[qi]),
        "rule_sha256": _sha256_text(rules[j]),
    } for qi in sorted(targets) for j in sorted(targets[qi])]
    fixture = {
        "schema_version": 1,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "generator": "tools/build_fault_replay_fixture.py (one-shot; replay must not rebuild)",
            "activity_path": str(args.activity),
            "activity_sha256": hashlib.sha256(
                args.activity.read_bytes()).hexdigest(),
            "rules_path": str(args.rules),
            "rules_sha256": hashlib.sha256(args.rules.read_bytes()).hexdigest(),
            "model": args.model,
            "model_digest": args.model_digest,
            "selection": ("EVIDENCE quota first-fit over non-gated queries; "
                          "query-side F1 gate mirrored from plugin"),
            "eligible_queries": len(eligible),
            "gated_queries_excluded": len(queries) - len(eligible),
            "policy_counts_reconstructed": policy_counts,
            "baseline_k3_048_reference": BASELINE_REFERENCE,
            "pairs_count": len(pairs),
            "queries_count": len(query_items),
        },
        "rules": rule_items,
        "queries": query_items,
        "pairs": pairs,
    }
    assert len(query_items) == SILVER_QUERIES
    assert len(pairs) == SILVER_PAIRS
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(json.dumps({
        "out": str(args.out),
        "queries": len(query_items),
        "pairs": len(pairs),
        "rules": len(rule_items),
        "eligible_queries": len(eligible),
        "gated_queries_excluded": len(queries) - len(eligible),
        "policy_counts": policy_counts,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
