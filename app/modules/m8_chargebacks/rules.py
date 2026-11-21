"""拒付归集策略。

已发出的分账（结算账 payable_cents）不可改写，两种策略都只生成独立的拒付事件：

* ``setoff`` 先占用后续未结应收：按场次键升序逐场冲减该票的未结余额，
  占不满的部分挂在渠道侧待追回；
* ``reallocate`` 重算原场次份额：按原权益权重把拒付拆成负向份额调整，
  只动原场次的净应收，不占用后续未结应收。

两类输出都以独立事件行表达，原分账与结算账本身保持不变。
"""

from app.modules.m4_allocation.rules import split_cents


def setoff_allocations(amount, pool):
    """从未结应收池中按场次键升序占用，返回 (分配, 待追回)。"""
    remaining = amount
    allocated = {}
    for show in sorted(pool):
        if remaining <= 0:
            break
        take = min(remaining, pool[show])
        if take:
            allocated[show] = take
            remaining -= take
    recoverable = remaining
    return allocated, recoverable


def reallocation_allocations(amount, original):
    """按原权益权重用最大余数法重算原场次份额，并以原分账为限额。"""
    weights = {show: row['weight'] for show, row in original.items()}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {}, amount
    shares = dict(split_cents(amount, weights))
    allocated = {show: row['allocated_cents'] for show, row in shares.items()}
    # 负向调整不得超过原场次已分金额，超出部分回到待追回，避免净应收被改成负数。
    capped, recoverable = {}, amount
    for show in sorted(allocated):
        take = min(allocated[show], original[show]['allocated_cents'])
        if take:
            capped[show] = take
        recoverable -= take
    return capped, max(recoverable, 0)
