# 重构步骤 002：提取 checkout 运费计算

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 内部的「运费计算」。

目标是把 checkout 第 6 步的重量累计、免运费判断、`FREESHIP` 强制免邮判断提取到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不修改 `estimate_shipping()`，因为它是 quote 使用的现有口径。
- 不统一 quote 与 checkout 的运费逻辑。
- 不修复任何现有 bug。
- 不改变任何测试锁住的现状。
- diff 控制在小范围内，仅触碰 `OrderSystem` 内部。
- 完成后必须运行全部特征测试：

```bash
python3 -m unittest discover -s test -v
```

## 重构后结构

新增私有方法：

```python
def _checkout_shipping_fee(self, items, current_total):
    ...
```

`current_total` 表示 checkout 当前第 6 步进入运费计算时的金额，也就是当前现状里的加税后 `t`。

## 私有方法必须保持的当前规则

`_checkout_shipping_fee(items, current_total)` 当前行为：

- 读取配置：`SHIPPING_TABLE.get(self.region, SHIPPING_TABLE["cn"])`。
- 计算重量：对每个 item 累加 `it.get("weight", 0.5) * it["qty"]`。
- 如果 `self._tmp.get("force_freeship")` 为真：
  - 返回 `0.0`。
- 否则使用传入的 `current_total` 判断免邮：
  - 如果 `current_total >= cfg["free_threshold"]`，返回 `0.0`。
  - 否则返回 `cfg["base"] + cfg["per_kg"] * weight`。

## checkout 保持的当前结构

checkout 第 6 步改为：

```python
ship = self._checkout_shipping_fee(items, t)
t = t + ship
bd["shipping"] = ship
```

这里的 `t` 必须仍然是加税后的当前金额，不能改成税前、券前或积分后的金额。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_checkout_core.py`
  - confirmed 单 `shipping = 0.0`，total 仍为 `146.77`。
  - 「现状」`dry_run=True` 行为不变。
- `test/test_status_paths.py`
  - 「现状」out_of_stock EU luxury `shipping = 0.0`。
- `test/test_pricing_rules.py`
  - 「现状」EU electronics `shipping = 19.0`，total 仍为 `91.2`。
  - `quote` 仍走 `estimate_shipping()`，继续忽略 coupon。
- 其他 public API、拒单、facade、配置快照测试必须保持全绿。

## 非目标

本步不做：

- 不修复免运费门槛使用加税后金额的现状。
- 不统一 `estimate_shipping()` 与 checkout 运费逻辑。
- 不修改 `FREESHIP` 通过 `self._tmp["force_freeship"]` 生效的机制。
- 不修改税、积分、风控、库存、落库、通知逻辑。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem.estimate_shipping` 附近新增 `_checkout_shipping_fee` 私有方法。
2. 将 checkout 第 6 步中的运费 if/else 块替换为调用 `_checkout_shipping_fee(items, t)`。
3. 保留 `t = t + ship` 和 `bd["shipping"] = ship`。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构并推送。

## 回滚点

上一步重构提交：

```text
101e982 refactor: extract checkout line pricing
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
