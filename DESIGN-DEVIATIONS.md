# DESIGN-DEVIATIONS.md — 实施轮口径澄清与偏差记录

> **Port note (2026-09-13):** this file was imported from a verified local
> implementation tree into this repository. References to internal workspace
> paths (`prod/`, `evidence/...`) refer to that tree; those files are not part
> of this repository. The behavioural semantics and the documented deviations
> themselves are the same code that passed 490 release tests.

日期：2026-09-12（实施轮）
范围：设计文档 → 实现轮（本仓库代码）
状态：以下均为**口径澄清**（设计两处表述取其一并说明依据），无实质性设计变更；未发现自相矛盾到不可实现的点。

---

## 1. S0 "state 写入即迁" 与 §3 "其余 state/rule 处理保持" 的口径取舍

- 设计冲突点：
  - Q2 S0 表格行写 "state / type_override=state | 写入即迁（冷层可达时）| 不看 | 直写路径或溢流立即冷迁移"；
  - §3 改动点清单 `_handle_typed_entry` 行写 "S0 kw-sink 门槛 HARD→SOFT；**其余 state/rule 处理保持**"。
- 实施取舍：
  - 写路径（store_fact / direct_write_govern）对 state 的立即冷迁移**本来就存在**，保持不变；
  - 溢流主循环对**普通 type=state** 条目保持既有 7 天 TTL 语义（"其余处理保持"），不提前；
  - **type_override=state**（人工标注）条目在溢流时 S0 立即冷迁移（不等待 TTL），冷层失败留热层兜底；
  - 存量 6 条（含已判 state 的 #22）由 `tools/retype_20260912.py` 一次性立即冷迁移，不在热层滞留。
- 建议：设计文档把 S0 表格行的"溢流立即冷迁移"改为"写路径/人工标注立即冷迁移；词法判型 state 按 7 天 TTL"，与 §3 措辞对齐。
- 影响：无数据风险。词法判型的 state 最多滞留 7 天（与旧版一致），人工标注与迁移清单不受 TTL 限制。

## 2. 收敛阶梯中 warm 级年龄不足（快照时点）

- 观察：快照 5 条非 protected 规则（#7/#12/#13/#15/#18）全部被近 7 天查询词法命中（warm 级，门槛 14 天），但 2026-09-12 时点 #12/#15/#18 的 updated_at 年龄只有 13/9/8 天。
- 后果：按机制运行时，47% 平台期要到约 5–7 天后才完全达成（设计"1–2 次溢流到 47%"在当天不可达）；前两轮溢流会先 stub #7/#13，其余 3 条因 warm 驻留期未满继续留热，并输出 `budget_blocked_by_protected=true`（残余含 protected，符合 Q6 合法平台期口径）。
- 实施处理：机制本身严格按设计（不放松门槛）；全链路验收测试（tests/test_e2e_sink_convergence.py）把 5 条非 protected 的 updated_at 回填 60 天以锁定收敛阶梯本身（分级门槛另由 test_lru_budget.py 单测覆盖）。
- 建议：设计 §Q6"预期时间"改为"迁移当天到 55%；随后 1–2 周内（warm 驻留期满后）2 次溢流到 47%"，或接受 plateau_reason 期间输出。

## 3. 预算挤权 kw-sink 候选的释放口径

- 设计 §3 `enforce_rule_budget` 行："候选为 state/kw-sink 时全文冷迁移（释放全文），普通 rule 才 stub"。
- 实施：预算路径候选若 `_rule_retype_eligible` → 全文冷迁移；否则若 `should_keep_local=False`（kw-sink）→ 全文冷迁移；否则 stub。与设计一致。
- 澄清：`_select_retirement_candidates` 仍只返回 rule 型条目（state 不参与 LRU 池）；"候选为 state"仅在 `type_override=state` 语义下由 `_handle_typed_entry` S0 路径承担，不进预算循环。两者合起来覆盖设计全部意图。
- 影响：无。

## 4. protected 的 `protect_override=false` 语义

