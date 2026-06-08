# 重构步骤 003：提取 checkout 税额计算

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 内部的「税额计算」。

目标是把 checkout 第 5 步的税额计算提取到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不引入或调用 `TaxCalculator`。
- 不支持免税品类、奢品附加税、数字服务税等细口径。
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
def _checkout_tax_amount(self, taxable_amount):
    ...
```

该方法只服务于 checkout 主链路，表达 checkout 当前第 5 步的一刀切税额口径。

## 私有方法必须保持的当前规则

`_checkout_tax_amount(taxable_amount)` 当前行为：

- 返回 `taxable_amount * self.tax`。
- 不做 `_round2`。
- 不读取 `TAX_EXEMPT_CATEGORIES`。
- 不按行计算。
- 不调用 `TaxCalculator`。

## checkout 保持的当前结构

checkout 第 5 步改为：

```python
tax_amt = self._checkout_tax_amount(t)
t = t + tax_amt
bd["tax"] = tax_amt
bd["after_tax"] = t
```

这里的 `t` 必须仍然是券后、负数兜底后的金额。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_checkout_core.py`
  - confirmed cn 单 `tax = 0.0`，total 仍为 `146.77`。
- `test/test_status_paths.py`
  - 「现状」EU luxury out_of_stock `tax = 6800.0`，total 仍为 `40800.0`。
- `test/test_pricing_rules.py`
  - 「现状」EU electronics `tax = 12.034`，total 仍为 `91.2`。
- `test/test_facade_and_config.py`
  - 「现状」旧 `OrderSystem` 实例继续使用构造时 `self.tax` 快照。
- 其他 public API、拒单、dry_run、quote、facade 测试必须保持全绿。

## 非目标

本步不做：

- 不统一 checkout 与 `TaxCalculator`。
- 不修复 checkout 不看免税品类的现状。
- 不修改 `ConfigManager.set_tax_rate` 的半生效现状。
- 不修改运费、积分、风控、库存、落库、通知逻辑。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem` 的 checkout helper 附近新增 `_checkout_tax_amount` 私有方法。
2. 将 checkout 第 5 步中的 `tax_amt = t * self.tax` 替换为调用 `_checkout_tax_amount(t)`。
3. 保留 `t = t + tax_amt`、`bd["tax"]`、`bd["after_tax"]`。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构并推送。

## 回滚点

上一步重构提交：

```text
a15dab6 refactor: extract checkout shipping fee
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
