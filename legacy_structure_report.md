# order_system.py 遗留代码现状阅读报告

目标文件：`order_system.py`

说明：本文档只描述现状，用于重构前建立认知和补特征测试。不包含重构方案，也不修正任何可疑行为。

## 1. Public API / 入口清单

名义公共 API 在 `__all__` 中维护，但文件内很多全局变量、表封装对象和内部函数也可能被历史调用方直接 import，因此真实调用面比 `__all__` 更大。

### 主要下单入口

- `dispatch_checkout(items, user, coupon=None, region="cn", **kw)`
  - 对外统一入口。
  - 受 `FEATURE_FLAGS["use_legacy_v1"]` 影响。
  - 关闭 legacy 时返回 `OrderSystem.checkout()` 的订单 dict。
  - 开启 legacy 时返回 `{"total": ..., "engine": "v1"}`，结构不同。

- `OrderSystem(region="cn", store=None)`
  - 核心 god class。
  - 下游可能直接依赖的方法包括：
    - `checkout`
    - `quote`
    - `price_item`
    - `vip_discount`
    - `estimate_shipping`
    - `risk_score`
    - `reserve`
    - `release`
    - `earn_points`
    - `burn_points`
    - `save_order`
    - `load_order`
    - `notify`
    - `report_last`

- `CheckoutFacade.place_order(...)`
  - 门面入口，串起校验、下单、支付、仓储路由、通知、小票。
  - 行为比底层 checkout 更多，也会产生额外副作用。

### 旧结算入口

- `calc_v0(items, user, region="cn")`
- `calc_v1(items, user, coupon=None, region="cn")`
- `calc_v2(items, user, coupon=None, region="cn", use_points=0)`
- `legacy_calc(items, user, coupon=None, region="cn")`
- `legacy_calc_total(cart, customer, region="cn")`
- `get_price(items, user, **kw)`
- `quick_total(items)`

这些函数是兼容层或旧报表入口，金额口径和主链路不完全一致。

### 报价 / 对账入口

- `OrderSystem.quote(...)`
- `PriceQuoteBuilder.build(...)`
- `checkout_v3_experimental(...)`
- `RegionalSettlement.settle(region, items, user)`
- `RegionalSettlement.settle_cn/us/eu/uk/jp/au/sg(...)`
- `SettlementReconciler.run(...)`
- `SettlementReconciler.report(...)`

这些入口看起来也能“算价”，但和正式 `OrderSystem.checkout` 口径不同。

### 校验 / 配置 / 运营入口

- `CartValidator.validate(...)`
- `ConfigManager.get_flag/set_flag/enable_legacy/disable_legacy/set_tax_rate/snapshot`
- `CouponIssuer.issue_fixed/issue_percent/revoke/validate_code`
- `InventoryAdmin.restock/adjust/low_stock/reconcile`
- `GiftCardService.issue/balance/redeem/apply_to_order`
- `LoyaltyManager.tier_of/earn/redeem/expire/user_history`

### 订单 / 支付 / 售后入口

- `OrderRepository.save/get/delete/list/page/total_amount`
- `PaymentSimulator.authorize/refund_payment/status_of`
- `RefundEngine.refund`
- `DisputeCenter.open_case/resolve/reconsider_risk`
- `OrderStateMachine.can/transition/is_terminal`

### 报表 / 展示 / 工具入口

- `ReportBuilder`
- `Analytics`
- `TaxFilingReport`
- `ReceiptPrinter`
- `NotificationDispatcher`
- `render_notification`
- `format_money`
- `country_to_region`
- `health_check`
- `audit_config_consistency`
- `seed_demo_data`
- `demo_full_pipeline`
- `migrate_v1_orders_to_v2`
- `convert_currency`
- `summarize_user`
- `cart_weight`
- `cart_item_count`
- `distinct_categories`
- `estimate_delivery_days`

### 可能被直接依赖的全局对象

- 配置表：`CATEGORY_RULES`、`VIP_TIERS`、`COUPON_CATALOG`、`TAX_TABLE`、`CURRENCY_RATES`、`SHIPPING_TABLE`、`FEATURE_FLAGS` 等。
- 存储对象：`_INVENTORY`、`_RESERVATIONS`、`_AUDIT_LOG`、`_EVENTS`、`_ORDERS`、`_GIFT_CARDS`、`_PAYMENTS`。
- 数据库控制：`configure_db`、`reset_state`。

