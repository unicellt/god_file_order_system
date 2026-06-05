# 当前行为 Spec

本文档描述 `order_system.py` 当前可观察行为，用于特征测试锁定现状。这里不判断行为是否合理，也不提出重构方案。

## 测试隔离约定

- 每条特征测试必须调用 `configure_db(<临时 sqlite 路径>)`。
- 每条特征测试必须调用 `reset_state()`。
- 每条特征测试必须恢复会影响其他测试的全局配置：
  - `FEATURE_FLAGS`
  - `TAX_TABLE`
  - `COUPON_CATALOG`
- 测试只锁当前行为，不修改业务代码。

## Public API / 返回结构

### dispatch_checkout

- 当 `FEATURE_FLAGS["use_legacy_v1"] = True`：
  - 返回结构为 `{"total": <number>, "engine": "v1"}`。
  - 不返回订单 id、status、breakdown、currency。
- 当 `FEATURE_FLAGS["use_legacy_v1"] = False`：
  - 返回 `OrderSystem.checkout()` 的完整订单 dict。
  - 包含 `id`、`user`、`region`、`items`、`total`、`breakdown`、`status`、`points_earned`、`points_used`、`currency`。

## checkout 正常 confirmed 单

给定：

- region: `cn`
- items:
  - `SKU-FRESH-1`，price `20`，qty `3`，cat `fresh`，weight `1.0`
  - `SKU-BOOK-1`，price `30`，qty `5`，cat `book`，weight `0.4`
- user:
  - id `u1`
  - vip `True`
  - vip_level `2`
  - loyalty_points `500`
- coupon: `PCT10`
- use_points: `200`

当前行为：

- 订单 id 为 `ORD-1001`。
- 状态为 `confirmed`。
- total 为 `146.77`。
- 行项目金额：
  - fresh 行为 `54.0`
  - book 行为 `120.0`
- breakdown：
  - `subtotal_after_cat = 174.0`
  - `after_vip = 165.29999999999998`
  - `after_coupon = 148.76999999999998`
  - `tax = 0.0`
  - `after_tax = 148.76999999999998`
  - `shipping = 0.0`
  - `points_used = 200`
  - `points_earned = 163`
  - `risk_score = 0`
- user 会被原地修改：`loyalty_points` 从 `500` 变为 `463`。
- 库存会被扣减：
  - `SKU-FRESH-1` 从 `100` 到 `97`
  - `SKU-BOOK-1` 从 `200` 到 `195`
- `_RESERVATIONS` 会保留 `ORD-1001` 的预留记录。
- `_ORDERS` 会保存该订单。
- `_AUDIT_LOG` 追加：
  - `reserved ...`
  - `saved order ORD-1001 total=146.77`
  - `notify u1: 订单 ORD-1001 已确认，应付 146.77`
- `_EVENTS` 追加一个 `order_confirmed` 事件。

## dry_run 当前行为

标记「现状」：

- `dry_run=True` 不保存订单。
- `dry_run=True` 不扣库存。
- `dry_run=True` 不写 audit。
- `dry_run=True` 不写 events。
- `dry_run=True` 仍会推进订单序列：
  - seq 从 `1000` 到 `1001`。
- `dry_run=True` 仍会写 `_SESSION["current_user"]`。
- 返回订单仍是 `status = "confirmed"`。

## 风控拒单当前行为

标记「现状」：

给定黑名单用户购买有库存商品：

- 先预留库存。
- 再判断风控拒单。
- 返回订单 `status = "rejected"`。
- 返回订单没有 `currency` 字段。
- `points_earned` 顶层为 `0`，但 breakdown 中仍有计算出的 `points_earned`。
- 拒单订单会保存到 `_ORDERS`。
- `_RESERVATIONS` 中仍保留该订单预留。
- 库存已扣减且不会自动释放。
- `_EVENTS` 追加 `order_rejected`。
- `_AUDIT_LOG` 包含 `reserved ...` 和 `saved order ...`。

## 缺货当前行为

标记「现状」：

给定库存不足商品：

- 返回订单 `status = "out_of_stock"`。
- 返回订单包含 `currency`。
- 不保存订单到 `_ORDERS`。
- 不写 `_RESERVATIONS`。
- 不扣库存。
- 不写 events。
- `_AUDIT_LOG` 写入 `reserve failed: ...`。

## quote 当前行为

标记「现状」：

- `OrderSystem.quote(items, user, coupon=...)` 接收 `coupon` 参数。
- 当前不会应用 coupon。
- quote 不保存订单。
- quote 不扣库存。
- quote 不写 audit/events。

## 百分比券阈值分叉

标记「现状」：

- checkout 中 percent 券阈值使用 `>`。
- `calc_v1` 中 percent 券阈值使用 `>=`。
- 当券前金额刚好等于 `PCT10.threshold = 100`：
  - checkout 不打折，总额为 `100.0`。
  - `calc_v1` 打折，总额为 `90.0`。

## EU 当前特殊行为

标记「现状」：

给定 EU 区 electronics 商品，price `33.335`，qty `2`：

- checkout 会先应用 electronics 0.95 折。
- EU 区会额外再应用一次 0.95 折。
- EU 区逐行 round 后行金额为 `60.17`。
- breakdown:
  - `subtotal_after_cat = 60.17`
  - `tax = 12.034`
  - `shipping = 19.0`
  - `points_earned = 60`
- total 为 `91.2`。

## CheckoutFacade 支付失败当前行为

标记「现状」：

当底层 checkout confirmed 后，使用 `bnpl` 支付且金额超过上限：

- `CheckoutFacade.place_order` 返回：
  - `ok = False`
  - `stage = "payment"`
  - 返回订单状态被改为 `payment_failed`
  - payment 记录 `status = "declined"`
- `payment_failed` 不在 `ORDER_STATES` 中。
- 底层 checkout 已经保存订单、扣库存、写 `order_confirmed` 事件并发送一次 notify audit。
- 支付 declined 不会写 `payment_captured` 事件。
- 由于支付失败发生在 dispatcher 前，不会写 `notification_sent` 事件。

## 配置快照当前行为

标记「现状」：

- `OrderSystem.__init__` 会把 `TAX_TABLE[region]` 读入实例字段 `self.tax`。
- `ConfigManager.set_tax_rate(region, rate)` 修改全局 `TAX_TABLE`。
- 已创建的 `OrderSystem` 实例继续使用旧 `self.tax`。
- 新创建的 `OrderSystem` 实例使用新的税率。