- 设计 Q4 判据 2 只定义了 `protect_override is True` 加保；`false` 未定义。
- 实施：`protect_override=false` **不解除**红线硬词与 importance≥0.9 保护（安全方向：显式 false 只降级文本类保护），并在此文件中记录。
- 影响：无（纯增量定义，不影响设计所列判据）。

## 5. 独立评审整改轮（2026-09-12）B-1/G-1 实施说明

- B-1 修法：没有把 `STRONG_BEHAVIOR` 简单子串扩展成"见词就 rule"，而是：
  1. 完成态命中增加条件/时间状语护栏（`交付完成前/部署完成后/已部署前必须/已上线…必须先` 等不计完成态证据）；
  2. `_PENDING_MARKERS` 补 `待上线/待部署/待交付/待恢复/未交付/未完成/尚未完成/进行中`；
  3. 强行为词按上下文扩展：`用户要求/用户希望/用户喜欢/先确认` 直接命中；`必须/不允许/不能` 只在"动作+指令"上下文命中；`规范/原则/规则` 只认标题标签（句首或日期前缀+冒号），避免把 `#24 视觉必须用 mimo-v2.5`、`#19 规则初筛` 等技术叙述误判 rule；
  4. `direct_write_govern` / `memorycore_store_entry` 在"词法判型 state → 立即冷迁"前加 `should_keep_local()` 二次否决；显式 `type_hint/type_override=state` 不被否决。
- 为什么不用评审建议的朴素词表扩展：实测朴素扩展会把快照 `#24`（`视觉必须用…`）从 state 改判 rule，直接破坏 19/6 迁移口径；上下文护栏在 11 条反例全过的同时保住 6 条目标集合。
- B-2 修法：`用户偏好设置`、`旧习惯`、`调研结论…(技术原因)` 三类技术对象在强行为词与名词消歧两处都被排除；`should_keep_local` 的用户前缀启发同步排除 `用户偏好设置`。
- G-1：已按 DESIGN §Q6 实装（`run_overflow` stat 输出 `plateau_reason`，`_force_overflow_to_target` 读取并停止），不再是偏差。

## 6. SAFE-JUDGE v3 实施轮（2026-09-13）口径澄清

- **状态**：JUDGE-DESIGN.md §Q-A～§Q-E 已全部落地；本节只记录"设计未细写、实施必须二选一"的接口口径，不含判型机制降级。
- **6.1 `should_keep_local` 的 public/typed 双视图**：公开 `should_keep_local` 按 E-1 先调 `judge_entry`（state→False / ambiguous→True）；另提取 `should_keep_local_rule_view`，供 overflow 已有 `type=rule` sidecar 章的 S0 kw-sink 与预算分支继续按 rule 侧词法使用。原因：typed rule 若内容含完成态，应走 S2 `_handle_rule_retype`（统计 retyped/aged_sunk 保留），若直接吃 public 的 state→False 会绕过 S2 退化为 `_handle_cold_migration`，使 gate 观测失真。安全方向不变（新 reconcile/写入口判 state，仍按 state 元数据下沉）。
- **6.2 `type_source` 新值**：v3 新判型写 `judge_v3`（非 ambiguous）/`judge_v3_ambiguous`（ambiguous）；仅在 `JUDGE_V3_ENABLED=0` 回滚路径保留 `lexical_v2`。这是 E-2"新条目写 judge 字段"的审计落点，旧 sidecar 读路径不重判。
- **6.3 A0 合法平台期期限**：`_compute_plateau_reason` 只把带 `judge_review_at` 的 A0 计入合法 `ambiguous_hold`；旧格式/手工写入的无期限 ambiguous 不获得平台期豁免。与 C-4 "不允许无 review_at 的 ambiguous 永远占位"一致。
- **6.4 weekly 21d 兜底执行点**：`smart_tidy` 新增 ambiguous 终审 pass：到 7d/21d 或 `review_count>=2` → LLM；state→`_sink_entry`（冷层写成功才删）；rule→清 ambiguous+`judge_resolved_at`+14d grace；LLM 不可用/仍 ambiguous 且到 A1 门槛→只 stub-sink，不整条冷迁。A1 提前 stub 在 overflow 仍要求压力/预算触发（B-4 ③），weekly 的 21d 兜底按时限触发，二者不冲突。
- **6.5 `memorycore_store_entry` 的强制热边界**：仅 `strong rule`（band=strong）与 ambiguous 强制 hot；默认/weak rule 仍走既有 classify 冷热，避免破坏"低 importance 长尾事实可进冷层"的旧契约。ambiguous 本地 add 失败时返回 error 拒绝冷层兜底（设计禁止 ambiguous 冷迁）。
- **6.6 回滚实测**：`JUDGE_V3_ENABLED=0` attack 回到 20/29（v2 现役基线）；`JUDGE_AMBIGUOUS_HOLD=0` 无日期完成态直接 rule，不写 review_at/不丢热层。均无数据迁移动作。
- **6.7 "ambiguous 本地删除只能来自 stub-sink" 与 weekly LLM 终审 state 出口**：任务硬约束 §4.4 与设计 §B-4 出口①存在措辞张力。实施口径：判型同步路径与 overflow 自动路径严格只允许 stub-sink；weekly `smart_tidy` 在收到 LLM 明确 `state` 终审结论后按设计 §B-4① 走 `_sink_entry`（全文先写冷层确认）。为保持审计语义，同步/压力路径不会调用 `_handle_cold_migration`；任何 unresolved ambiguous 的 21d 兜底仍只 stub。

