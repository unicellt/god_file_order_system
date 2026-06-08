# 重构步骤 005：提取 checkout VIP 折扣

## 目标

本步只重构一个职责块：`OrderSystem.checkout` 内部的「VIP 折扣」。

目标是把 checkout 第 2 步的内联 VIP 折扣分支提取到私有 helper 中。所有 public API、返回结构、金额、数据库写入、全局状态、副作用和可疑行为都必须保持不变。

## 约束

- 不修改 public API。
- 不调用已有的 `vip_discount()`。
- 不统一 checkout 与 quote 的 VIP 规则实现。
- 不修改 `VIP_TIERS` 或任何配置。
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
def _checkout_apply_vip_discount(self, total, user):
    ...
```

该方法只服务于 checkout 主链路，表达 checkout 当前第 2 步的 VIP 折扣口径。

## 私有方法必须保持的当前规则

`_checkout_apply_vip_discount(total, user)` 当前行为：

- 如果 `user.get("vip")` 为假，原样返回 `total`。
- 如果是 VIP，`vip_level` 缺省值仍为 `1`。
- `vip_level == 1` 时乘 `0.98`。
- `vip_level == 2` 时乘 `0.95`。
- `vip_level == 3` 时乘 `0.9`。
- `vip_level == 4` 时乘 `0.88`。
- `vip_level >= 5` 时乘 `0.85`。
- 其他 VIP 等级不匹配任何分支时，原样返回 `total`。
- 不做 `_round2`。
- 不读取 `VIP_TIERS`。
- 不调用 `vip_discount()`。

## checkout 保持的当前结构

checkout 第 2 步改为：

```python
t = self._checkout_apply_vip_discount(t, user)
bd["after_vip"] = t
```

必须保持 `bd["after_vip"]` 的写入位置和数值不变。

## 必须保持不变的特征测试行为

本步完成后，以下测试必须继续全绿：

- `test/test_checkout_core.py`
  - confirmed cn 单 `breakdown["after_vip"]` 仍为 `165.29999999999998`。
  - total、积分、审计、事件、库存副作用不变。
- `test/test_public_api.py`
  - `calc_v0`、`calc_v1`、`checkout`、`dispatch` 行为不变。
- `test/test_pricing_rules.py`
  - quote 仍使用原来的 quote 路径，不受本步 helper 影响。
- 其他拒单、缺货、配置、facade 测试必须保持全绿。

## 非目标

本步不做：

- 不统一 checkout 和 `vip_discount()`。
- 不把 VIP 折扣改成读取 `VIP_TIERS`。
- 不修改优惠券、税、运费、积分、风控、库存、落库、通知逻辑。
- 不新增功能。

## 实施计划

1. 在 `OrderSystem` 的 checkout helper 附近新增 `_checkout_apply_vip_discount` 私有方法。
2. 将 checkout 第 2 步内联 VIP 分支替换为调用 `_checkout_apply_vip_discount(t, user)`。
3. 保留 `bd["after_vip"] = t`。
4. 运行全部特征测试。
5. 若测试失败：
   - 先指出是哪条现状被改变；
   - 只做最小修复；
   - 再次运行全部特征测试。
6. 测试全绿后提交本步重构并推送。

## 回滚点

上一步重构提交：

```text
fce1bc9 refactor: extract checkout currency code
```

如果本步重构出现不可接受的行为变化，可回滚到该提交。
