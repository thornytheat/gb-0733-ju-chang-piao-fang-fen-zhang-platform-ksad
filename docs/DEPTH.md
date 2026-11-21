# 深度登记 · 剧场票房分账台

规模为中型。巡演票务财务以一套原分账为基准，每次核销或停演退款报表到齐后核对收款与场馆应收。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 票源导入 | 发布本批报表，复核票款和源行 | `POST /api/runs`、`GET /api/intake/{run_id}` | `app/modules/m1_intake/reader.py` `app/modules/m1_intake/service.py` | `tests/test_intake.py` |
| M2 套票权益 | 查原场次资格、票场权重及冲突 | `GET /api/rights/{run_id}` | `app/modules/m2_rights/shows.py` `app/modules/m2_rights/service.py` | `tests/test_rights.py` |
| M3 入场核销 | 查最早通过的核销与拦截依据 | `GET /api/scans/{run_id}` | `app/modules/m3_scans/service.py` | `tests/test_scans.py` |
| M4 权重分账 | 按票复核分角尾差和未分配款 | `GET /api/allocation/{run_id}` | `app/modules/m4_allocation/rules.py` `app/modules/m4_allocation/service.py` | `tests/test_allocation.py` |
| M5 退款冲正 | 逐笔核对停演冲正、限额与超领原因 | `GET /api/refunds/{run_id}` | `app/modules/m5_refunds/rules.py` `app/modules/m5_refunds/service.py` | `tests/test_refunds.py` |
| M6 场馆勾稽 | 对照双账并按票、场馆、源文件复核 | `GET /api/reconcile/{run_id}` | `app/modules/m6_reconcile/ledger.py` `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py` |
| M7 结算凭证 | 查看原因码分布与源清单，下载可重算凭证 | `GET /api/evidence/{run_id}`、`GET /api/evidence/{run_id}/export` | `app/modules/m7_evidence/bundle.py` `app/modules/m7_evidence/csv_out.py` | `tests/test_evidence.py` |
| M8 拒付归集 | 登记渠道拒付、拆分与补正，核对两策略口径并下载对账单 | `POST /api/chargebacks/{run_id}/notices`、`GET /api/chargebacks/{run_id}/statement` | `app/modules/m8_chargebacks/service.py` `app/modules/m8_chargebacks/ledger.py` | `tests/test_chargebacks.py` |

## 陷阱登记