---

## 7. SAFE-JUDGE v3 独立复核整改轮（2026-09-13）

- **状态**：JUDGE-REVIEW 阻断项 1–6 全部落地，无判据放松；本节只记录复核建议中留白的口径选择。
- **7.1 label 复合模式白名单**：D-4 要求 label ∈ 闭类，或明确的"主题+标签"复合。实施为：
  NORM 侧允许"主题前缀 + NORM 闭类词尾"（误判 rule 只多留热层）；REPORT 侧只放行
  "主题 + 调研/结论"（`调研结论`、`示例项目自动化调研`）。`迁移记录`、`部署状态`、
  `完成情况`、`恢复流程` 等词尾不属于报告类标签，落回正文继续判 root 证据。
  这是对 §D-4 "REPORT 等标签" 的子集化解释，避免 REVIEW B/L 组的字符串子串误判。
- **7.2 条件触发与观察框架拆分**：D-3 条件触发严格用设计闭类
  `(完成|成功|完毕|结束)+(前|后|时)`，不维护动作动词；D-2 症状观察框架单独用
  结构式 `[动作短语 2..12 字]+(时|前|后)`，并加时间副词 lookbehind 排除
  `以后/之后/目前/同时/及时`。这样 `卸载旧驱动后重装` 仍按完成态流水，
  `重启后不能进入桌面` 仍判症状，二者不互相污染。
- **7.3 B-1.3 安全出口**：仅引号/括号内确有闭类证据（体貌/待办/道义/言语/能力/标签），
  或箭头/因此派生证据时返回 ambiguous；普通括号注释/技术列表不触发，保证存量
  快照 direct judge ambiguous=0、19/6 口径不变。选择 §B-1.3 ambiguous，而非
  §A-4 默认 rule；因此不属于偏离。
- **7.4 审计落盘**：ambiguous 的 `judge_reviewed_at` 显式写 `null`；
  `direct_write_govern` / `memorycore_store_entry` 热路径写
  `type_source=judge_v3`（ambiguous 为 `judge_v3_ambiguous`；人工标注 `manual_override`）。
  `_rule_activity_tier` 新增 `resolved_rule_grace`（`JUDGE_RESOLVED_RULE_GRACE_DAYS=14`），
  与 `_select_retirement_candidates` 的 14d skip 共用同一 config。
- **7.5 未完成项**：无。`_PENDING_RE` 的未来标记仍保留 `待/未/尚未/正在/进行中`
  闭类组合；新增领域"待办同义词"若未命中闭类算子会按安全默认 rule 处理（不冷迁），
  属设计 §A-4/§B-1.3 可接受范围。

---

## 8. 热层缓存化实施轮（2026-09-13）

