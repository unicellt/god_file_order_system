# 重构步骤 006：提取 checkout 优惠券解析

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 内部的「优惠券参数解析」。

目标是把 checkout 第 3 步开头将 `coupon` 转成 `coup` 的逻辑提取到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不修改任何优惠券折扣规则。
- 不提取 fixed / percent / firstorder / freeship / bogo 的应用逻辑。
- 不修复 checkout 与 `calc_v1` 的 percent 阈值差异。
- 不改变 quote 忽略 coupon 的现状。
- 不改变任何测试锁住的现状。
- diff 控制在小范围内，仅触碰 `OrderSystem` 内部。
- 完成后必须运行全部特征测试：

```bash
python3 -m unittest discover -s test -v
```

## 重构后结构

新增私有方法：

```python
def _checkout_resolve_coupon(self, coupon):
    ...
```

该方法只服务于 checkout 主链路，表达 checkout 当前把输入 `coupon` 解析成优惠券 dict 的口径。

## 私有方法必须保持的当前规则

`_checkout_resolve_coupon(coupon)` 当前行为：

- 初始结果为 `None`。
- 如果 `coupon` 为假值，返回 `None`。
- 如果 `coupon` 是字符串，返回 `COUPON_CATALOG.get(coupon)`。
- 如果 `coupon` 不是字符串但为真，原样返回 `coupon`。
- 不校验返回对象是否是 dict。
- 不复制优惠券对象。
- 不读取或修改 `self._tmp`。

## checkout 保持的当前结构

checkout 第 3 步开头改为：

```python
coup = self._checkout_resolve_coupon(coupon)
if coup:
    ...
```

后续 `ct = coup.get("type")` 和所有优惠券分支必须保持原样。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_pricing_rules.py`
  - 「现状」quote 接收 coupon 但忽略 coupon，且没有副作用。
  - 「现状」checkout 与 `calc_v1` 的 percent coupon 阈值不同。
- `test/test_checkout_core.py`
  - confirmed cn 单使用 `PCT10` 后 total、breakdown、积分、副作用不变。
- `test/test_facade_and_config.py`
  - facade 入口传入的 coupon 行为不变。
- 其他 public API、拒单、缺货、税、运费、事件、审计测试必须保持全绿。

## 非目标

本步不做：

- 不提取或统一优惠券应用规则。
- 不修改 coupon catalog。
- 不修复未知 coupon 静默忽略的现状。
- 不修复非 dict coupon 可能在后续 `.get()` 报错的现状。
- 不修改税、运费、积分、风控、库存、落库、通知逻辑。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem` 的 checkout helper 附近新增 `_checkout_resolve_coupon` 私有方法。
2. 将 checkout 第 3 步开头的 `coup = None` / `if coupon` / `isinstance(coupon, str)` 块替换为调用 `_checkout_resolve_coupon(coupon)`。
3. 保留所有优惠券应用分支。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构并推送。

## 回滚点

上一步重构提交：

```text
e9738b8 refactor: extract checkout vip discount
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