## 2. 职责板块拆分

### 结算主链路

散落位置：

- `OrderSystem.checkout`
- `dispatch_checkout`
- `CheckoutFacade.place_order`

职责包含：行价、品类折扣、VIP、优惠券、负数兜底、税、运费、积分、风控、库存预留、持久化、通知。

### 价格和报价

散落位置：

- `OrderSystem.price_item`
- `OrderSystem.quote`
- `calc_v0`
- `calc_v1`
- `calc_v2`
- `legacy_calc`
- `legacy_calc_total`
- `get_price`
- `quick_total`
- `checkout_v3_experimental`
- `RegionalSettlement`
- `PriceQuoteBuilder`
- `SettlementReconciler`
- `PriceExplainer`

同一购物车可以通过多条路径得出不同 total。

### 存储和仓储

散落位置：

- sqlite 封装：`_InventoryTable`、`_ReservationTable`、`_AuditTable`、`_EventTable`、`_OrderTable`、`_GiftCardTable`、`_PaymentTable`
- `OrderRepository`
- `LegacyDBAdapter`
- `_conn`
- `_ensure`
- `configure_db`
- `reset_state`

订单、库存、预留、审计、事件、礼品卡、支付和序列都落在 sqlite 中。

### 库存和仓配

散落位置：

- `OrderSystem.reserve`
- `OrderSystem.release`
- `InventoryAdmin`
- `WarehouseRouter`
- `InventoryForecast`
- `ShippingLabelPrinter`
- `RefundEngine.refund`
- `DisputeCenter.resolve`

库存存在两套概念：全局 `_INVENTORY` 和仓库级 `WAREHOUSE_STOCK`。

### 税和运费

散落位置：

- checkout 内部的 `self.tax` 和 `SHIPPING_TABLE`
- `TaxCalculator`
- `ShippingCalculator`
- `RegionalSettlement`
- `PriceQuoteBuilder`
- `TaxFilingReport`

主链路使用粗口径，细口径类存在但未被主 checkout 使用。

### 促销、券、礼品卡和套餐

散落位置：

- `COUPON_CATALOG`
- checkout 内部 coupon 分支
- `PROMO_STACK_MATRIX`
- `PromoEngine`
- `CouponIssuer`
- `GiftCardService`
- `BundleCatalog`
- `GiftWrapService`
- `SEASONAL_PROMOS`
- `TIERED_PROMO`

优惠券、季节促销、阶梯折扣、满赠、礼品卡、套餐、包装加价是多套系统。

### 风控

散落位置：

- `OrderSystem.risk_score`
- `ExtRiskEngine`
- `RISK_RULES`
- `EXT_RISK_RULES`
- `DisputeCenter.reconsider_risk`
- `PriceQuoteBuilder.build`

正式 checkout 暴露的是 `OrderSystem.risk_score` 口径，扩展风控仅在其他入口使用。

### 积分和客户

散落位置：

- `OrderSystem.earn_points`
- `OrderSystem.burn_points`
- checkout 内直接修改传入的 `user`
- `LoyaltyManager`
- `CustomerSegmentation`
- `OrderEnricher`
- `REDEEM_CATALOG`

积分发放和兑换有两套实现，且 checkout 会直接修改调用方传入的 user dict。

### 支付、状态、退款和争议

散落位置：

- `PaymentSimulator`
- `OrderStateMachine`
- `RefundEngine`
- `DisputeCenter`
- `RETURN_RULES`
- `PAYMENT_METHODS`

checkout 直接写状态字符串，没有通过状态机。

### 通知、报表、展示

散落位置：

- `OrderSystem.notify`
- `NotificationDispatcher`
- `render_notification`
- `NOTIFY_TEMPLATES`
- `ReceiptPrinter`
- `ReportBuilder`
- `Analytics`
- `TaxFilingReport`
- `PriceExplainer`
- `format_money`

通知和报表分别读订单表、事件流、审计日志，GMV 口径可能不一致。

## 3. 状态地图

### sqlite 数据库

