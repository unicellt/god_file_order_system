# 重构步骤 001：提取 checkout 行价计算

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 内部的「逐行计价 + 品类折扣」。

目标是降低 `checkout` 主方法内部噪音，把当前 checkout 独有的行价规则移动到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不修改 `dispatch_checkout`、`calc_v0`、`calc_v1`、`calc_v2` 等入口行为。
- 不复用 `OrderSystem.price_item()`。
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
def _checkout_price_line(self, item):
    ...
```

该方法只服务于 `checkout` 主链路，表达 checkout 当前第 1 步的硬编码规则。

## 私有方法必须保持的当前规则

`_checkout_price_line(item)` 当前行为：

- 基础金额：`item["price"] * item["qty"]`
- `fresh`
  - `qty >= 3` 时乘 `0.9`
- `book`
  - `qty >= 5` 时乘 `0.8`
  - 否则 `qty >= 2` 时乘 `0.95`
- `electronics`
  - `qty >= 2` 时乘 `0.95`
  - 且 `self.region == "eu"` 时额外再乘 `0.95`
- `clothing`
  - `qty >= 4` 时乘 `0.92`
- `grocery`
  - `qty >= 6` 时乘 `0.93`
- `luxury`、`digital`、`subscription`、未知品类
  - 不打折
- 当 `self.region == "eu"` 且 `FEATURE_FLAGS["round_eu_per_item"]` 为真：
  - 对单行金额调用 `_round2`
- 返回当前单行金额。

## checkout 保持的当前结构

`checkout` 第 1 步仍然：

- 遍历原始 `items`。
- 从原 item 读取 `cat`。
- 调用 `_checkout_price_line(i)` 得到 `p`。
- 累加 `t = t + p`。
- 生成当前相同结构的 `line_items`：

```python
{"sku": i.get("sku"), "cat": c, "line": p, "qty": i["qty"]}
```

- 写入：

```python
bd["subtotal_after_cat"] = t
sub_before_discounts = t
```

其中 `sub_before_discounts` 当前没有被后续使用，本步仍保留。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_public_api.py`
  - `dispatch_checkout` legacy/current 返回结构不变。
- `test/test_checkout_core.py`
  - confirmed 单金额、line items、breakdown、库存、reservation、audit、events、user 积分不变。
  - 「现状」`dry_run=True` 仍推进 seq 且写 session。
- `test/test_status_paths.py`
  - 「现状」rejected 订单仍保留 reservation、扣库存、无 `currency`。
  - 「现状」out_of_stock 订单仍不保存，只写 `reserve failed` audit。
- `test/test_pricing_rules.py`
  - 「现状」quote 仍忽略 coupon。
  - 「现状」checkout 与 `calc_v1` percent 阈值差异不变。
  - 「现状」EU electronics 仍双折并逐行 round。
- `test/test_facade_and_config.py`
  - 「现状」CheckoutFacade 支付失败路径不变。
  - 「现状」税率配置只影响新建 `OrderSystem` 实例。

## 非目标

本步不做：

- 不统一 `price_item()` 与 checkout。
- 不合并旧 `calc_*` 实现。
- 不修复 EU electronics 双折。
- 不修复 `dry_run` 推进订单序列。
- 不修复风控拒单后库存预留不释放。
- 不修改优惠券阈值。
- 不修改状态机。
- 不修改数据库 schema。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem.price_item` 附近新增 `_checkout_price_line` 私有方法。
2. 将 `checkout` 第 1 步中的行价 if/elif 块替换为调用 `_checkout_price_line(i)`。
3. 保留 `checkout` 中 `c = i.get("cat")` 和 `line_items.append(...)` 的现有输出结构。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构。

## 回滚点

重构前基线提交：

```text
354e8ac chore: establish characterization baseline
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