- **状态**：CACHE-DESIGN/FAULT-PATH/THRESHOLDS 已按任务拍板口径落地；本节只记录实施中与设计原文/未落盘输入有关的偏差与选择，不含机制降级。
- **8.1 state 冷层优先写入（用户拍板覆盖设计原文）**：设计改动点表曾写 `direct_write_govern` "state 不再立即冷迁"；任务 §2.4 明确 state 允许冷层优先写入（属初始放置）。实施口径：写路径（`memorycore_store_entry` / `direct_write_govern`）保留冷层优先分配，冷层写成功才删本地；**已进入热层的 state 不再有 TTL 直接换出分支**，与 rule/stub 同池由统一预算换出。二者合起来既满足"初始放置"，又满足"一旦在热层必须能换出"。
- **8.2 旧 S0/S2/S5/S4 阶梯保留（仅 rule/非 protected）**：统一预算换出（新 `enforce_rule_budget`）为默认主路径；旧 rule 阶梯仅对非 protected 条目保留 S0/S2/S5/S4 语义，不影响"全 protected 仍可被预算换出"。state/ambiguous 的旧直接换出分支默认已删除（仅 `MEMORYCORE_CACHE_POLICY_V2=0` 回滚时恢复）。
- **8.3 silver 原始清单未随设计落盘**：设计阶段只提供 200/226 聚合值与策略命中表，未提供逐条标签。`tools/replay_fault_rate.py` 按 EVIDENCE §3 的聚合口径（K3+0.48=117/200、K8+0.42=172、K8+0.42∪lex=188、K20=198）确定性重建 200 条/226 对 silver；重建结果与原聚合表逐项一致后才评测。该重建属验证口径补全，评测过程不使用本实现的命中结果。
- **8.4 缺页候选与写回拆开**：按"0.42 单独不写回全文、但可作候选/本轮注入"的口径，`_recall_sync` 中选择集包含纯 S≥0.42 候选，写回热层仅限 K/H/action 共识条目；因此控制集误注入 proxy 是 union/top5 的注入率（95.3%），低于生产快照 K=5 无阈值（100%），但高于设计给出的"top1≥0.42"方向性 proxy（90.3%）。这是候选注入与 top1 口径不同导致的差异，已记录在报告中。
- **8.5 `MEMORYCORE_CACHE_POLICY_V2=0` 的 protected 回滚**：`PROTECT_SKIP_LRU` 按用户要求默认 0 且告警；为了让总开关真正回滚到本轮前路径，legacy 候选选择在 `CACHE_POLICY_V2=0` 时恢复 protected 资格豁免（不依赖 `PROTECT_SKIP_LRU` 是否显式置 1）。`PROTECT_SKIP_LRU=1` 仍保留一个发布周期并打 DeprecationWarning。
- **8.6 未完成项**：无功能未完成项；缺页回放的人工 200 对全量 adjudication 未做（设计 §C 列为可选项），当前以聚合口径重建 + 未标注控制集 proxy 替代。

---

## 9. FIX3 独立复核整改轮（2026-09-13）