默认路径：

- `_DEFAULT_DB_PATH = <模块目录>/order_system.db`

连接状态：

- `_db_state = {"path": ..., "conn": ...}`

表：

- `orders(id TEXT PRIMARY KEY, data TEXT)`
- `inventory(sku TEXT PRIMARY KEY, stock INTEGER NOT NULL)`
- `reservations(order_id TEXT PRIMARY KEY, need TEXT)`
- `audit(id INTEGER PRIMARY KEY AUTOINCREMENT, msg TEXT)`
- `events(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)`
- `gift_cards(code TEXT PRIMARY KEY, balance REAL)`
- `payments(order_id TEXT PRIMARY KEY, data TEXT)`
- `seq(name TEXT PRIMARY KEY, val INTEGER)`

初始化行为：

- `_ensure` 建表。
- 如果库存为空，灌入 `SEED_INVENTORY`。
- 如果订单序列不存在，写入 `seq('order', 1000)`。

### 全局持久化对象

- `_INVENTORY`：读写 `inventory` 表。
- `_RESERVATIONS`：读写 `reservations` 表。
- `_AUDIT_LOG`：追加/读取 `audit` 表。
- `_EVENTS`：追加/读取 `events` 表。
- `_ORDERS`：读写 `orders` 表。
- `_GIFT_CARDS`：读写 `gift_cards` 表。
- `_PAYMENTS`：读写 `payments` 表。

### 全局配置

- `CATEGORY_RULES`
- `VIP_TIERS`
- `COUPON_CATALOG`
- `TAX_TABLE`
- `CURRENCY_RATES`
- `SHIPPING_TABLE`
- `LOYALTY_RATE`
- `LOYALTY_TIER_BONUS`
- `RISK_RULES`
- `FEATURE_FLAGS`
- `SEED_INVENTORY`
- `COUNTRY_TO_REGION`
- `TAX_EXEMPT_CATEGORIES`
- `PROMO_STACK_MATRIX`
- `SHIPPING_ZONES`
- `LOCALE_FORMATS`
- `RETURN_RULES`
- `REDEEM_CATALOG`
- `PAYMENT_METHODS`
- `ROUNDING_POLICY`
- `SUBSCRIPTION_PLANS`
- `WAREHOUSES`
- `WAREHOUSE_STOCK`
- `BUNDLES`
- `GIFT_WRAP`

### 内存状态

- `_CACHE`：目前定义但主流程基本未使用。
- `_SESSION`
  - `current_user`：checkout 开始时写入。
  - `last_region`：`OrderSystem.__init__` 写入。
- `OrderSystem._tmp`
  - checkout 内用于 `force_freeship`。
- `OrderSystem._last_breakdown`
  - `report_last` 读取。
- `LoyaltyManager.history`
  - 仅实例内存。
- `DisputeCenter.cases`
  - 仅实例内存。

### 事件和日志

审计日志：

- `_audit(msg)` 写入 `_AUDIT_LOG`。
- 库存预留、释放、保存订单、通知、退款、补货、配置修改、支付、争议、礼品卡、包装等路径都会写。

事件流：

- `_emit(kind, payload)` 写入 `_EVENTS`。
- 常见事件：
  - `order_confirmed`
  - `order_rejected`
  - `order_refunded`
  - `giftcard_issued`
  - `subscription_renewed`
  - `payment_captured`
  - `payment_refunded`
  - `dispute_opened`
  - `notification_sent`

### 隐式输入

- `FEATURE_FLAGS` 改变 checkout 行为。
- `TAX_TABLE` 在 `OrderSystem.__init__` 时被读成 `self.tax` 快照。
- `COUPON_CATALOG` 可被 `CouponIssuer` 修改。
- `SEED_INVENTORY` 影响 reset 和库存对账。
- sqlite 当前路径由 `configure_db` 决定。
- 传入的 `user` 是可变 dict，会被 checkout、积分、兑换等路径修改。

### 隐式输出

- 推进订单号：`_next_order_id` 更新 `seq`。
- 扣库存：`OrderSystem.reserve` 写 `_INVENTORY` 和 `_RESERVATIONS`。
- 保存订单：`save_order` 写 `orders`。
- 修改用户积分：checkout confirmed 时直接改 `user["loyalty_points"]`。
- 写 audit/events/payments/gift_cards。
- 一些服务会直接改传入的 `order` dict，例如礼品卡、礼品包装、富化、争议。

