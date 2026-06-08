# 重构步骤 004：提取 checkout 币种字段解析

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 在 confirmed / out_of_stock 正常返回订单时写入的 `currency` 字段。

目标是把 checkout 订单组装里的内联币种读取提取到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不新增或调用 `convert_currency`。
- 不新增 `converted` / `display_currency` 字段。
- 不修复 rejected 分支没有 `currency` 字段的现状。
- 不改变任何测试锁住的现状。
- diff 控制在小范围内，仅触碰 `OrderSystem` 内部。
- 完成后必须运行全部特征测试：

```bash
python3 -m unittest discover -s test -v
```

## 重构后结构

新增私有方法：

```python
def _checkout_currency_code(self):
    ...
```

该方法只服务于 checkout 主链路的订单返回结构，表达 checkout 当前使用的币种 code 解析口径。

## 私有方法必须保持的当前规则

`_checkout_currency_code()` 当前行为：

- 返回 `CURRENCY_RATES.get(self.region, ("CNY", 1.0))[0]`。
- 未知区域回退到 `"CNY"`。
- 只返回币种 code，不返回汇率。
- 不计算本地币金额。
- 不调用模块级 `convert_currency`。

## checkout 保持的当前结构

正常订单分支中的字段改为：

```python
"currency": self._checkout_currency_code(),
```

必须保持：

- confirmed 订单包含 `currency`。
- out_of_stock 订单仍沿用同一个正常订单结构，因此也包含 `currency`。
- rejected 分支仍然不包含 `currency`。
- `total`、`breakdown`、`items`、`points_earned`、`points_used` 等字段不变。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_checkout_core.py`
  - confirmed cn 单仍返回 `currency = "CNY"`。
- `test/test_status_paths.py`
  - 「现状」out_of_stock 订单仍有 `currency = "EUR"`。
  - 「现状」rejected 订单仍没有 `currency`。
- `test/test_public_api.py`
  - `dispatch("checkout", ...)` 的返回结构不变。
- 其他 public API、计价、运费、税、积分、风控、库存、事件、审计测试必须保持全绿。

## 非目标

本步不做：

- 不统一 checkout 与 `convert_currency`。
- 不给 rejected 分支补 `currency`。
- 不修改报表、导出、显示币种逻辑。
- 不修改税、运费、积分、风控、库存、落库、通知逻辑。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem` 的 checkout helper 附近新增 `_checkout_currency_code` 私有方法。
2. 将正常订单分支中的 `CURRENCY_RATES.get(self.region, ("CNY", 1.0))[0]` 替换为调用 `_checkout_currency_code()`。
3. 保持 rejected 分支订单结构不变。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构并推送。

## 回滚点

上一步重构提交：

```text
3161a4a refactor: extract checkout tax amount
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