- **状态**：CACHE-REVIEW.md F1–F7 已逐项处理，无判据放松；本节记录 F1 闸门边界、F5 fixture 重定基线、F7 未实装口径。
- **9.1 F1 查询闸门包含"泛闲聊/通用外部工具问答"两类扩展**：设计原文只点名 Hermes trivial gate、中文确认/低信息、系统噪声前缀；独立复核 §5.2 的 15 条清单还包含"天气/吃饭/周末/诗/头像/回复寒暄"与"Python/GIL/Excel/CSV"这类无 MemoryCore 对象关系的查询。实施在 `_is_low_information_query` 中补了 `_CHITCHAT_RE` 与 `_GENERIC_OFFTOPIC_RE` 两个通用类别（不是逐条用例文本特判），命中后只回常驻目录、不发起全文 recall。该扩展不改变 S/K/H 通道语义，只降低与目录/规则无对象关系的上下文污染。
- **9.2 F5 silver fixture 重定基线口径**：原始 `tools/replay_fault_rate.py` 按聚合配额 first-fit 重建的 200 条查询中，包含 183 条会被 F1 闸门正确拦下的低信息/噪声/闲聊/通用问答（例如 `[IMPORTANT:...]` 系统消息、`好的`、`继续`、`系统`）。为让缺页率验收与"低信息不注入"设计一致，一次性生成器 `tools/build_fault_replay_fixture.py` 在**通过 F1 闸门的查询集合**（1076/1259）上重新 first-fit 配额 117/55/16/10/2，产出 200 查询 / 226 对；K3/K8/K8∪lex/K20 四个策略命中数仍严格为 117/172/188/198（与 EVIDENCE 表一致），baseline 仍为 41.5%。fixture 已固化 dense 向量，回放不再访问 Ollama/activity，脚本 `replay_fault_rate.py` 只读 fixture。该重选只排除设计口径下"不该注入"的查询类型，未按实现命中结果筛选目标规则。
- **9.3 F7 Q4c 软优先级不单独实装（记录取舍）**：设计 Q4c 希望"写回命中条目在 `RULE_MIN_RESIDENCY_DAYS=7` 内、普通压力下软优先，硬压力立即失效"。本轮 `enforce_rule_budget` 的触发条件本身就是硬压力定义（`content_chars > _budget` 或 `usage_pct >= HARD_THRESHOLD`），不存在"普通压力下 LRU 触发"的入口；若在硬压力入口额外跳过 7 天内写回条目，会与 F2/无永久驻留口径冲突（全 protected/全写回条目仍必须可换出），也会破坏 `RULE_BUDGET_CHARS` 硬预算收敛。因此不引入单独软优先级排序分支：
  - 写回恢复已给全文更高权重 `WEIGHT_INIT+HIT_STRONG_INCREMENT=2.0` 与 `last_recall_hit_at`，并删除旧 7/14/30 天资格豁免；
  - 硬压力一旦发生，权重/活性立即按 `_rule_rank` 排序生效，不存在 7 天免疫；
  - `RULE_MIN_RESIDENCY_DAYS` 仅保留为旧常量/审计解释，不改变候选资格。
  此为设计到实现的压力口径澄清，不放松任何换出/数据安全判据。
- **9.4 F3 stub 唯一化格式**：`_make_stub` 在 ≤40 字预算内改为 `[规则指针]{主题10字}→recall("{主题前8字}-{sha256前4}")`；唯一的短哈希来自全文，保证同类前缀规则不碰撞，`_stub_topic` 仍取 `→` 前主题词，H/目录/恢复映射口径不变。

---

## 10. FIX4 缓存化整改轮（2026-09-13）