## 4. 现有系统里可能并存的多套实现或多套规则

### 结算实现并存

- `OrderSystem.checkout`：当前主链路。
- `calc_v0`：最早版，只有品类折扣、VIP、粗税。
- `calc_v1`：支持固定/百分比券，VIP 只有三档。
- `calc_v2`：加积分，但无运费、风控、库存。
- `checkout_v3_experimental`：组合 `TaxCalculator`、`ShippingCalculator`、`PromoEngine`。
- `RegionalSettlement`：按区域各写一套。
- `PriceQuoteBuilder`：前端完整报价，但口径和真实 checkout 不一样。
- `CheckoutFacade`：在 checkout 外再包支付、仓储、通知。

### 品类折扣规则并存

- `CATEGORY_RULES` 配置表。
- `OrderSystem.price_item` 读配置表。
- `OrderSystem.checkout` 硬编码规则。
- `calc_v0/v1/v2` 各自复制。
- `RefundEngine._reprice_line` 再复制。
- `RegionalSettlement._lines` 再复制。
- `PriceQuoteBuilder` / `checkout_v3_experimental` 再写一套。

差异示例：

- EU electronics 在 checkout 中有额外 0.95 折，其他大多数路径没有。
- `calc_v0` 的 book 只有一档 0.95，没有 `qty >= 5` 的 0.8。
- `calc_v2` 支持 clothing，但不支持 grocery。

### VIP 规则并存

- `VIP_TIERS` 支持 0-5 级。
- checkout 硬编码 1/2/3/4/>=5。
- `calc_v1/calc_v2` 对 `lv >= 3` 都是 0.9。
- `calc_v0` VIP 一刀切 0.95。
- `LoyaltyManager.tier_of` 根据积分推导等级，和用户传入的 `vip_level` 是两种来源。

### 优惠 / 促销规则并存

- checkout 支持 `fixed`、`percent`、`firstorder`、`freeship`、`bogo`。
- `calc_v1/calc_v2` 只支持 fixed/percent。
- `PROMO_STACK_MATRIX` 描述叠加关系，但 checkout 没有真正按矩阵控制。
- `PromoEngine` 有季节券、阶梯折扣、满件赠。
- `PriceQuoteBuilder` 用 `PromoEngine.best_of`，不是 checkout 的 coupon 逻辑。

### 税规则并存

- checkout 用 `t * self.tax`，不看免税品类。
- `TaxCalculator` 支持免税品类、奢品附加税、数字/订阅附加税。
- `RegionalSettlement` 部分区域读 `TAX_EXEMPT_CATEGORIES`。
- `TaxFilingReport` 从订单 breakdown 中读 checkout 产生的粗税。

### 运费规则并存

- checkout 用 `SHIPPING_TABLE` 的 base/per_kg/free_threshold。
- `OrderSystem.quote` 也用 `SHIPPING_TABLE`，但免运费判断基于券前 subtotal。
- checkout 免运费判断基于加税后的当前 `t`。
- `ShippingCalculator` 用 `SHIPPING_ZONES`、province、express、阶梯重量费。
- `RegionalSettlement` 每个区域硬编码一套运费。

### 风控规则并存

- `OrderSystem.risk_score`：黑名单、大额、件数、新客大额、奢品大额。
- `ExtRiskEngine`：在上述基础上增加 `many_skus`、`points_drain` 等命中明细。
- `PriceQuoteBuilder` 用扩展风控。
- checkout 用主风控。

### 通知规则并存

- `OrderSystem.notify`：写 audit，发 `order_confirmed` 事件。
- `NotificationDispatcher.dispatch`：按用户偏好/手机号/邮箱选渠道，发 `notification_sent` 事件。
- `render_notification` 使用多语言模板。
- `CheckoutFacade` 会在 checkout 已通知后再次调用 `NotificationDispatcher`。

### 状态规则并存