| TRAP | 样例现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | shows.csv 使用 GB18030 | 剧名乱码 | 失败回退及记录编码 | `app/modules/m1_intake/reader.py` | `tests/test_rights.py::test_gb18030_show_titles_decode_without_corruption` |
| TRAP-02 | tickets.csv 带 BOM | 首列票码读取失败 | UTF-8 BOM 解码 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_bom_and_zero_prefixed_ticket_are_preserved` |
| TRAP-03 | tickets.csv 票据备注带换行 | 一张票拆成两张 | 保留两行范围但只计一个逻辑行 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_quoted_multiline_keeps_physical_line_range` |
| TRAP-04 | tickets.csv 分段重复表头 | 重复表头被当作坏票 | 表头和平铺记录独立计数 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_repeated_header_and_empty_line_are_counted` |
| TRAP-05 | tickets.csv 缺陷行尾额外逗号 | 多列静默丢弃 | 保留原值并隔离 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_extra_column_enters_quarantine` |
| TRAP-06 | tickets.csv 有千位分组及全角金额 | 导入失败或金额错 | 格式检查与金额归一 | `app/contracts.py` | `tests/test_intake.py::test_mixed_money_formats_are_exact` |
| TRAP-07 | tickets.csv 缺款以 N/A 表示 | 缺款成为零元票 | 必填空值隔离 | `app/contracts.py` | `tests/test_intake.py::test_missing_amount_and_conflicting_tickets_are_not_funded` |
| TRAP-08 | tickets.csv 票码 00107 | 前导零丢失 | 字符串票码 | `app/contracts.py` | `tests/test_intake.py::test_bom_and_zero_prefixed_ticket_are_preserved` |
| TRAP-10 | tickets.csv 混合小数与大额 | 金额合计丢失最小单位 | Decimal 转整数分 | `app/contracts.py` | `tests/test_intake.py::test_mixed_money_formats_are_exact` |
| TRAP-11 | scans.csv 同时刻 Z 和 +08:00 | 两个时刻参与排序 | 归一 UTC 后按编号裁决 | `app/contracts.py` | `tests/test_scans.py::test_timestamp_offsets_resolve_same_logical_scan` |
| TRAP-13 | scans.csv 较早有效记录在后 | 首条到达记录占用票码 | 按业务时间重放 | `app/modules/m3_scans/service.py` | `tests/test_scans.py::test_late_arriving_earlier_scan_wins_replay` |
| TRAP-14 | tickets.csv 同票两份不同收款 | 后写覆盖 | 冲突组全隔离 | `app/deduplicate.py` | `tests/test_intake.py::test_missing_amount_and_conflicting_tickets_are_not_funded` |
| TRAP-15 | tickets.csv 元、分混用 | 收款多计百倍 | 单位转换后累计 | `app/contracts.py` | `tests/test_intake.py::test_mixed_money_formats_are_exact` |
| TRAP-23 | scans.csv 含起点和关闭时刻 | 边界二次入账 | 半开核销窗 | `app/modules/m3_scans/service.py` | `tests/test_scans.py::test_window_start_is_inclusive_and_close_exclusive` |
| TRAP-24 | tickets.csv 的 00107 拆给相同权重 | 三场取整后少分或多分 | 最大余数与场次键裁决 | `app/modules/m4_allocation/rules.py` | `tests/test_allocation.py::test_equal_remainders_use_stable_show_key_and_conserve` |
| TRAP-26 | 以 refunds.csv 已发布样例为基准提交仅表头快照 | 原退款被旧表清除 | 校验已过账冲正都保留 | `app/modules/m5_refunds/service.py` | `tests/test_refunds.py::test_previous_approval_cannot_be_erased_by_stale_submission` |
| TRAP-30 | 渠道结算后判拒付，场馆回款已发出 | 直接改写原分账或结算账 | 原分账与 `payable_cents` 冻结，只追加带符号拒付事件 | `app/modules/m8_chargebacks/service.py` | `tests/test_chargebacks.py::test_setoff_without_available_unsettled_hangs_recovery_on_channel` |
| TRAP-31 | 同一拒付通知网络重试、或被拆成多笔 | 重试/拆分各扣一次，重复扣款 | `notice_id` 幂等返回原结果；拆分父单退出归集，子单合计不超额 | `app/modules/m8_chargebacks/ledger.py` | `tests/test_chargebacks.py::test_retry_same_notice_returns_same_snapshot_and_deducts_once`、`tests/test_chargebacks.py::test_split_replaces_parent_occupy_without_double_deduction` |
| TRAP-32 | 渠道下次结账前补齐证据 | 补正删除历史或二次红冲 | reversals 独立表留痕，占用回退未结池，重复补正被拒 | `app/modules/m8_chargebacks/service.py` | `tests/test_chargebacks.py::test_reversal_releases_occupancy_but_keeps_all_records` |
| TRAP-33 | 归集写入中途存储故障 | 半成品快照覆盖旧对账单 | 通知、补正、快照单事务同生共死，失败保留上一版 | `app/modules/m8_chargebacks/ledger.py` | `tests/test_chargebacks.py::test_failed_persistence_rolls_back_and_keeps_previous_snapshot` |
| TRAP-34 | 同一批次混用两种归集策略 | 净应收口径前后不可比 | 首次登记锁定策略，混用返回 `CHARGEBACK_POLICY_MIXED` | `app/modules/m8_chargebacks/service.py` | `tests/test_chargebacks.py::test_policy_cannot_mix_within_one_run` |

## 对账规则

源逻辑行按采信、隔离、空行、表头解释完毕。实收分等于原分账与未分配票款之和，扣除停演退款后等于场馆应收与未结余额之和。额外覆盖逐票守恒、限额、跨场馆汇总和故障注入。

## 拒付归集口径

拒付是渠道侧事后事件，独立于五份 CSV 的批次流水线，存储在 `chargeback_notices`、`chargeback_reversals` 与版本化的 `chargeback_snapshots` 三张表，事件台账由通知追加式重放生成。`setoff` 下 `场馆净应收 = 原应收 + 已占用（负）`、`剩余可占用未结 = 原未结 + setoff 占用（负）`，占不满进 `待渠道追回（负）`；`reallocate` 下未结池不变，净应收按原权益权重的负向调整减少。两策略的补正都以正号红冲，使净应收、未结与待追回回到拒付前。对账单 ZIP 含通知、补正、事件、按场馆/场次/票码汇总与带 SHA-256 的 manifest，导出不修改快照。