- **状态**：FIX3-REVIEW 的 P0（F1 闸门过度拦截）已收窄语义，P1（F2 真 0 预算/极多短全文）与 P2（F3 stub 碰撞）已补机制，P3 文档/测试口径已对齐；无判据放松。
- **10.1 F1 闸门收窄为“近乎确定的噪声”**：确认/低信息类改为整条 fullmatch（尾随标点允许），不再做裸子串/前缀匹配；主题/内容词（天气/周末/拍照/头像/游戏机/全家桶/python/excel/csv/电影/诗/喝什么 等）不再单独作为拦截依据，只作为“短句 + 闲聊/外部问答结构”的辅助信号；长句、含规则规范词、含 MemoryCore 对象词或动作意图的查询一律正常召回。系统噪声前缀保留，但剥离 `[IMPORTANT/ASYNC/...]` 后正文含操作语义时让位召回（FIX3-REVIEW A 组 #31）。动作触发或本地 H 句柄/主题共识命中时在 `_recall_sync` 入口直接跳过闸门；K 共识需要在召回后判定，因此可能与操作语义查询同批进入召回，不单独用主题词前置阻断。
- **10.2 F2 真 0 stub 预算的 T3 cold-only 语义**：`--budget 0` 现在通过工具同时把 `core.config.RULE_BUDGET_CHARS` 与 `overflow.RULE_BUDGET_CHARS` 置零；`_handle_rule_stub_sink` 在显式 0 指针预算下不做本地 stub，冷写确认成功后直接删除本地全文（T3 cold-only）。该模式下 `evicted_no_ptr>0` 是零本地指针预算的必然结果，含义是“冷层有全文但本地不再承诺句柄”，不是静默数据丢失；`tools/f2_snapshot_budget_replay.py` 输出 `budget_semantics=T3_cold_only_zero_pointer_budget` 显式标记。非 0 预算下“每个已换出原文必须有 ≤40 字 stub/cold_id 映射”的判据不放松。
- **10.3 F2 极多短全文/失败候选**：冷层写成功后本地 `replace` 因 5000 硬顶/容量失败时，改为安全删除本地全文（cold-only fallback），不再每轮 “冷写成功→replace 失败→errors” 卡死；阶段 1 单个候选失败只记录并继续尝试后续候选，不再立即阻塞整批。短条目（stub 比原文更长）在非 0 预算下仍优先保留 stub；只有显式 0 指针预算或 replace 失败才走 cold-only。
- **10.4 F3 stub 指纹**：`_make_stub` 从全文 sha256 取 **16 hex（64 bit）** 作可见指纹（旧 4-hex 保留的前缀不再唯一）；写回路径增加碰撞检测，若同 stub 文本已被不同 `cold_id` 占用则 salted 重新派生，连续冲突则 cold-only，绝不覆盖既有映射。旧 4-hex 碰撞 pair 在回归中已实测各自写回。
- **10.5 F5 回放口径**：`replay_fault_rate.py` 的 docstring 与实现对齐 —— silver fixture 已由生成器按 F1 口径筛选，离线回放不再重复执行 gate；`relative_drop_vs_baseline` 改为使用 fixture 内复算的 baseline，不再硬编码 41.5%（该值仅作 reference 展示）。

---

## 11. FIX7 独立复核整改轮（2026-09-13）

- **状态**：FIX7 复核阻断 1 / 重要 5 / 次要 2 已逐项处理。本节只显式记录
  需求方拍板"不为回滚路径保留 bug 兼容"（选 (b)）后的两处 legacy 行为差异，
  以及 FIX7 新增的明确例外；不包含机制降级或数据安全放松。
- **11.1 legacy 差异①（活动扫描后 `protected=True` 被保留）**：
  FIX5 的 `stamp` 是整字典覆盖，`_degraded_lexical_hits` 未显式传
  `protected` 时会丢掉原有 `protected=True`，于是中性 legacy 条目会被
  `_select_retirement_candidates_legacy` 选中。FIX6 起的合并式 `stamp`
  保留该标记，legacy 资格豁免继续把它排除。FIX7 选 (b)：不为回滚路径恢复
  "丢 protected" 的 bug 兼容；"legacy 一字未变"收窄为**候选排序主体不变**。
  证据：FIX5 / FIX7（`CACHE_POLICY_V2=0`）的实现期输出（不在本仓库）
  显示 protected 与 selected 的差异；
  同一脚本的 `candidate_order_core` 五条顺序哈希逐位相同
  `48c8f41b,b6ba2c79,cf0c7109,4384d63b,32b29c0f`。
  回归：`tests/test_fix7_runtime.py::test_i6_legacy_activity_stamp_keeps_protected_and_excludes_candidate`。
- **11.2 legacy 差异②（malformed `judge_review_at` 不再算 `ambiguous_hold`）**：
  FIX5 的 `_ambiguous_hold_valid` 只做 `bool(meta.get("judge_review_at"))`，
  malformed 字符串被视为合法 A0 期限 → `plateau_reason=ambiguous_hold`。
  FIX6+ 要求经 `_ts_anchor(..., allow_future=True)` 可解析才算 hold；
  malformed 返回 False/继续循环 → `plateau_reason=None`。FIX7 选 (b)：
  保留该安全性修复，不回退 bool 判定。证据：FIX5 的实现期输出（不在本仓库）为
  `ambiguous_hold_valid=true / plateau_reason=ambiguous_hold`，
  FIX7 legacy 的输出为 `false / null`。回归：
  `test_i6_legacy_malformed_review_at_is_not_ambiguous_hold`。