- checkout 直接写 `confirmed/out_of_stock/rejected`。
- `PaymentFacade` 支付失败时写 `payment_failed`，但这个状态不在 `ORDER_STATES` 中。
- `OrderStateMachine` 定义合法流转，但主 checkout 不走它。
- `DisputeCenter` 会写 `refunded/dispute_rejected`。

### 舍入规则并存

- `_round2` 使用 Python `round`。
- checkout 的 EU 会逐行 round 且总额 round。
- `MoneyPolicy` 提供 half_even/half_up/ceil，但 checkout 没有统一使用它。
- `format_money` 对 jp 显示 0 位小数。

## 5. 可疑行为清单

这些行为看起来像 bug，但重构前应该用特征测试先锁住。

1. `checkout(dry_run=True)` 仍会调用 `_next_order_id()`，推进数据库 `seq`。
2. `checkout(dry_run=True)` 仍会写 `_SESSION["current_user"]`。
3. `OrderSystem.__init__` 会写 `_SESSION["last_region"]`。
4. 风控在库存预留之后执行；被风控拒单时，已经预留的库存不会释放。
5. 风控拒单会保存订单，但返回订单缺少 `currency` 字段。
6. 缺货单设置 `status = "out_of_stock"`，但普通路径只保存 confirmed 订单，因此缺货订单可能不落库。
7. `reserved` 变量在 checkout 中赋值后没有实际参与后续逻辑。
8. `sub_before_discounts` 赋值后没有使用。
9. `OrderSystem.quote` 接收 `coupon` 参数，但完全不应用优惠券。
10. `OrderSystem.quote` 的免运费基于券前 subtotal，checkout 的免运费基于加税后的当前金额。
11. checkout 的 fixed 券门槛使用 `>=`，percent 券门槛使用 `>`。
12. `calc_v1` 的 percent 券门槛使用 `>=`，和 checkout/v2 不同。
13. BOGO 只减同品类最便宜一件的单价，而不是按购买数量成对处理。
14. `CartValidator` 会报无效券码，但 `dispatch_checkout` 不强制校验。
15. 负价格、负数量如果绕过 validator，checkout 仍可能参与计算。
16. `burn_points` 对 `want < 0` 没有保护，可能出现负积分抵扣。
17. checkout confirmed 时直接修改传入的 `user["loyalty_points"]`。
18. `ConfigManager.set_tax_rate` 改的是全局 `TAX_TABLE`，但已创建的 `OrderSystem` 使用旧 `self.tax`。
19. `CouponIssuer` 会直接修改全局 `COUPON_CATALOG`，影响后续所有调用。
20. `reset_state` 会清库并重建，但不重置被 `CouponIssuer` 或 `ConfigManager` 改过的全局配置表。
21. `GiftCardService.apply_to_order` 会事后修改订单 total，但不更新 breakdown、支付记录或 GMV 事件。
22. `GiftWrapService.apply` 会事后增加订单 total，但不更新 breakdown、支付记录或 GMV 事件。
23. `SubscriptionBilling.renew` 创建 confirmed 订单，但不保存到 `_ORDERS`。
24. `PaymentSimulator.authorize` 支付失败会写 `_PAYMENTS`，但不会发事件或改订单状态；`CheckoutFacade` 才会改成 `payment_failed`。
25. `payment_failed` 不在 `ORDER_STATES` 中。
26. `CheckoutFacade.place_order` 会产生两套通知：checkout 内部通知一次，facade 后续 dispatcher 再通知一次。
27. `RefundEngine.refund` 按 return_items 重新计价，不按原订单行或实付比例严格反算。
28. `RefundEngine.refund` 会补库存，但没有更新原订单状态。
29. `DisputeCenter.resolve("refund")` 会补库存并发退款事件，但不处理支付退款。
30. `ReportBuilder.gmv` 读 store confirmed，`gmv_from_events` 读事件流，两者可能长期不一致。
31. `ReportBuilder.to_csv` 和 `to_json_like` 手拼字符串，遇到逗号、引号等可能产生坏格式。
32. `country_to_region` 对未知国家码静默兜底到 `cn`。
33. `RegionalSettlement.settle` 对未知 region 静默兜底到 `cn`。
34. `format_money` 对欧洲格式只替换千分位分隔符，小数点仍是 `.`。
35. `_CACHE` 存在但几乎未使用，可能是历史残留状态点。

