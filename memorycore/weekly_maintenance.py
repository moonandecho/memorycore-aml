#!/usr/bin/env python3
"""MemoryCore 每周治理 (weekly_maintenance.py) — 2026-09-04 从 Hermes cron 内聚回 memorycore

编排 (替代原 mnemosyne-weekly-overflow Hermes cron job):
  1. 六步溢流 (run_overflow × memory/user)
  2. 智能整理 (溢流后热层仍 >60% 时): 规则初筛 + 可调 LLM 确认
       a) 历史条目下沉: 含明确日期(距今≥14天, 取内嵌日期/written_at 较新锚点) + 完成/退役词
          + 非保护 + 非活性豁免(Q2 分级年龄: idle 7d / warm 14d / active 30d,
          与 _select_retirement_candidates 同口径) + should_keep_local=False
          → LLM 确认过时 → 冷层查重(same 不重写 / similar 合并) → 沉冷层后删热层
       b) 高度重叠行为准则合并: 相似度≥阈值 + 均非保护 + 均 should_keep_local=False
          → LLM 生成合并文本(要点零丢失) → 原文沉冷层(写前查重) → 热层合并
     LLM 不可用 (无 LLM_API_KEY) → a) 与 b) 全部跳过: _llm_confirm_sink 返回 False → a) 也
     全部跳过 (保守行为, 无"规则强信号路径")
  3. 冷层全量治理 (run_maintenance)
  4. 报告: 写 ~/.hermes/memorycore/logs/weekly-YYYYMMDD.md + email-notify.sh

保护 (2026-09-12, DESIGN §Q4): _is_protected_rule (importance≥0.9 / 红线词 /
      行为准则词 / 用户偏好前缀 / protect_override) 条目不被 smart_tidy 内容判定
      下沉/合并, 也不参与自动沉 (六步溢流的 LRU 挤权候选前置过滤, 不再是乘数语义);
      超预算时 enforce_rule_budget 告警 budget_blocked_by_protected, 由人类处理。
保守上限: 每 target 下沉 ≤3 条、合并 ≤1 对; 拿不准一律不动。
用法: python3 weekly_maintenance.py [--dry-run] [--no-email]
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .local_store import LocalStore  # noqa: E402
from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.classifier import should_keep_local  # noqa: E402
from .core.config import (ACTIVITY_LOG_ENABLED, CHAR_LIMIT_MEMORY,  # noqa: E402
                         SOFT_THRESHOLD, TIDY_ACTIVITY_EXEMPT_DAYS,
                         TIDY_COMPLETE_AGE_DAYS, TIDY_DONE_WORDS,
                         TIDY_MAX_MERGE_PER_RUN, TIDY_MAX_SINK_PER_RUN,
                         TIDY_MERGE_RATIO,
                         JUDGE_AMBIGUOUS_REVIEW_DAYS,
                         JUDGE_AMBIGUOUS_LRU_DAYS,
                         JUDGE_AMBIGUOUS_MAX_REVIEWS)
from .core.metadata import (MetaStore, _ts_anchor,  # noqa: E402
                           load_recent_queries, entry_age_days)
from .core import llm_config  # noqa: E402
from .core.overflow import (run_overflow, _is_protected_rule, _topic_overlap,  # noqa: E402
                           _lex_evidence, _find_best_match, _merge_two_entries,
                           _recall_safe, _rule_activity_tier,
                           _handle_rule_stub_sink)
from .core.maintenance import run_maintenance  # noqa: E402

# ---- 常量 (2026-09 评审 P1: 阈值/词表收敛至 core/config.py, 消除双源漂移) ----
MEMORY_LIMIT = CHAR_LIMIT_MEMORY  # 报告输出别名 (core.config 单一真相源)
# (终审 R3) 本文件不再从 ~/.hermes/.env 读取任何键:
#  - LLM 键 (LLM_* / <PROVIDER>_API_KEY): 唯一解析器 = core.llm_config
#    (parse_dotenv 白名单 + 来源链), 与发布版一致 — 旧 _load_env 已删除,
#    它曾把文件键经 os.environ 中转 (来源被误标为 env:LLM_API_KEY), 且
#    line.lstrip("export ") 是字符集剥离 bug。
#  - 非 LLM 键 MEMORYCORE_EMBED_URL / MEMORYCORE_EMBED_MODEL: 与发布版一致,
#    只从进程环境读取 (launchd plist EnvironmentVariables 或 shell), 由
#    core/overflow.py 在 import 时绑定 (_EMBED_URL/_EMBED_MODEL 模块常量);
#    不从 .env 读取 (本地当前 .env 亦无这两键, 无功能变化)。
# 邮件通知为可选功能 (开源/参赛版默认关闭): 由环境变量
# MEMORYCORE_NOTIFY_SCRIPT 指定通知脚本 (如用户本地 email-notify.sh)。
# 代码库内不内置任何个人通知通道/邮箱地址; 未配置时报告仅落盘。
NOTIFY_SCRIPT = os.environ.get("MEMORYCORE_NOTIFY_SCRIPT", "")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def _llm_call(system: str, user: str, max_tokens: int = 900) -> str | None:
    """调 llm_config 通道 (惰性解析 + 护栏); 失败返回 None (保守跳过)。

    观测三态经 llm_config 写入当前会话 stat["llm"] 与日志 (评审必改项 5);
    未配置/每轮上限/失败退避 → None, 不再静默。
    """
    cfg = llm_config.acquire("下沉确认/合并")
    if cfg is None:
        return None
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    try:
        data = llm_config.chat(
            cfg, payload,
            timeout=float(os.environ.get("LLM_TIMEOUT", "20")))
        content = data["choices"][0]["message"]["content"].strip()
        llm_config.note_success()
        return content
    except llm_config.LLMError as e:
        llm_config.note_failure(e.category, e.detail)
        return None
    except Exception:
        llm_config.note_failure("bad_response")
        return None


def _entry_date_days(entry: str, meta: dict) -> int | None:
    """条目距今较新锚点天数: 内嵌日期与 written_at 取较小者 (较新者)。

    较新锚点语义 (评审 P4/E5): 恢复/新写条目 written_at=now → 获得全新 14 天时钟,
    不被条内老内嵌日期"复活年龄"; 无内嵌日期 → 用 written_at; 均无 → None。
    FIX6 R2: 两处时间戳统一走 _ts_anchor; 未来/损坏戳夹到 now 并计入
    ts_anomaly, 不产生"未来日期复活年龄"或负年龄漏洞。
    """
    days_list = []
    now = datetime.now(timezone.utc)
    m = _DATE_RE.search(entry)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         tzinfo=timezone.utc)
            a = _ts_anchor(d, now, field="embedded_date")
            if a is not None:
                days_list.append(max((now - a).days, 0))
        except ValueError:
            pass
    raw = meta.get("written_at")
    if raw:
        a = _ts_anchor(raw, now, field="written_at")
        if a is not None:
            days_list.append(max((now - a).days, 0))
    return min(days_list) if days_list else None


def _is_historical_done(entry: str) -> bool:
    return any(w in entry for w in TIDY_DONE_WORDS)


def _llm_confirm_sink(entry: str) -> bool:
    """LLM 确认: 该条目是否为已过时/已解决、可安全从热层下沉的历史记录。拿不准→False。"""
    sys_p = "你是记忆治理助手。判断一条记忆是否属于'已解决/已过时的历史记录, 不再需要在每轮注入的热层保留'。只判断事实状态, 不判断价值。拿不准一律输出 false。"
    out = _llm_call(sys_p, f"<entry>{entry[:400]}</entry>\n该条目可以安全下沉到冷层吗? 只输出 JSON: {{\"sink\": true/false}}")
    if not out:
        return False
    try:
        m = re.search(r"\"sink\"\s*:\s*(true|false)", out)
        return m is not None and m.group(1) == "true"
    except Exception:
        return False


def _llm_review_ambiguous(entry: str, meta: dict) -> str | None:
    """周治理 ambiguous 异步 LLM 终审: 只输出 {"type":"state|rule|ambiguous"}。

    返回 None = LLM 不可用/不确定/响应不可解析 (保守保留, 不冷迁)。
    """
    signals = meta.get("judge_signals") or {}
    try:
        sig_txt = json.dumps(signals, ensure_ascii=False)[:400]
    except Exception:
        sig_txt = "{}"
    sys_p = ("你是记忆判型终审助手。只做一件事: 判断该条目属于 "
             "'state'(已发生的历史结果/状态记录, 可下沉冷层) 还是 "
             "'rule'(仍需每轮遵守的行为要求/偏好/准则, 留热层)。"
             "拿不准输出 ambiguous。只输出 JSON: "
             '{\"type\":\"state\"|\"rule\"|\"ambiguous\"}')
    user = (f"<judge_signals>{sig_txt}</judge_signals>\n"
            f"<entry>{entry[:400]}</entry>\n输出:")
    out = _llm_call(sys_p, user, max_tokens=80)
    if not out:
        return None
    try:
        m = re.search(r'"type"\s*:\s*"(state|rule|ambiguous)"', out)
        return m.group(1) if m else None
    except Exception:
        return None


def _ambiguous_review_due(meta: dict, now: datetime) -> bool:
    # FIX6 R2: review_at 是计划性未来期限, allow_future=True (不作异常).
    ra = (_ts_anchor(meta.get("judge_review_at"), now,
                     field="judge_review_at", allow_future=True)
          if meta.get("judge_review_at") else None)
    return ra is None or now >= ra


def _tidy_ambiguous(store, client, target: str, meta_store, stat: dict,
                    dry: bool) -> None:
    """ambiguous 终审分支: 7d 复审 / 最多 2 次 / 21d stub 兜底 (B-4)。

    - 到期/到 21d → LLM 终审; state → _sink_entry (冷层写成功才删本地);
      rule → 清 ambiguous + 14d grace; 失败/不确定 → review_count+1 退避 +14d;
    - 21d 或 review_count≥2 且 LLM 不可用/仍 ambiguous → 只走 stub-sink
      (全文先入冷层, 热层留指针), 永不整条冷迁;
    - protected 条目不自动 stub/下沉 (人工语义不变), 只复审标记。
    """
    now = datetime.now(timezone.utc)
    for _k, _v in (("stubbed", 0), ("cold_errors", 0), ("errors", 0)):
        stat.setdefault(_k, _v)
    for e in list(store.entries(target)):
        meta = meta_store.get_entry(e) or {}
        if meta.get("judge_decision") != "ambiguous":
            continue
        if meta.get("judge_resolution") == "rule":
            continue
        age = entry_age_days(meta, now=now)
        review_count = int(meta.get("judge_review_count") or 0)
        a1_due = ((age is not None and age >= JUDGE_AMBIGUOUS_LRU_DAYS)
                  or review_count >= JUDGE_AMBIGUOUS_MAX_REVIEWS)
        if not (a1_due or _ambiguous_review_due(meta, now)):
            continue
        protected = _is_protected_rule(e, meta)
        if dry:
            stat.setdefault("ambiguous_dry", []).append(e[:50])
            continue
        verdict = _llm_review_ambiguous(e, meta)
        if verdict == "rule":
            # 清 ambiguous, 给 14d grace (从 judge_resolved_at 起算)
            meta_store.update_fields(
                e, judge_resolution="rule",
                judge_reviewed_at=now.isoformat(),
                judge_resolved_at=now.isoformat(),
                judge_review_count=review_count + 1,
                judge_review_at=None)
            stat["ambiguous_resolved_rule"] = stat.get(
                "ambiguous_resolved_rule", 0) + 1
            continue
        if verdict == "state" and not protected:
            _sink_entry(store, client, target, e, meta, stat, dry)
            if e not in store.entries(target):
                stat["ambiguous_sunk"] = stat.get("ambiguous_sunk", 0) + 1
                continue
            # 冷层失败: 保留, 记一次复审并退避
            meta_store.update_fields(
                e, judge_reviewed_at=now.isoformat(),
                judge_review_count=review_count + 1,
                judge_review_at=(now + timedelta(
                    days=JUDGE_AMBIGUOUS_REVIEW_DAYS)).isoformat())
            continue
        # LLM 不可用/不确定/输出 ambiguous: 到 A1 门槛 → stub 兜底 (永不全文冷迁)
        if a1_due and not protected:
            _handle_rule_stub_sink(store, client, target, e, meta_store,
                                   stat, [])
            if e not in store.entries(target):
                stat["ambiguous_stubbed"] = stat.get("ambiguous_stubbed", 0) + 1
                continue
        meta_store.update_fields(
            e, judge_reviewed_at=now.isoformat(),
            judge_review_count=review_count + 1,
            judge_review_at=(now + timedelta(
                days=JUDGE_AMBIGUOUS_REVIEW_DAYS)).isoformat())


def _llm_merge_text(a: str, b: str) -> str | None:
    """LLM 生成两条同主题准则的合并文本 (要点零丢失)。失败返回 None。"""
    sys_p = ("你是记忆整理助手。把两条同主题的记忆准则合并成一条精炼文本: "
             "保留所有要点、约束词、日期、例子, 只去重复表述; 不添加新事实; 100字以内一句话。只输出合并后文本。")
    out = _llm_call(sys_p, f"A: {a}\nB: {b}\n合并:")
    if not out or len(out) < 20:
        return None
    return out.strip().strip("\"'").strip()


def _sink_entry(store, client, target, entry, meta, stat, dry: bool) -> None:
    """下沉一条历史条目: 冷层查重 → (跳过写/合并更新/新写入) → 删热层。

    评审 P4: 复用 _recall_safe/_find_best_match/_merge_two_entries, 语义与
    _handle_cold_migration 一致 — same → 不重复写只删本地 (计数 sunk);
    similar → merge-update 后删本地; 无匹配 → remember。评审 P5: importance
    0.6 对齐 stub-sink/迁移路径 (原 0.3 为四路径孤例)。铁律: 冷层写成功才删
    本地; 任一冷层失败 → 本地保留 (错误计数)。
    """
    if dry:
        stat["sink_dry"].append(entry[:50])
        return

    def _drop_local() -> None:
        before = len(store.entries(target))
        store.remove_by_exact(target, entry)
        if len(store.entries(target)) < before:
            stat["sunk"] += 1

    try:
        existing = _recall_safe(client, entry)
        if existing:
            matched = _find_best_match(entry, existing)
            if matched:
                if matched["level"] == "same":
                    _drop_local()  # 冷层已有相同事实 → 不重复写, 只删本地
                    return
                merged = _merge_two_entries(entry, matched["content"])
                r = client.update(matched["id"], merged)
                if r.get("status") != "updated":
                    stat["errors"] += 1
                    return  # update 失败 → 本地保留
                _drop_local()
                return
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") != "stored":
            stat["errors"] += 1
            return
    except Exception:
        stat["errors"] += 1
        return
    _drop_local()


def _merge_pair(store, client, target, a, b, meta_a, stat, dry: bool) -> None:
    """合并 a+b → 合并文本: 原文沉冷层 → a 替换为合并版 → b 删除。"""
    merged = _llm_merge_text(a, b)
    if not merged:
        stat["merge_skipped"] += 1
        return
    if dry:
        stat["merge_dry"].append((a[:30], b[:30], merged[:60]))
        return
    # 原文先沉冷层 (信息零丢失); 写前查重 (评审 P4: 冷层已有 same 全文 → 不重复写)
    ok = True
    for txt in (a, b):
        try:
            existing = _recall_safe(client, txt)
            if existing:
                matched = _find_best_match(txt, existing)
                if matched and matched["level"] == "same":
                    continue  # 冷层已有相同事实 → 不重复写
            r = client.remember(txt, importance=0.5, scope="global")
            if r.get("status") != "stored":
                ok = False
        except Exception:
            ok = False
    if not ok:
        stat["errors"] += 1
        return
    entries_now = store.entries(target)
    if a not in entries_now or b not in entries_now:
        stat["errors"] += 1
        return
    store.replace(target, a, merged)
    store.remove_by_exact(target, b)
    if merged in store.entries(target) and b not in store.entries(target):
        stat["merged"] += 1


def smart_tidy(store, client, target: str, stat: dict, dry: bool) -> None:
    """智能整理: 历史下沉 + 重叠合并。

    候选过滤 (评审 P2/P3 + v2 2026-09-12, 全部"少沉/不误沉"方向):
    非保护 (_is_protected_rule) + 活性分级年龄豁免 (Q2 同口径:
    _rule_activity_tier → idle 7d / warm 14d / active 30d; 词法活跃
    不再一票豁免, 只抬门槛) + should_keep_local=False
    + 日期较新锚点 ≥14 天 + 完成态词 → LLM 确认 → 冷层查重后沉。
    """
    entries = store.entries(target)
    meta_store = MetaStore(target, memory_path=store.memory_path, user_path=store.user_path)

    # --- 0) ambiguous 终审 (B-4: 7d 复审 / 最多 2 次 / 21d stub 兜底) ---
    _tidy_ambiguous(store, client, target, meta_store, stat, dry)

    # --- a) 历史条目下沉 ---
    # 近 7 天查询 (与 LRU 挤权同口径); 日志异常 → 不豁免 (维持现状保守方向)
    try:
        active_queries = (load_recent_queries(days=TIDY_ACTIVITY_EXEMPT_DAYS)
                          if ACTIVITY_LOG_ENABLED else [])
    except Exception:
        active_queries = []
    now = datetime.now(timezone.utc)
    sink_cands = []
    for e in entries:
        meta = meta_store.get_entry(e) or {}
        if _is_protected_rule(e, meta):
            continue
        # ① v2 活性分级豁免 (Q2 同口径, 2026-09-12): 取代旧"last_active_at 新鲜
        # 或词法命中 → 一律豁免" — 活性只抬高年龄门槛 (idle 7 / warm 14 /
        # active 30), 不再一票否决; 分级年龄按类型取 (state→written_at,
        # rule→updated_at, 与 _select_retirement_candidates 同源)。
        tier, min_age = _rule_activity_tier(meta, e, active_queries, now)
        age_meta = entry_age_days(meta)
        if age_meta is not None and age_meta < min_age:
            continue
        # ② 保护面补强 (评审 P3/E4): classifier 强 keep/用户偏好前缀 → skip
        if should_keep_local(e):
            continue
        days = _entry_date_days(e, meta)
        if days is None or days < TIDY_COMPLETE_AGE_DAYS:
            continue
        if not _is_historical_done(e):
            continue
        sink_cands.append((days, e, meta))
    sink_cands.sort(key=lambda t: -t[0])  # 最老优先
    processed = 0
    for days, e, meta in sink_cands:
        if processed >= TIDY_MAX_SINK_PER_RUN:
            break
        if len(store.entries(target)) <= 3:
            break  # 热层保底, 不掏空
        if not _llm_confirm_sink(e):  # LLM 不可用/不确定 → 跳过 (纯规则保守)
            continue
        _sink_entry(store, client, target, e, meta, stat, dry)
        processed += 1

    # --- b) 重叠准则合并 (需 LLM 语义, 未配置直接跳过) ---
    # 必改项 2: 硬闸改调 resolver — 原 if not os.environ.get("LLM_API_KEY")
    # 只认 env, 文件来源 (~/.hermes/.env / config.yaml) 的 key 全部被拦;
    # 现在统一走 llm_config (env 键 + 文件白名单键都生效)。
    if not llm_config.resolve().configured:
        return
    entries = store.entries(target)
    merged_any = True
    merge_count = 0
    while merged_any and merge_count < TIDY_MAX_MERGE_PER_RUN:
        merged_any = False
        entries = store.entries(target)
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                if not (_topic_overlap(a, b) or _ratio(a, b) >= TIDY_MERGE_RATIO):
                    continue
                meta_a = meta_store.get_entry(a) or {}
                meta_b = meta_store.get_entry(b) or {}
                if _is_protected_rule(a, meta_a) or _is_protected_rule(b, meta_b):
                    continue
                # 评审 P3: 任一方 classifier 强保留 → 跳过该对 (保守, 合并不碰保留条目)
                if should_keep_local(a) or should_keep_local(b):
                    continue
                if len(a) < 60 or len(b) < 60:
                    continue
                _merge_pair(store, client, target, a, b, meta_a, stat, dry)
                merged_any = True
                merge_count += 1
                break
            if merged_any:
                break


# ---- 冷层数据完整性守卫 (2026-09-18) ----

def _cold_integrity_lines(client) -> list:
    """把「有多少记忆掉出召回范围」变成每周报告里可见的一行。

    2026-09-18 事故: 08-15 引入 working_memory 层时, 此前写入的 123 条历史条目
    从未迁入 —— 它们既召回不到、也不参与治理, 而 doctor 判「正常」, 4 周后才被
    偶然发现。适配层 stats() 现在上报 memory_rows / orphan_rows, 这里消费它。
    """
    try:
        st = client.stats() or {}
    except Exception as e:  # 冷层不可达不能让周治理整体失败
        return [f"  冷层完整性: 检查失败 ({type(e).__name__})"]
    orphan = st.get("orphan_rows")
    raw = st.get("memory_rows")
    if orphan is None:
        return ["  冷层完整性: ⚠️ 未上报 (适配层 stats 无 orphan_rows, 可能是旧版)"
                " — 无法确认是否有记忆掉出召回"]
    if orphan > 0:
        return [f"  冷层完整性: ⚠️ {orphan} 条记忆只在 memories 表、对召回与治理不可见"
                f" (raw={raw}) — 需要回填!"]
    return [f"  冷层完整性: 正常 (raw={raw} 条, 差集 0)"]


def _ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def main() -> None:
    dry = "--dry-run" in sys.argv
    no_email = "--no-email" in sys.argv
    LOG_DIR.mkdir(exist_ok=True)

    store = LocalStore()
    client = ColdStoreClient()

    # 冷层可用性 (只读探测)
    cold_ok = True
    try:
        client.stats()
    except Exception:
        cold_ok = False

    stat = {
        "overflowed": 0, "updated": 0, "deleted": 0, "merged": 0, "kept": 0, "errors": 0,
        "sunk": 0, "sink_dry": [], "merge_dry": [], "merge_skipped": 0,
    }
    # LLM 护栏会话 (评审 C2) — 覆盖 smart_tidy 的下沉确认/合并调用;
    # 状态经 close() 写入 stat["llm"]。
    # 低危修复 (终审): close 进 finally — 异常路径也写 stat["llm"] 并复位
    llm_guard = llm_config.start_session(stat=stat, name="weekly")
    lines = [f"# MemoryCore 每周治理报告 {datetime.now():%Y-%m-%d %H:%M}",
             f"模式: {'DRY-RUN(不落盘)' if dry else '执行'} | 冷层: {'OK' if cold_ok else '不可达(降级)'}"]

    try:
        for target in ("memory", "user"):
            before_pct = store.usage_pct(target)
            r = run_overflow(store, client, target)
            for k in stat:
                if k in r:
                    stat[k] += r[k]
            after_pct = store.usage_pct(target)
            lines.append(f"\n## {target}  溢流: {before_pct}% → {after_pct}%")
            # LLM 三态 (评审必改项 5): 溢流内压缩/合并/休眠判定的通路状态
            if r.get("llm"):
                lines.append(f"  LLM: {llm_config.format_status(r['llm'])}")

            # 智能整理: 溢流后仍 >60%
            if after_pct > SOFT_THRESHOLD * 100 and cold_ok:
                t_stat = {"sunk": 0, "merged": 0, "errors": 0, "sink_dry": [], "merge_dry": [], "merge_skipped": 0}
                smart_tidy(store, client, target, t_stat, dry)
                stat["sunk"] += t_stat["sunk"]
                stat["merged"] += t_stat["merged"]
                stat["errors"] += t_stat["errors"]
                for k in ("ambiguous_sunk", "ambiguous_resolved_rule",
                          "ambiguous_stubbed", "ambiguous_dry"):
                    if k in t_stat:
                        stat.setdefault(k, 0)
                        if isinstance(t_stat[k], list):
                            stat[k] += len(t_stat[k])
                        else:
                            stat[k] += t_stat[k]
                post = store.usage_pct(target)
                lines.append(f"  智能整理: 下沉={t_stat['sunk']} 合并={t_stat['merged']} 跳过合并={t_stat['merge_skipped']} → {post}%")
                if any(k in t_stat for k in ("ambiguous_sunk",
                                             "ambiguous_resolved_rule",
                                             "ambiguous_stubbed",
                                             "ambiguous_dry")):
                    lines.append(
                        f"  ambiguous 终审: 判state沉={t_stat.get('ambiguous_sunk', 0)} "
                        f"判rule={t_stat.get('ambiguous_resolved_rule', 0)} "
                        f"stub兜底={t_stat.get('ambiguous_stubbed', 0)} "
                        f"dry={len(t_stat.get('ambiguous_dry', []))}")
                if t_stat["sink_dry"]:
                    lines.append("  [dry] 拟下沉: " + "; ".join(t_stat["sink_dry"]))
                if t_stat["merge_dry"]:
                    for x in t_stat["merge_dry"]:
                        lines.append(f"  [dry] 拟合并: {x[0]} + {x[1]} → {x[2]}")
            else:
                lines.append(f"  智能整理: 跳过 (占用 {after_pct}% ≤ {SOFT_THRESHOLD*100:.0f}% 或冷层不可达)")

        # 冷层治理
        try:
            m = run_maintenance(client)
            lines.append(f"\n## 冷层治理\n{m}")
            if m.get("llm"):
                lines.append(f"  冷层 LLM: {llm_config.format_status(m['llm'])}")
        except Exception as e:
            lines.append(f"\n## 冷层治理\n异常: {e}")
            stat["errors"] += 1
        # 冷层数据完整性 (2026-09-18): 掉出召回范围的历史行
        lines.extend(_cold_integrity_lines(client))
    finally:
        llm_guard.close()

    # LLM 通路小结 (必改项 5): 三态 + 本轮调用次数/上限/是否退避 —
    # weekly 会话覆盖 smart_tidy 的下沉确认/合并调用
    lines.append(f"\n## LLM 通路 (周整理)\n{llm_config.format_status(stat['llm'])}")

    lines.append(f"\n## 汇总\n溢流溢出={stat['overflowed']} 历史下沉={stat['sunk']} 合并={stat['merged']} 错误={stat['errors']}")
    for target in ("memory", "user"):
        lines.append(f"{target}: {store.usage_pct(target)}% ({store.char_count(target)}/{MEMORY_LIMIT})")

    report = "\n".join(lines)
    print(report)

    if dry:
        return
    # 落盘报告
    fp = LOG_DIR / f"weekly-{datetime.now():%Y%m%d-%H%M}.md"
    fp.write_text(report + "\n")
    # 邮件通知 (可选): 仅当 MEMORYCORE_NOTIFY_SCRIPT 配置且脚本存在时发送;
    # 未配置/被 --no-email 禁用 → 报告已落盘, 打印提示。
    if not no_email and NOTIFY_SCRIPT:
        if not Path(NOTIFY_SCRIPT).exists():
            print(f"[warn] 通知脚本不存在: {NOTIFY_SCRIPT}", file=sys.stderr)
        else:
            try:
                r = subprocess.run(["bash", str(NOTIFY_SCRIPT), "MemoryCore 每周治理报告"],
                                   input=report, text=True, timeout=120, capture_output=True)
                if r.returncode != 0:
                    print(f"[warn] 通知发送失败 rc={r.returncode}: {r.stderr[-300:]}", file=sys.stderr)
            except Exception as e:
                print(f"[warn] 通知发送失败: {e}", file=sys.stderr)
    elif not no_email:
        print("[info] 未配置 MEMORYCORE_NOTIFY_SCRIPT, 通知跳过 (报告已落盘 logs/)")


if __name__ == "__main__":
    main()