- **11.3 stub sidecar schema 不再显式写 `grace_defer_count: 0`**：
  `_handle_rule_stub_sink` 只在源 meta 已存在该键时透传，避免 legacy
  sidecar 因换形平白增键。证据（实现期输出，不在本仓库）：FIX6 为
  `has_grace_defer_count=true / value=0`，FIX7 为 `false / null`。
- **11.4 `_parse_iso` legacy 明确例外（FIX7 I2）**：
  `core/overflow.py` 的 `_select_retirement_candidates_legacy` 在
  `CACHE_POLICY_V2=0` 回滚路径读取 `judge_resolved_at` 时仍直接
  `_parse_iso`，该处已用代码注释标为"FIX7 I2 明确例外"；生产活动路径其余
  点名位置全部迁到 `_ts_anchor`。测试：
  `test_fix7_i2_legacy_direct_parse_documented_exception`。

---

## 12. FIX8 换挡轮（2026-09-13）

- **状态**：宽限从候选资格分档降级为 `_rule_rank` 排序乘数，候选饥饿修复；
  无判据放松、无数据安全口径变化。
- **12.1 宽限乘数（B1/B2）**：删除 `normals/graces` 两档、`grace_fallback`、
  `grace_defer_count` 结构计数与 `_commit_grace_deferrals`；统一候选池按
  `w_eff × protected×3 × kw_sink×0.5 × (新鲜窗口 ? GRACE_MULT : 1)`
  升序选择。`GRACE_MULT=9.0` 为设计标定值；随仓库落盘的合成快照
  `tests/fixtures/snapshot_20260912` 可复现新鲜窗口排序（标定方法随 `tests/`、`tools/` 内的用例一并落盘）。新鲜窗口
  仍为 `written_at`/`last_recall_hit_at` ≤ `RULE_MIN_RESIDENCY_DAYS`（时间戳
  统一过 `_ts_anchor`）；`reconcile_anchor_fallback=True` 语义改为“不给新鲜
  乘数”，字段与测试保留，不再是资格开关。`grace_defer_count` 仅由
  `MetaStore.stamp` 保留 legacy optional 三态；stub 换形/恢复/server 入口
  不再生成或透传该键，也没有任何选择器读取（见 §11.3 的历史行为已在
  FIX8 收窄）。
- **12.2 P5 换出口径（固定选择“独立口径”）**：`MAX_EVICT_PER_RUN` 只约束
  全文内容换出（`stat["lru_evicted"]`，含 `--budget 0` T3 cold-only 删全文），
  不包含 `_stub_gc` 删指针；`_stub_gc` 单轮独立受 `MAX_STUB_PER_RUN` 约束。
  固定测试：`tests/test_fix8_runtime.py::test_p5_budget_zero_evict_cap_scopes`
  与 `test_p5_stub_gc_has_independent_cap_and_not_lru_counted`。
- **12.3 冷层 API 时间戳例外**：`core/decay.py` 与 `core/maintenance.py`
  属于冷层展示/治理 API，继续使用原生 `datetime.fromisoformat`，不接入
  `core.metadata._ts_anchor`（未来戳不夹 now、不计 `ts_anomaly`）；热层驻留/
  换出路径必须继续使用 `_ts_anchor`。已在两模块 docstring 显式写明。
- **12.4 I5 三态例外文档**：`MetaStore.stamp` docstring 已列例外——
  `written_at` 新键默认 now / 既有非空值未传保留 / 旧值缺失保持缺失；
  `updated_at` 未传或 None 均刷新 now；`judge_decision="ambiguous"` 时
  `judge_reviewed_at`/`judge_resolution` 的 None 保留 null；其余可选键维持
  未传保留/None 清空/非 None 覆盖。P2 起非法 type 走 `_safe_entry_type`
  安全默认，永不写 `type:null`。
- **12.5 P3 内嵌日期锚点**：`reconcile`、`tools/retype_20260912._restamp`
  以及 overflow 内 `_rule_retype_eligible`/`_handle_rule_retype` 解析出的
  内嵌日期统一经 `_ts_anchor(field="embedded_date")` 后写入/比较；未来日期
  夹 now 且 `ts_anomaly` 可见，不再写 2099。