## 6. 第一批应该补的特征测试

目标是先锁住现有行为，尤其是金额、返回结构和副作用。

### 入口兼容测试

- `dispatch_checkout` 在 `FEATURE_FLAGS["use_legacy_v1"] = False` 时返回完整订单 dict。
- `dispatch_checkout` 在 `FEATURE_FLAGS["use_legacy_v1"] = True` 时返回 `{"total": ..., "engine": "v1"}`。
- `legacy_calc`、`legacy_calc_total`、`get_price`、`quick_total` 的当前返回值。

### checkout 正常单测试

覆盖：

- 行价和品类折扣。
- VIP 折扣。
- fixed/percent/firstorder/freeship/bogo 券。
- 税。
- 运费。
- 积分使用和积分发放。
- 库存扣减。
- 订单保存。
- audit 写入。
- event 写入。
- 传入 user 的积分变更。

### dry_run 特征测试

锁住：

- 不扣库存。
- 不保存订单。
- 不发通知事件。
- 会递增订单序列。
- 会写 `_SESSION["current_user"]`。

### 风控和库存顺序测试

场景：

- 黑名单用户。
- 超硬上限金额。
- 超软上限金额。
- 件数过多。
- 新客大额。
- 奢品大额。

重点锁住：

- `risk_score` 数值。
- `status = "rejected"`。
- rejected 是否保存。
- rejected 是否保留库存预留。
- rejected 订单字段是否缺 `currency`。

### 缺货测试

锁住：

- 库存不足时返回 `status = "out_of_stock"`。
- 库存不扣减。
- reservation 不写入。
- 订单是否不保存。
- audit 中 `reserve failed` 文案。

### EU 特例测试

覆盖：

- EU electronics 双重 0.95 折。
- `FEATURE_FLAGS["round_eu_per_item"] = True/False` 差异。
- EU 总额 `_round2`。
- EU 积分基数使用税前 `sub_after_discounts`。

### 优惠券边界测试

覆盖：

- fixed 券 `t == threshold` 生效。
- percent 券 `t == threshold` 不生效。
- `calc_v1` percent 在 `t == threshold` 生效。
- unknown coupon 静默不生效。
- 自定义 coupon dict 行为。
- BOGO 对多行同品类最便宜单价的处理。

### 积分边界测试

覆盖：

- 用户积分少于 `use_points`。
- `use_points` 超过订单 total。
- `FEATURE_FLAGS["enable_loyalty"] = False`。
- `use_points` 为负数的现有行为。

### 多套金额口径快照测试

同一购物车分别跑：

- `OrderSystem.checkout(dry_run=True)`
- `OrderSystem.quote`
- `calc_v0`
- `calc_v1`
- `calc_v2`
- `RegionalSettlement.settle`
- `checkout_v3_experimental`
- `PriceQuoteBuilder.build`
- `SettlementReconciler.run`

锁住当前差异，不追求一致。

### 配置和全局副作用测试

覆盖：

- `ConfigManager.set_flag` 影响后续 `dispatch_checkout`。
- `ConfigManager.set_tax_rate` 对新旧 `OrderSystem` 实例的差异。
- `CouponIssuer.issue_fixed/issue_percent/revoke` 对全局券目录的影响。
- `reset_state` 清数据库和 session，但不恢复配置表。

### 售后 / 支付 / 通知测试

覆盖：

- `RefundEngine.refund` 对库存、audit、event 和返回结构的影响。
- `DisputeCenter.resolve("refund")` 对订单状态、库存和事件的影响。
- `PaymentSimulator.authorize` 成功、超限、bnpl 新客拒绝。
- `PaymentSimulator.refund_payment` 成功和无可退支付。
- `CheckoutFacade.place_order` 的双通知行为和支付失败状态。

### 报表和展示测试

覆盖：

- `ReportBuilder.gmv` 与 `gmv_from_events` 可不一致。
- `ReportBuilder.to_csv` 当前手拼格式。
- `ReportBuilder.to_json_like` 当前伪 JSON 格式。
- `format_money` 各 region 当前输出。
- `ReceiptPrinter.render` 当前小票结构。
- `render_notification` 模板缺失时的回退文案。

