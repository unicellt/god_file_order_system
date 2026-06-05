"""订单结算引擎。

把购物车算成应付金额并落库：逐行计价、品类折扣、VIP、优惠券、税、运费、积分、
库存预留、风控、持久化、通知。订单、库存、预留、审计、事件、礼品卡、支付都存 sqlite。

历史上从早期的 calc() 一路加功能演进而来，calc_v0 / calc_v1 / calc_v2 等旧入口
仍保留给老调用方。
"""

import json
import os
import sqlite3

# =============================================================================
# 全局配置
# =============================================================================

CATEGORY_RULES = {
    "fresh": {"bulk_qty": 3, "bulk_rate": 0.9, "tag": "生鲜"},
    "book": {"q1": 2, "r1": 0.95, "q2": 5, "r2": 0.8, "tag": "图书"},
    "electronics": {"bulk_qty": 2, "bulk_rate": 0.95, "tag": "电子"},
    "clothing": {"bulk_qty": 4, "bulk_rate": 0.92, "tag": "服装"},
    "grocery": {"bulk_qty": 6, "bulk_rate": 0.93, "tag": "日杂"},
    "luxury": {"bulk_qty": 999, "bulk_rate": 1.0, "tag": "奢品"},  # 奢品不打折
    "digital": {"bulk_qty": 1, "bulk_rate": 1.0, "tag": "数字"},
    "subscription": {"bulk_qty": 1, "bulk_rate": 1.0, "tag": "订阅"},
}

VIP_TIERS = {
    0: 1.0,
    1: 0.98,
    2: 0.95,
    3: 0.9,
    4: 0.88,
    5: 0.85,
}

# 优惠券目录：type 决定走哪条分支，字段不统一（历史遗留）
COUPON_CATALOG = {
    "FIX10": {"type": "fixed", "amount": 10, "threshold": 100},
    "FIX50": {"type": "fixed", "amount": 50, "threshold": 300},
    "PCT10": {"type": "percent", "rate": 0.1, "threshold": 100},
    "PCT20": {"type": "percent", "rate": 0.2, "threshold": 200},
    "PCT30": {"type": "percent", "rate": 0.3, "threshold": 500},
    "FREESHIP": {"type": "freeship", "threshold": 0},
    "FIRST15": {"type": "firstorder", "rate": 0.15, "threshold": 0},
    "BOGO": {"type": "bogo", "threshold": 0, "cat": "book"},
}

# 各区域税率
TAX_TABLE = {
    "cn": 0.0,
    "us": 0.08,
    "eu": 0.2,
    "uk": 0.2,
    "jp": 0.1,
    "au": 0.1,
    "sg": 0.07,
}

# 各区域结算币种与对人民币汇率（仅用于展示折算，结算用本位币）
CURRENCY_RATES = {
    "cn": ("CNY", 1.0),
    "us": ("USD", 0.14),
    "eu": ("EUR", 0.13),
    "uk": ("GBP", 0.11),
    "jp": ("JPY", 20.0),
    "au": ("AUD", 0.21),
    "sg": ("SGD", 0.19),
}

# 运费表：按区域 + 重量档（kg）。free_threshold 是免运费门槛。
SHIPPING_TABLE = {
    "cn": {"base": 8, "per_kg": 2, "free_threshold": 99},
    "us": {"base": 12, "per_kg": 3, "free_threshold": 150},
    "eu": {"base": 15, "per_kg": 4, "free_threshold": 200},
    "uk": {"base": 14, "per_kg": 4, "free_threshold": 200},
    "jp": {"base": 10, "per_kg": 3, "free_threshold": 120},
    "au": {"base": 18, "per_kg": 5, "free_threshold": 250},
    "sg": {"base": 9, "per_kg": 2, "free_threshold": 88},
}

# 积分规则：每消费 1 元本位币累计多少积分
LOYALTY_RATE = 1.0
LOYALTY_TIER_BONUS = {0: 1.0, 1: 1.0, 2: 1.1, 3: 1.2, 4: 1.3, 5: 1.5}

# 风控规则（确定性，不依赖随机/时间，便于复现）
RISK_RULES = {
    "amount_hard_limit": 50000,   # 超过直接拒
    "amount_soft_limit": 8000,    # 超过加分
    "item_count_limit": 30,       # 单数过多加分
    "reject_score": 80,           # 总分 >= 拒单
}

FEATURE_FLAGS = {
    "use_legacy_v1": False,        # 切到老结算路径
    "enable_loyalty": True,
    "enable_risk": True,
    "enable_notify": True,
    "round_eu_per_item": True,     # EU 逐行四舍五入
}

# 种子库存
SEED_INVENTORY = {
    "SKU-FRESH-1": 100,
    "SKU-FRESH-2": 50,
    "SKU-BOOK-1": 200,
    "SKU-BOOK-2": 30,
    "SKU-ELEC-1": 20,
    "SKU-ELEC-2": 5,
    "SKU-CLOTH-1": 80,
    "SKU-LUX-1": 3,
    "SKU-DIGI-1": 999999,
    "SKU-SUB-1": 999999,
    "SKU-GROC-1": 300,
}

# =============================================================================
# sqlite 存储层：订单 / 库存 / 预留 / 审计 / 事件 / 礼品卡 / 支付 都落库
# 下面用一组 Mapping/Sequence 风格的封装把表包起来，调用方按字典/列表语义读写。
# =============================================================================

_DEFAULT_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "order_system.db"))
_db_state = {"path": _DEFAULT_DB_PATH, "conn": None}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders       (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS inventory    (sku TEXT PRIMARY KEY, stock INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (order_id TEXT PRIMARY KEY, need TEXT);
CREATE TABLE IF NOT EXISTS audit        (id INTEGER PRIMARY KEY AUTOINCREMENT, msg TEXT);
CREATE TABLE IF NOT EXISTS events        (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT);
CREATE TABLE IF NOT EXISTS gift_cards   (code TEXT PRIMARY KEY, balance REAL);
CREATE TABLE IF NOT EXISTS payments     (order_id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS seq          (name TEXT PRIMARY KEY, val INTEGER);
"""


def configure_db(path):
    """切换数据库文件（测试用临时库时调）。"""
    if _db_state["conn"] is not None:
        _db_state["conn"].close()
        _db_state["conn"] = None
    _db_state["path"] = os.path.abspath(path)


def _conn():
    if _db_state["conn"] is None:
        c = sqlite3.connect(_db_state["path"])
        c.row_factory = sqlite3.Row
        _db_state["conn"] = c
        _ensure(c)
    return _db_state["conn"]


def _ensure(c):
    c.executescript(_SCHEMA)
    if c.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
        c.executemany("INSERT INTO inventory(sku, stock) VALUES (?, ?)",
                      list(SEED_INVENTORY.items()))
    if c.execute("SELECT COUNT(*) FROM seq WHERE name='order'").fetchone()[0] == 0:
        c.execute("INSERT INTO seq(name, val) VALUES ('order', 1000)")
    c.commit()


class _InventoryTable:
    def __getitem__(self, sku):
        row = _conn().execute("SELECT stock FROM inventory WHERE sku=?", (sku,)).fetchone()
        if row is None:
            raise KeyError(sku)
        return row["stock"]

    def __setitem__(self, sku, val):
        c = _conn()
        c.execute("INSERT OR REPLACE INTO inventory(sku, stock) VALUES (?, ?)", (sku, val))
        c.commit()

    def get(self, sku, default=None):
        row = _conn().execute("SELECT stock FROM inventory WHERE sku=?", (sku,)).fetchone()
        return row["stock"] if row else default

    def items(self):
        return [(r["sku"], r["stock"])
                for r in _conn().execute("SELECT sku, stock FROM inventory").fetchall()]

    def keys(self):
        return [r["sku"] for r in _conn().execute("SELECT sku FROM inventory").fetchall()]

    def __iter__(self):
        return iter(self.keys())

    def __contains__(self, sku):
        return _conn().execute("SELECT 1 FROM inventory WHERE sku=?", (sku,)).fetchone() is not None

    def __len__(self):
        return _conn().execute("SELECT COUNT(*) FROM inventory").fetchone()[0]


class _ReservationTable:
    def __setitem__(self, order_id, need):
        c = _conn()
        c.execute("INSERT OR REPLACE INTO reservations(order_id, need) VALUES (?, ?)",
                  (order_id, json.dumps(need)))
        c.commit()

    def pop(self, order_id, default=None):
        c = _conn()
        row = c.execute("SELECT need FROM reservations WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            return default
        c.execute("DELETE FROM reservations WHERE order_id=?", (order_id,))
        c.commit()
        return json.loads(row["need"])

    def get(self, order_id, default=None):
        row = _conn().execute("SELECT need FROM reservations WHERE order_id=?", (order_id,)).fetchone()
        return json.loads(row["need"]) if row else default

    def items(self):
        return [(r["order_id"], json.loads(r["need"]))
                for r in _conn().execute("SELECT order_id, need FROM reservations").fetchall()]

    def __contains__(self, order_id):
        return _conn().execute("SELECT 1 FROM reservations WHERE order_id=?",
                               (order_id,)).fetchone() is not None

    def __iter__(self):
        return iter([r["order_id"]
                     for r in _conn().execute("SELECT order_id FROM reservations").fetchall()])

    def __len__(self):
        return _conn().execute("SELECT COUNT(*) FROM reservations").fetchone()[0]

    def __repr__(self):
        return repr(dict(self.items()))


class _AuditTable:
    def append(self, msg):
        c = _conn()
        c.execute("INSERT INTO audit(msg) VALUES (?)", (str(msg),))
        c.commit()

    def _all(self):
        return [r["msg"] for r in _conn().execute("SELECT msg FROM audit ORDER BY id").fetchall()]

    def __getitem__(self, idx):
        return self._all()[idx]

    def __iter__(self):
        return iter(self._all())

    def __len__(self):
        return _conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0]


class _EventTable:
    def append(self, ev):
        c = _conn()
        c.execute("INSERT INTO events(data) VALUES (?)", (json.dumps(ev),))
        c.commit()

    def _all(self):
        return [json.loads(r["data"])
                for r in _conn().execute("SELECT data FROM events ORDER BY id").fetchall()]

    def __getitem__(self, idx):
        return self._all()[idx]

    def __iter__(self):
        return iter(self._all())

    def __len__(self):
        return _conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]


class _OrderTable:
    def __setitem__(self, order_id, order):
        c = _conn()
        c.execute("INSERT OR REPLACE INTO orders(id, data) VALUES (?, ?)",
                  (order_id, json.dumps(order)))
        c.commit()

    def __getitem__(self, order_id):
        row = _conn().execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return json.loads(row["data"])

    def get(self, order_id, default=None):
        row = _conn().execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        return json.loads(row["data"]) if row else default

    def values(self):
        return [json.loads(r["data"])
                for r in _conn().execute("SELECT data FROM orders").fetchall()]

    def keys(self):
        return [r["id"] for r in _conn().execute("SELECT id FROM orders").fetchall()]

    def __iter__(self):
        return iter(self.keys())

    def __contains__(self, order_id):
        return _conn().execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone() is not None

    def __len__(self):
        return _conn().execute("SELECT COUNT(*) FROM orders").fetchone()[0]


class _GiftCardTable:
    def get(self, code, default=None):
        row = _conn().execute("SELECT balance FROM gift_cards WHERE code=?", (code,)).fetchone()
        return row["balance"] if row else default

    def __getitem__(self, code):
        row = _conn().execute("SELECT balance FROM gift_cards WHERE code=?", (code,)).fetchone()
        if row is None:
            raise KeyError(code)
        return row["balance"]

    def __setitem__(self, code, balance):
        c = _conn()
        c.execute("INSERT OR REPLACE INTO gift_cards(code, balance) VALUES (?, ?)", (code, balance))
        c.commit()

    def __contains__(self, code):
        return _conn().execute("SELECT 1 FROM gift_cards WHERE code=?", (code,)).fetchone() is not None


class _PaymentTable:
    def get(self, order_id, default=None):
        row = _conn().execute("SELECT data FROM payments WHERE order_id=?", (order_id,)).fetchone()
        return json.loads(row["data"]) if row else default

    def __getitem__(self, order_id):
        row = _conn().execute("SELECT data FROM payments WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return json.loads(row["data"])

    def __setitem__(self, order_id, rec):
        c = _conn()
        c.execute("INSERT OR REPLACE INTO payments(order_id, data) VALUES (?, ?)",
                  (order_id, json.dumps(rec)))
        c.commit()

    def __contains__(self, order_id):
        return _conn().execute("SELECT 1 FROM payments WHERE order_id=?",
                               (order_id,)).fetchone() is not None


_INVENTORY = _InventoryTable()
_RESERVATIONS = _ReservationTable()
_AUDIT_LOG = _AuditTable()
_EVENTS = _EventTable()
_ORDERS = _OrderTable()
_GIFT_CARDS = _GiftCardTable()
_PAYMENTS = _PaymentTable()

# 仍留在内存里的非持久化状态（缓存 / 会话）
_CACHE = {}
_SESSION = {"current_user": None, "last_region": "cn"}


def _audit(msg):
    _AUDIT_LOG.append(str(msg))


def _emit(kind, payload):
    _EVENTS.append({"kind": kind, "payload": payload})


def _next_order_id():
    c = _conn()
    c.execute("UPDATE seq SET val = val + 1 WHERE name='order'")
    val = c.execute("SELECT val FROM seq WHERE name='order'").fetchone()[0]
    c.commit()
    return "ORD-%d" % val


def reset_state():
    """清库重建 + 重新灌种子库存，并清掉内存缓存/会话。测试/重跑前调。"""
    c = _conn()
    c.executescript("""
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS inventory;
        DROP TABLE IF EXISTS reservations;
        DROP TABLE IF EXISTS audit;
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS gift_cards;
        DROP TABLE IF EXISTS payments;
        DROP TABLE IF EXISTS seq;
    """)
    _ensure(c)
    _CACHE.clear()
    _SESSION.update({"current_user": None, "last_region": "cn"})


def _round2(x):
    return round(x + 0.0, 2)


# =============================================================================
# god class：一个类塞下整套结算引擎
# =============================================================================

class OrderSystem:
    """订单结算引擎。checkout() 把计价、折扣、券、税、运费、积分、库存、风控、持久化、通知都做了。"""

    def __init__(self, region="cn", store=None):
        self.region = region
        self.store = store if store is not None else _ORDERS
        self.tax = TAX_TABLE.get(region, 0.0)
        self._tmp = {}
        self._last_breakdown = {}
        _SESSION["last_region"] = region

    # ---- helper ----

    def price_item(self, it):
        """单行计价 + 品类折扣。"""
        p = it["price"] * it["qty"]
        c = it.get("cat")
        if c == "fresh":
            if it["qty"] >= CATEGORY_RULES["fresh"]["bulk_qty"]:
                p = p * CATEGORY_RULES["fresh"]["bulk_rate"]
        elif c == "book":
            if it["qty"] >= CATEGORY_RULES["book"]["q2"]:
                p = p * CATEGORY_RULES["book"]["r2"]
            elif it["qty"] >= CATEGORY_RULES["book"]["q1"]:
                p = p * CATEGORY_RULES["book"]["r1"]
        elif c == "electronics":
            if it["qty"] >= CATEGORY_RULES["electronics"]["bulk_qty"]:
                p = p * CATEGORY_RULES["electronics"]["bulk_rate"]
        elif c == "clothing":
            if it["qty"] >= CATEGORY_RULES["clothing"]["bulk_qty"]:
                p = p * CATEGORY_RULES["clothing"]["bulk_rate"]
        elif c == "grocery":
            if it["qty"] >= CATEGORY_RULES["grocery"]["bulk_qty"]:
                p = p * CATEGORY_RULES["grocery"]["bulk_rate"]
        # luxury / digital / subscription 不打折
        return p

    def vip_discount(self, t, user):
        """VIP 折扣。"""
        if not user.get("vip"):
            return t
        lv = user.get("vip_level", 1)
        rate = VIP_TIERS.get(lv, VIP_TIERS[5] if lv > 5 else 1.0)
        return t * rate

    def estimate_shipping(self, items, subtotal):
        """运费预估（quote 用）。免运费按传入的 subtotal 判断。"""
        cfg = SHIPPING_TABLE.get(self.region, SHIPPING_TABLE["cn"])
        w = 0.0
        for it in items:
            w += it.get("weight", 0.5) * it["qty"]
        if subtotal >= cfg["free_threshold"]:
            return 0.0
        return cfg["base"] + cfg["per_kg"] * w

    def quote(self, items, user, coupon=None):
        """报价：给前端展示用的预估，不落库、不扣库存、不发通知。
        """
        sub = 0.0
        for it in items:
            sub += self.price_item(it)
        sub = self.vip_discount(sub, user)
        ship = self.estimate_shipping(items, sub)   # 注意：用券前 sub
        tax = sub * self.tax
        total = sub + tax + ship
        return _round2(total)

    # ---- 风控（确定性打分） ----

    def risk_score(self, items, user, amount):
        score = 0
        if user.get("blacklist"):
            score += 100
        if amount > RISK_RULES["amount_hard_limit"]:
            score += 100
        elif amount > RISK_RULES["amount_soft_limit"]:
            score += 40
        cnt = sum(i["qty"] for i in items)
        if cnt > RISK_RULES["item_count_limit"]:
            score += 30
        if user.get("new") and amount > 3000:
            score += 25
        # 奢品大额加分
        for it in items:
            if it.get("cat") == "luxury" and it["price"] * it["qty"] > 5000:
                score += 20
                break
        return score

    # ---- 库存（就地扣减，副作用） ----

    def reserve(self, order_id, items):
        """预留库存：就地扣减 _INVENTORY，并记到 _RESERVATIONS。库存不足返回 False。"""
        need = {}
        for it in items:
            sku = it.get("sku")
            if sku is None:
                continue
            need[sku] = need.get(sku, 0) + it["qty"]
        # 先检查
        for sku, q in need.items():
            if _INVENTORY.get(sku, 0) < q:
                _audit("reserve failed: %s need %d have %d" % (sku, q, _INVENTORY.get(sku, 0)))
                return False
        # 再扣减
        for sku, q in need.items():
            _INVENTORY[sku] -= q
        _RESERVATIONS[order_id] = need
        _audit("reserved %s for %s" % (need, order_id))
        return True

    def release(self, order_id):
        """释放预留：把库存加回去。"""
        need = _RESERVATIONS.pop(order_id, None)
        if not need:
            return
        for sku, q in need.items():
            _INVENTORY[sku] = _INVENTORY.get(sku, 0) + q
        _audit("released reservation for %s" % order_id)

    # ---- 积分 ----

    def earn_points(self, base_amount, user):
        lv = user.get("vip_level", 0) if user.get("vip") else 0
        bonus = LOYALTY_TIER_BONUS.get(lv, 1.0)
        return int(base_amount * LOYALTY_RATE * bonus)

    def burn_points(self, user, want):
        have = user.get("loyalty_points", 0)
        used = min(have, want)
        return used

    # ---- 持久化（假 DB：就是写进 self.store dict） ----

    def save_order(self, order):
        self.store[order["id"]] = order
        _audit("saved order %s total=%s" % (order["id"], order["total"]))
        return order["id"]

    def load_order(self, order_id):
        return self.store.get(order_id)

    # ---- 通知 ----

    def notify(self, user, order):
        if not FEATURE_FLAGS["enable_notify"]:
            return
        msg = "订单 %s 已确认，应付 %.2f" % (order["id"], order["total"])
        _audit("notify %s: %s" % (user.get("id"), msg))
        _emit("order_confirmed", {"order_id": order["id"], "total": order["total"]})

    # =========================================================================
    # checkout
    # =========================================================================

    def checkout(self, items, user, coupon=None, use_points=0, dry_run=False):
        """下单结算。返回订单 dict（含 total / breakdown / status）。

        步骤：
          计价 → 品类折扣 → VIP → 券 → 负数兜底 → 税 → 运费 → 积分 → 风控 → 库存 → 落库 → 通知
        """
        _SESSION["current_user"] = user.get("id")
        oid = _next_order_id()
        self._tmp = {}
        bd = {}   # breakdown 明细

        # ---------------- 1. 逐行计价 + 品类折扣 ----------------
        t = 0.0
        line_items = []
        for i in items:
            p = i["price"] * i["qty"]
            c = i.get("cat")
            if c == "fresh":
                if i["qty"] >= 3:
                    p = p * 0.9
            elif c == "book":
                if i["qty"] >= 5:
                    p = p * 0.8
                elif i["qty"] >= 2:
                    p = p * 0.95
            elif c == "electronics":
                if i["qty"] >= 2:
                    p = p * 0.95
                    # eu 区清仓追加折扣（2019Q4 大促临时加的，活动早结束了）
                    if self.region == "eu":
                        p = p * 0.95
            elif c == "clothing":
                if i["qty"] >= 4:
                    p = p * 0.92
            elif c == "grocery":
                if i["qty"] >= 6:
                    p = p * 0.93
            elif c == "luxury":
                pass
            elif c == "digital":
                pass
            elif c == "subscription":
                pass
            else:
                pass
            # eu 历史上要求每行金额都展示到分，这里逐行 round 一下
            if self.region == "eu" and FEATURE_FLAGS["round_eu_per_item"]:
                p = _round2(p)
            t = t + p
            line_items.append({"sku": i.get("sku"), "cat": c, "line": p, "qty": i["qty"]})
        bd["subtotal_after_cat"] = t
        sub_before_discounts = t

        # ---------------- 2. VIP 折扣 ----------------
        if user.get("vip"):
            lv = user.get("vip_level", 1)
            if lv == 1:
                t = t * 0.98
            elif lv == 2:
                t = t * 0.95
            elif lv == 3:
                t = t * 0.9
            elif lv == 4:
                t = t * 0.88
            elif lv >= 5:
                t = t * 0.85
        bd["after_vip"] = t

        # ---------------- 3. 优惠券 ----------------
        coup = None
        if coupon:
            if isinstance(coupon, str):
                coup = COUPON_CATALOG.get(coupon)
            else:
                coup = coupon
        if coup:
            ct = coup.get("type")
            if ct == "fixed":
                # 固定立减：阈值用 >=
                if t >= coup.get("threshold", 0):
                    t = t - coup["amount"]
            elif ct == "percent":
                # 在 VIP 折后的 t 上按比例打折
                if t > coup.get("threshold", 0):
                    t = t * (1 - coup["rate"])
            elif ct == "firstorder":
                if user.get("new"):
                    t = t * (1 - coup["rate"])
            elif ct == "freeship":
                self._tmp["force_freeship"] = True
            elif ct == "bogo":
                # 买一赠一：同品类最便宜一件免费
                cat = coup.get("cat")
                cands = [li for li in line_items if li["cat"] == cat]
                if cands:
                    cheapest = min(cands, key=lambda x: x["line"] / max(x["qty"], 1))
                    t = t - (cheapest["line"] / max(cheapest["qty"], 1))
            else:
                pass
        bd["after_coupon"] = t

        # ---------------- 4. 负数兜底 ----------------
        if t < 0:
            t = 0.0
        sub_after_discounts = t

        # ---------------- 5. 税 ----------------
        tax_amt = t * self.tax
        t = t + tax_amt
        bd["tax"] = tax_amt
        bd["after_tax"] = t

        # ---------------- 6. 运费 ----------------
        cfg = SHIPPING_TABLE.get(self.region, SHIPPING_TABLE["cn"])
        weight = 0.0
        for it in items:
            weight += it.get("weight", 0.5) * it["qty"]
        ship = 0.0
        if self._tmp.get("force_freeship"):
            ship = 0.0
        else:
            # 免运费门槛：用当前的 t 判断
            if t >= cfg["free_threshold"]:
                ship = 0.0
            else:
                ship = cfg["base"] + cfg["per_kg"] * weight
        t = t + ship
        bd["shipping"] = ship

        # ---------------- 7. EU 总额取整 ----------------
        if self.region == "eu":
            t = _round2(t)

        # ---------------- 8. 用积分抵扣 ----------------
        used_pts = 0
        if use_points and FEATURE_FLAGS["enable_loyalty"]:
            used_pts = self.burn_points(user, use_points)
            # 100 积分 = 1 元
            t = t - used_pts / 100.0
            if t < 0:
                t = 0.0
        bd["points_used"] = used_pts

        # ---------------- 9. 赚取积分 ----------------
        earned = 0
        if FEATURE_FLAGS["enable_loyalty"]:
            # 积分基数：eu/uk 用税前，其它区域用税后
            if self.region in ("eu", "uk"):
                base_for_pts = sub_after_discounts
            else:
                base_for_pts = bd["after_tax"]
            earned = self.earn_points(base_for_pts, user)
        bd["points_earned"] = earned

        total = _round2(t)

        # ---------------- 10. 风控 ----------------
        status = "confirmed"
        risk = 0
        if FEATURE_FLAGS["enable_risk"]:
            risk = self.risk_score(items, user, total)
        bd["risk_score"] = risk

        # ---------------- 11. 库存预留（注意顺序：先留再判风控） ----------------
        reserved = False
        if not dry_run:
            reserved = self.reserve(oid, items)
            if not reserved:
                status = "out_of_stock"

        # 风控拒单：标记状态并直接返回
        if FEATURE_FLAGS["enable_risk"] and risk >= RISK_RULES["reject_score"]:
            status = "rejected"
            order = {
                "id": oid, "user": user.get("id"), "region": self.region,
                "items": line_items, "total": total, "breakdown": bd,
                "status": status, "points_earned": 0, "points_used": used_pts,
            }
            self._last_breakdown = bd
            _emit("order_rejected", {"order_id": oid, "risk": risk})
            if not dry_run:
                self.save_order(order)
            return order

        order = {
            "id": oid, "user": user.get("id"), "region": self.region,
            "items": line_items, "total": total, "breakdown": bd,
            "status": status, "points_earned": earned, "points_used": used_pts,
            "currency": CURRENCY_RATES.get(self.region, ("CNY", 1.0))[0],
        }
        self._last_breakdown = bd

        if not dry_run and status == "confirmed":
            # 真正扣积分 / 加积分（写回 user）
            if FEATURE_FLAGS["enable_loyalty"]:
                user["loyalty_points"] = user.get("loyalty_points", 0) - used_pts + earned
            self.save_order(order)
            self.notify(user, order)
        return order

    # ---- 报表 ----

    def report_last(self):
        bd = self._last_breakdown
        if not bd:
            return "no order yet"
        lines = ["=== 上一单明细 ==="]
        for k in ("subtotal_after_cat", "after_vip", "after_coupon",
                  "tax", "shipping", "after_tax", "points_earned", "risk_score"):
            if k in bd:
                lines.append("%-18s : %s" % (k, bd[k]))
        return "\n".join(lines)


# =============================================================================
# 模块级"legacy v1"路径：老版本结算函数，feature flag 控制，和类里的新逻辑并存
# （典型遗留：新老两套都还在跑，谁也不敢删）
# =============================================================================

def calc_v1(items, user, coupon=None, region="cn"):
    """老版结算（v1）。

    
    
    """
    t = 0
    for i in items:
        p = i["price"] * i["qty"]
        if i.get("cat") == "fresh" and i["qty"] >= 3:
            p = p * 0.9
        elif i.get("cat") == "book":
            if i["qty"] >= 5:
                p = p * 0.8
            elif i["qty"] >= 2:
                p = p * 0.95
        elif i.get("cat") == "electronics" and i["qty"] >= 2:
            p = p * 0.95
        t = t + p
    if user.get("vip"):
        lv = user.get("vip_level", 1)
        if lv == 1:
            t = t * 0.98
        elif lv == 2:
            t = t * 0.95
        elif lv >= 3:
            t = t * 0.9
    if coupon:
        c = COUPON_CATALOG.get(coupon) if isinstance(coupon, str) else coupon
        if c and c["type"] == "fixed":
            if t >= c.get("threshold", 0):
                t = t - c["amount"]
        elif c and c["type"] == "percent":
            if t >= c.get("threshold", 0):
                t = t * (1 - c["rate"])
    if region == "cn":
        t = t * 1.0
    elif region == "us":
        t = t * 1.08
    elif region == "eu":
        t = t * 1.2
    if t < 0:
        t = 0
    return round(t, 2)


def dispatch_checkout(items, user, coupon=None, region="cn", **kw):
    """对外的统一入口：按 feature flag 决定走 v1 还是新引擎。

    很多调用方其实只 import 这个函数，不知道底下有两套实现。
    """
    if FEATURE_FLAGS["use_legacy_v1"]:
        return {"total": calc_v1(items, user, coupon, region), "engine": "v1"}
    sys = OrderSystem(region=region)
    return sys.checkout(items, user, coupon=coupon, **kw)


# =============================================================================
# 更多配置
# =============================================================================

# 国家码 → 区域。很多调用方传国家码，引擎内部却按 region 工作，靠这张表硬映射。
COUNTRY_TO_REGION = {
    "CN": "cn", "HK": "cn", "MO": "cn", "TW": "cn",
    "US": "us", "CA": "us",          # 注意：加拿大被粗暴归到 us 区（历史遗留）
    "DE": "eu", "FR": "eu", "IT": "eu", "ES": "eu", "NL": "eu",
    "GB": "uk", "IE": "uk",
    "JP": "jp",
    "AU": "au", "NZ": "au",
    "SG": "sg", "MY": "sg",
}

# 部分品类在部分区域免税（口径分散，checkout 的税步骤并没有读这张表！）
TAX_EXEMPT_CATEGORIES = {
    "cn": set(),
    "us": {"grocery", "book"},
    "eu": {"book"},
    "uk": {"book", "fresh"},
    "jp": set(),
    "au": {"fresh", "grocery"},
    "sg": set(),
}

# 促销叠加矩阵：哪些券能和 VIP 叠加。
PROMO_STACK_MATRIX = {
    "fixed": {"vip": True, "points": True},
    "percent": {"vip": True, "points": True},
    "firstorder": {"vip": False, "points": True},
    "freeship": {"vip": True, "points": True},
    "bogo": {"vip": False, "points": False},
}

# 运费分区（按区域 + 远近）
SHIPPING_ZONES = {
    "cn": {"local": 6, "remote": 18, "remote_provinces": {"XJ", "XZ", "QH", "NM"}},
    "us": {"local": 10, "remote": 30, "remote_provinces": {"AK", "HI"}},
    "eu": {"local": 12, "remote": 28, "remote_provinces": set()},
    "uk": {"local": 11, "remote": 26, "remote_provinces": set()},
    "jp": {"local": 8, "remote": 22, "remote_provinces": {"OKINAWA"}},
    "au": {"local": 15, "remote": 40, "remote_provinces": {"NT", "TAS"}},
    "sg": {"local": 7, "remote": 7, "remote_provinces": set()},
}

# 各 locale 金额格式
LOCALE_FORMATS = {
    "cn": {"symbol": "¥", "sep": ",", "decimals": 2, "symbol_first": True},
    "us": {"symbol": "$", "sep": ",", "decimals": 2, "symbol_first": True},
    "eu": {"symbol": "€", "sep": ".", "decimals": 2, "symbol_first": False},
    "uk": {"symbol": "£", "sep": ",", "decimals": 2, "symbol_first": True},
    "jp": {"symbol": "¥", "sep": ",", "decimals": 0, "symbol_first": True},
    "au": {"symbol": "A$", "sep": ",", "decimals": 2, "symbol_first": True},
    "sg": {"symbol": "S$", "sep": ",", "decimals": 2, "symbol_first": True},
}

# 退货规则：多少天内可退，哪些品类不可退
RETURN_RULES = {
    "window_days": 7,
    "non_returnable": {"digital", "subscription", "fresh"},
    "restock_fee_rate": {"electronics": 0.1, "luxury": 0.15},
}

# 积分兑换目录（LoyaltyManager 用）
REDEEM_CATALOG = {
    "R-5OFF": {"cost": 500, "kind": "cash", "value": 5},
    "R-FREESHIP": {"cost": 300, "kind": "freeship", "value": 0},
    "R-VIP1M": {"cost": 2000, "kind": "vip_upgrade", "value": 1},
}


def country_to_region(country, default="cn"):
    """把国家码翻译成区域。传错就回默认区——静默兜底，是隐藏 bug 的好地方。"""
    if country is None:
        return default
    return COUNTRY_TO_REGION.get(country.upper(), default)


def format_money(amount, region="cn"):
    """按 locale 格式化金额（字符串）。报表/通知到处用，口径却各写各的。"""
    fmt = LOCALE_FORMATS.get(region, LOCALE_FORMATS["cn"])
    dec = fmt["decimals"]
    if dec == 0:
        body = "%d" % int(round(amount))
    else:
        body = ("%." + str(dec) + "f") % amount
    # 千分位（很糙，只处理整数部分）
    if "." in body:
        ip, fp = body.split(".")
    else:
        ip, fp = body, ""
    neg = ip.startswith("-")
    if neg:
        ip = ip[1:]
    grouped = ""
    while len(ip) > 3:
        grouped = fmt["sep"] + ip[-3:] + grouped
        ip = ip[:-3]
    grouped = ip + grouped
    if fp:
        grouped = grouped + "." + fp
    if neg:
        grouped = "-" + grouped
    if fmt["symbol_first"]:
        return fmt["symbol"] + grouped
    return grouped + " " + fmt["symbol"]


# =============================================================================
# 购物车 / 用户 / 券 校验：一堆 verbose 的防御式检查，散落且部分和 checkout 重复
# =============================================================================

class CartValidator:
    """下单前校验。checkout 自己其实没认真校验，很多脏数据是这里挡的——
    但 dispatch_checkout 又没有强制先过 validator，所以脏数据有时还是漏进去了。
    """

    def __init__(self, region="cn"):
        self.region = region
        self.errors = []
        self.warnings = []

    def _err(self, msg):
        self.errors.append(msg)

    def _warn(self, msg):
        self.warnings.append(msg)

    def validate_items(self, items):
        if not items:
            self._err("购物车为空")
            return
        seen = {}
        for idx, it in enumerate(items):
            if "price" not in it:
                self._err("第%d行缺少 price" % idx)
                continue
            if "qty" not in it:
                self._err("第%d行缺少 qty" % idx)
                continue
            if it["price"] < 0:
                self._err("第%d行价格为负: %s" % (idx, it["price"]))
            if it["qty"] <= 0:
                self._err("第%d行数量非正: %s" % (idx, it["qty"]))
            cat = it.get("cat")
            if cat is not None and cat not in CATEGORY_RULES:
                self._warn("第%d行未知品类: %s（将按不打折处理）" % (idx, cat))
            sku = it.get("sku")
            if sku is not None:
                seen[sku] = seen.get(sku, 0) + 1
                if sku not in _INVENTORY:
                    self._warn("第%d行 sku 不在库存表: %s" % (idx, sku))
            if it.get("weight", 0.5) > 100:
                self._warn("第%d行重量异常: %s kg" % (idx, it.get("weight")))
        for sku, n in seen.items():
            if n > 1:
                self._warn("sku %s 在购物车里出现 %d 次（未合并行，可能重复计费）" % (sku, n))

    def validate_user(self, user):
        if not user:
            self._err("缺少用户")
            return
        if user.get("vip"):
            lv = user.get("vip_level", 1)
            if lv < 0 or lv > 5:
                self._warn("VIP 等级越界: %s（将被 clamp）" % lv)
        pts = user.get("loyalty_points", 0)
        if pts < 0:
            self._err("用户积分为负: %s" % pts)
        if user.get("blacklist") and user.get("vip"):
            self._warn("黑名单用户同时是 VIP（矛盾状态，风控会拒）")

    def validate_coupon(self, coupon, user):
        if coupon is None:
            return
        c = COUPON_CATALOG.get(coupon) if isinstance(coupon, str) else coupon
        if c is None:
            self._err("无效券码: %s" % coupon)
            return
        ct = c.get("type")
        if ct not in ("fixed", "percent", "firstorder", "freeship", "bogo"):
            self._err("未知券类型: %s" % ct)
        if ct == "firstorder" and not user.get("new"):
            self._warn("firstorder 券但用户非新客（券不会生效）")
        if ct == "fixed" and "amount" not in c:
            self._err("fixed 券缺少 amount")
        if ct == "percent":
            if "rate" not in c:
                self._err("percent 券缺少 rate")
            elif not (0 < c["rate"] < 1):
                self._warn("percent 券 rate 越界: %s" % c["rate"])

    def validate(self, items, user, coupon=None):
        self.errors = []
        self.warnings = []
        self.validate_items(items)
        self.validate_user(user)
        self.validate_coupon(coupon, user)
        return {"ok": not self.errors, "errors": list(self.errors),
                "warnings": list(self.warnings)}


# =============================================================================
# 退款 / 退货引擎
# =============================================================================

class RefundEngine:
    """退款引擎。退款金额按"原单实付里这些行占的比例"反算，
    
    """

    def __init__(self, store, region="cn"):
        self.store = store
        self.region = region

    def _reprice_line(self, li_like):
        """退款时重新给一行计价。注意它用的折扣阈值和 checkout 不完全一样（少了 electronics 的 eu 双折）。"""
        price = li_like["price"]
        qty = li_like["qty"]
        cat = li_like.get("cat")
        p = price * qty
        if cat == "fresh" and qty >= 3:
            p = p * 0.9
        elif cat == "book":
            if qty >= 5:
                p = p * 0.8
            elif qty >= 2:
                p = p * 0.95
        elif cat == "electronics" and qty >= 2:
            p = p * 0.95
        elif cat == "clothing" and qty >= 4:
            p = p * 0.92
        elif cat == "grocery" and qty >= 6:
            p = p * 0.93
        return p

    def refund(self, order_id, return_items):
        """按行退款。return_items: [{"sku","price","qty","cat"}]。"""
        order = self.store.get(order_id)
        if not order:
            return {"ok": False, "reason": "订单不存在"}
        if order.get("status") != "confirmed":
            return {"ok": False, "reason": "订单状态不可退: %s" % order.get("status")}
        refundable = 0.0
        fee = 0.0
        rejected = []
        for ri in return_items:
            cat = ri.get("cat")
            if cat in RETURN_RULES["non_returnable"]:
                rejected.append({"sku": ri.get("sku"), "reason": "不可退品类 %s" % cat})
                continue
            line_val = self._reprice_line(ri)
            rate = RETURN_RULES["restock_fee_rate"].get(cat, 0.0)
            f = line_val * rate
            fee += f
            refundable += (line_val - f)
            # 退货补库存（副作用：就地改 _INVENTORY）
            sku = ri.get("sku")
            if sku is not None:
                _INVENTORY[sku] = _INVENTORY.get(sku, 0) + ri["qty"]
        refundable = _round2(refundable)
        fee = _round2(fee)
        _audit("refund order=%s amount=%s fee=%s" % (order_id, refundable, fee))
        _emit("order_refunded", {"order_id": order_id, "amount": refundable})
        return {"ok": True, "order_id": order_id, "refund": refundable,
                "restock_fee": fee, "rejected": rejected}


# =============================================================================
# 库存管理后台：补货、调整、低库存预警、对账（都直接改 _INVENTORY 全局）
# =============================================================================

class InventoryAdmin:
    def restock(self, sku, qty):
        _INVENTORY[sku] = _INVENTORY.get(sku, 0) + qty
        _audit("restock %s += %d -> %d" % (sku, qty, _INVENTORY[sku]))
        return _INVENTORY[sku]

    def adjust(self, sku, new_qty):
        old = _INVENTORY.get(sku, 0)
        _INVENTORY[sku] = new_qty
        _audit("adjust %s %d -> %d" % (sku, old, new_qty))
        return new_qty

    def low_stock(self, threshold=10):
        out = []
        for sku, q in _INVENTORY.items():
            if q < threshold:
                out.append((sku, q))
        out.sort(key=lambda x: x[1])
        return out

    def reconcile(self, store=None):
        """对账。两层：
          1) 守恒：当前库存 + 所有未释放预留，应当 == 种子库存。
          2) 孤儿预留：若传入 store，找出订单状态已不是 confirmed、却仍占着预留的库存。
        """
        rebuilt = dict(_INVENTORY)
        for oid, need in _RESERVATIONS.items():
            for sku, q in need.items():
                rebuilt[sku] = rebuilt.get(sku, 0) + q
        diffs = {}
        for sku, seed in SEED_INVENTORY.items():
            cur = rebuilt.get(sku, 0)
            if cur != seed:
                diffs[sku] = {"seed": seed, "rebuilt": cur, "delta": cur - seed}
        orphan = {}
        if store is not None:
            for oid, need in _RESERVATIONS.items():
                o = store.get(oid)
                if o is None or o.get("status") != "confirmed":
                    orphan[oid] = {"status": (o or {}).get("status"), "held": dict(need)}
        return {"balanced": not diffs, "diffs": diffs,
                "orphan_reservations": orphan,
                "leak": bool(orphan)}


# =============================================================================
# 更老的结算变体：calc_v0（最早版）、calc_v2（中间版）。三套并存，谁都不敢删。
# =============================================================================

def calc_v0(items, user, region="cn"):
    """最早的结算（v0）：连券都不支持，只有品类折扣 + 粗暴税。许多老报表还在调它对数。"""
    total = 0
    for it in items:
        sub = it["price"] * it["qty"]
        if it.get("cat") == "fresh" and it["qty"] >= 3:
            sub = sub * 0.9
        elif it.get("cat") == "book" and it["qty"] >= 2:
            sub = sub * 0.95     # v0 book 只有一档 0.95，没有 >=5 的 0.8
        total += sub
    if user.get("vip"):
        total = total * 0.95     # v0 VIP 一刀切 0.95，不分等级
    if region == "us":
        total = total * 1.08
    elif region == "eu":
        total = total * 1.2
    return round(total, 2)


def calc_v2(items, user, coupon=None, region="cn", use_points=0):
    """中间版（v2）：加了券和积分，但运费/风控/库存都没有。
    和 v1 的区别是 v2 的 percent 券阈值用 >，和现在的 checkout 对齐；但 VIP 仍只有三档。
    """
    t = 0.0
    for i in items:
        p = i["price"] * i["qty"]
        c = i.get("cat")
        if c == "fresh" and i["qty"] >= 3:
            p = p * 0.9
        elif c == "book":
            if i["qty"] >= 5:
                p = p * 0.8
            elif i["qty"] >= 2:
                p = p * 0.95
        elif c == "electronics" and i["qty"] >= 2:
            p = p * 0.95
        elif c == "clothing" and i["qty"] >= 4:
            p = p * 0.92
        t += p
    if user.get("vip"):
        lv = user.get("vip_level", 1)
        if lv == 1:
            t = t * 0.98
        elif lv == 2:
            t = t * 0.95
        elif lv >= 3:
            t = t * 0.9
    if coupon:
        c = COUPON_CATALOG.get(coupon) if isinstance(coupon, str) else coupon
        if c and c["type"] == "fixed" and t >= c.get("threshold", 0):
            t = t - c["amount"]
        elif c and c["type"] == "percent" and t > c.get("threshold", 0):
            t = t * (1 - c["rate"])
    if use_points:
        t = t - min(use_points, user.get("loyalty_points", 0)) / 100.0
    t = t * (1 + TAX_TABLE.get(region, 0.0))
    if t < 0:
        t = 0.0
    return round(t, 2)


# =============================================================================
# 报表 / 导出：直接读全局 _EVENTS / store / _AUDIT_LOG，口径又各写一套
# =============================================================================

class ReportBuilder:
    """报表中心。
    
    
    """

    def __init__(self, store, region="cn"):
        self.store = store
        self.region = region

    def confirmed_orders(self):
        return [o for o in self.store.values() if o.get("status") == "confirmed"]

    def gmv(self):
        """GMV：把所有 confirmed 订单的 total 加起来。"""
        return _round2(sum(o["total"] for o in self.confirmed_orders()))

    def gmv_from_events(self):
        """从事件流算 GMV：order_confirmed 累加。
        """
        total = 0.0
        for ev in _EVENTS:
            if ev["kind"] == "order_confirmed":
                total += ev["payload"]["total"]
        return _round2(total)

    def by_region(self):
        agg = {}
        for o in self.confirmed_orders():
            r = o.get("region", "cn")
            agg.setdefault(r, {"count": 0, "amount": 0.0})
            agg[r]["count"] += 1
            agg[r]["amount"] += o["total"]
        for r in agg:
            agg[r]["amount"] = _round2(agg[r]["amount"])
        return agg

    def by_category(self):
        """按品类聚合销售额（用 line，已含品类折扣）。
        """
        agg = {}
        for o in self.confirmed_orders():
            for li in o.get("items", []):
                cat = li.get("cat") or "unknown"
                agg.setdefault(cat, {"count": 0, "amount": 0.0})
                agg[cat]["count"] += li.get("qty", 0)
                agg[cat]["amount"] += li.get("line", 0.0)
        for c in agg:
            agg[c]["amount"] = _round2(agg[c]["amount"])
        return agg

    def points_summary(self):
        earned = sum(o.get("points_earned", 0) for o in self.confirmed_orders())
        used = sum(o.get("points_used", 0) for o in self.confirmed_orders())
        return {"earned": earned, "used": used, "net": earned - used}

    def status_breakdown(self):
        agg = {}
        for o in self.store.values():
            st = o.get("status", "unknown")
            agg[st] = agg.get(st, 0) + 1
        return agg

    def to_csv(self):
        """导出订单明细 CSV（手拼字符串，没转义，遇到逗号就崩——经典遗留导出）。"""
        rows = ["order_id,user,region,status,total,points_earned,points_used"]
        for o in self.store.values():
            rows.append("%s,%s,%s,%s,%.2f,%d,%d" % (
                o["id"], o.get("user"), o.get("region"), o.get("status"),
                o.get("total", 0.0), o.get("points_earned", 0), o.get("points_used", 0)))
        return "\n".join(rows)

    def to_json_like(self):
        """伪 JSON（手拼，仅供老系统对接，别用它做正经序列化）。"""
        parts = []
        for o in self.store.values():
            parts.append('{"id":"%s","total":%.2f,"status":"%s"}' % (
                o["id"], o.get("total", 0.0), o.get("status")))
        return "[" + ",".join(parts) + "]"

    def audit_tail(self, n=10):
        return _AUDIT_LOG[-n:]

    def reconcile_gmv(self):
        """对比 gmv() 与 gmv_from_events() 的差额。"""
        a = self.gmv()
        b = self.gmv_from_events()
        return {"gmv_store": a, "gmv_events": b, "delta": _round2(a - b)}

    def daily_text_report(self):
        lines = ["==== 日报（%s 区）====" % self.region]
        lines.append("订单状态: %s" % self.status_breakdown())
        lines.append("GMV(store): %s" % format_money(self.gmv(), self.region))
        lines.append("GMV(events): %s" % format_money(self.gmv_from_events(), self.region))
        ps = self.points_summary()
        lines.append("积分: 发放 %d / 消耗 %d / 净 %d" % (ps["earned"], ps["used"], ps["net"]))
        lines.append("分区: %s" % self.by_region())
        lines.append("分品类: %s" % self.by_category())
        return "\n".join(lines)


# =============================================================================
# 积分管理：等级、过期、兑换、历史。
# =============================================================================

class LoyaltyManager:
    """积分管理。earn/redeem 会更新传入的 user。
    
    
    """

    def __init__(self):
        self.history = []     # 每次变动一条记录

    def _record(self, user_id, delta, reason):
        self.history.append({"user": user_id, "delta": delta, "reason": reason})

    def tier_of(self, user):
        pts = user.get("loyalty_points", 0)
        if pts >= 10000:
            return 5
        if pts >= 5000:
            return 4
        if pts >= 2000:
            return 3
        if pts >= 800:
            return 2
        if pts >= 200:
            return 1
        return 0

    def earn(self, user, amount):
        """按成交额发积分（按 total）。"""
        tier = self.tier_of(user)
        bonus = LOYALTY_TIER_BONUS.get(tier, 1.0)
        pts = int(amount * LOYALTY_RATE * bonus)
        user["loyalty_points"] = user.get("loyalty_points", 0) + pts
        self._record(user.get("id"), pts, "earn")
        return pts

    def redeem(self, user, code):
        item = REDEEM_CATALOG.get(code)
        if not item:
            return {"ok": False, "reason": "无效兑换码"}
        have = user.get("loyalty_points", 0)
        if have < item["cost"]:
            return {"ok": False, "reason": "积分不足: 需要 %d 现有 %d" % (item["cost"], have)}
        user["loyalty_points"] = have - item["cost"]
        self._record(user.get("id"), -item["cost"], "redeem:" + code)
        if item["kind"] == "vip_upgrade":
            user["vip"] = True
            user["vip_level"] = min(5, user.get("vip_level", 0) + item["value"])
        return {"ok": True, "kind": item["kind"], "value": item["value"],
                "remaining": user["loyalty_points"]}

    def expire(self, user, expire_pts):
        """积分过期：直接扣。没有按 FIFO/批次，一律按总额扣——遗留实现。"""
        have = user.get("loyalty_points", 0)
        gone = min(have, expire_pts)
        user["loyalty_points"] = have - gone
        self._record(user.get("id"), -gone, "expire")
        return gone

    def user_history(self, user_id):
        return [h for h in self.history if h["user"] == user_id]


# =============================================================================
# 批量结算 / 迁移工具：把上面这些零件用最暴力的方式拼起来，复制了一堆 checkout 的判断
# =============================================================================

class BatchProcessor:
    """批量下单。它没复用 OrderSystem 的实例方法编排，而是自己又写了一遍'校验→下单→统计'，
    于是和单笔 checkout 的行为在边界上会有细微差别（比如它跳过了 dry_run 的某些分支）。
    """

    def __init__(self, region="cn", store=None):
        self.region = region
        self.store = store if store is not None else _ORDERS

    def run(self, carts):
        """carts: [{"items","user","coupon","use_points"}]，返回每单结果 + 汇总。"""
        results = []
        ok = 0
        rejected = 0
        oos = 0
        validator = CartValidator(self.region)
        engine = OrderSystem(region=self.region, store=self.store)
        for c in carts:
            v = validator.validate(c.get("items"), c.get("user"), c.get("coupon"))
            if not v["ok"]:
                results.append({"status": "invalid", "errors": v["errors"]})
                continue
            o = engine.checkout(c.get("items"), c.get("user"),
                                coupon=c.get("coupon"), use_points=c.get("use_points", 0))
            results.append(o)
            if o["status"] == "confirmed":
                ok += 1
            elif o["status"] == "rejected":
                rejected += 1
            elif o["status"] == "out_of_stock":
                oos += 1
        summary = {"total": len(carts), "confirmed": ok, "rejected": rejected,
                   "out_of_stock": oos}
        return {"results": results, "summary": summary}


def migrate_v1_orders_to_v2(orders):
    """把一批 v1 老订单的金额用 v2 口径重算（数据迁移脚本残留在主模块里）。
    这种'迁移脚本和业务代码混在一个文件'也是遗留常态。
    """
    out = []
    for o in orders:
        items = o.get("items", [])
        user = {"id": o.get("user"), "vip": o.get("vip", False),
                "vip_level": o.get("vip_level", 1),
                "loyalty_points": o.get("loyalty_points", 0)}
        new_total = calc_v2(items, user, o.get("coupon"), o.get("region", "cn"))
        out.append({"id": o.get("id"), "old_total": o.get("total"),
                    "new_total": new_total, "delta": _round2(new_total - o.get("total", 0))})
    return out


# =============================================================================
# 通知模板：多语言文案
# =============================================================================

NOTIFY_TEMPLATES = {
    "cn": {
        "confirmed": "您的订单 {id} 已确认，应付 {money}，预计 3-5 天送达。",
        "rejected": "很抱歉，订单 {id} 未通过风控审核，款项未扣。",
        "refunded": "订单 {id} 的退款 {money} 已原路退回。",
        "out_of_stock": "订单 {id} 中部分商品库存不足，请调整后重试。",
    },
    "us": {
        "confirmed": "Order {id} confirmed. Total due {money}. Ships in 3-5 days.",
        "rejected": "Sorry, order {id} did not pass risk review. You were not charged.",
        "refunded": "Refund of {money} for order {id} is on its way.",
        "out_of_stock": "Some items in order {id} are out of stock.",
    },
    "eu": {
        "confirmed": "Bestellung {id} bestätigt. Zu zahlen {money}.",
        "rejected": "Bestellung {id} wurde abgelehnt.",
        "refunded": "Rückerstattung {money} für Bestellung {id}.",
        "out_of_stock": "Einige Artikel in Bestellung {id} sind nicht vorrätig.",
    },
}


def render_notification(order, region="cn"):
    """渲染通知文案。缺语言回退到 cn。"""
    pack = NOTIFY_TEMPLATES.get(region, NOTIFY_TEMPLATES["cn"])
    tpl = pack.get(order.get("status"))
    if not tpl:
        return "订单 %s 状态更新：%s" % (order.get("id"), order.get("status"))
    money = format_money(order.get("total", 0.0), region)
    return tpl.format(id=order.get("id"), money=money)


# =============================================================================
# 一堆零散工具函数 + 一段被注释掉的"实验性优化"（死代码，遗留现场必有）
# =============================================================================

def cart_weight(items):
    return sum(it.get("weight", 0.5) * it["qty"] for it in items)


def cart_item_count(items):
    return sum(it["qty"] for it in items)


def distinct_categories(items):
    return sorted(set(it.get("cat") for it in items if it.get("cat")))


def estimate_delivery_days(region, weight):
    base = {"cn": 3, "us": 5, "eu": 6, "uk": 6, "jp": 4, "au": 7, "sg": 3}.get(region, 7)
    if weight > 20:
        base += 2
    elif weight > 10:
        base += 1
    return base


def convert_currency(amount, region):
    """折算到本地币种展示（仅展示，结算仍用本位币）。"""
    code, rate = CURRENCY_RATES.get(region, ("CNY", 1.0))
    return code, _round2(amount * rate)


def summarize_user(user):
    return {
        "id": user.get("id"),
        "vip": user.get("vip", False),
        "vip_level": user.get("vip_level", 0) if user.get("vip") else 0,
        "points": user.get("loyalty_points", 0),
        "flags": [k for k in ("new", "blacklist") if user.get(k)],
    }


# --- 下面这段是某次"想优化但没敢上线"的实验，注释着留到了今天（典型死代码） ---
# def _experimental_fast_price(items):
#     # 一次循环算完品类折扣 + 重量 + 行价
#     acc = 0.0
#     for it in items:
#         r = CATEGORY_RULES.get(it.get("cat"), {})
#         q = it["qty"]
#         rate = 1.0
#         if "bulk_qty" in r and q >= r["bulk_qty"]:
#             rate = r["bulk_rate"]
#         acc += it["price"] * q * rate
#     return round(acc, 2)


def health_check():
    """模块自检：库存非负、订单序列单调、事件可读。运维脚本会调它。"""
    problems = []
    for sku, q in _INVENTORY.items():
        if q < 0:
            problems.append("库存为负: %s=%d" % (sku, q))
    seq = _conn().execute("SELECT val FROM seq WHERE name='order'").fetchone()
    if seq is None or seq[0] < 1000:
        problems.append("订单序列异常")
    return {"ok": not problems, "problems": problems,
            "inventory_skus": len(_INVENTORY), "events": len(_EVENTS)}


# =============================================================================
# 税务计算器：考虑了品类免税的"正确版"税。讽刺的是 checkout 根本没调它，
# checkout 用的是一刀切 t*self.tax。所以"系统里其实有两套税口径"——重构时极易踩。
# =============================================================================

class TaxCalculator:
    """按区域 + 品类算税。免税品类来自 TAX_EXEMPT_CATEGORIES。
    这是更细的口径，但只有少数报表/对账脚本用它；下单主链路用的是粗口径。
    """

    def __init__(self, region="cn"):
        self.region = region
        self.rate = TAX_TABLE.get(region, 0.0)
        self.exempt = TAX_EXEMPT_CATEGORIES.get(region, set())

    def line_tax(self, cat, taxable_amount):
        if self.rate == 0.0:
            return 0.0
        if cat in self.exempt:
            return 0.0
        # 奢品在部分区域有附加税
        if cat == "luxury":
            if self.region in ("eu", "uk"):
                return _round2(taxable_amount * (self.rate + 0.05))
            if self.region == "us":
                return _round2(taxable_amount * (self.rate + 0.02))
        # 数字/订阅在 eu 有数字服务税
        if cat in ("digital", "subscription") and self.region in ("eu", "uk"):
            return _round2(taxable_amount * (self.rate + 0.03))
        return _round2(taxable_amount * self.rate)

    def order_tax(self, line_amounts):
        """line_amounts: [(cat, amount)]，逐行算税再汇总（和 checkout 的总额一次性算法不同）。"""
        total = 0.0
        detail = []
        for cat, amt in line_amounts:
            tx = self.line_tax(cat, amt)
            detail.append({"cat": cat, "amount": amt, "tax": tx})
            total += tx
        return {"tax": _round2(total), "detail": detail}

    def effective_rate(self, line_amounts):
        base = sum(a for _, a in line_amounts)
        if base <= 0:
            return 0.0
        r = self.order_tax(line_amounts)["tax"] / base
        return _round2(r)


# =============================================================================
# 运费计算器：基于 SHIPPING_ZONES，支持加急
# =============================================================================

class ShippingCalculator:
    """基于分区表的运费。
    
    """

    def __init__(self, region="cn"):
        self.region = region
        self.zone = SHIPPING_ZONES.get(region, SHIPPING_ZONES["cn"])

    def is_remote(self, province):
        if province is None:
            return False
        return province.upper() in self.zone["remote_provinces"]

    def base_fee(self, province):
        return self.zone["remote"] if self.is_remote(province) else self.zone["local"]

    def weight_fee(self, weight):
        # 阶梯重量费
        if weight <= 1:
            return 0.0
        if weight <= 5:
            return (weight - 1) * 2
        if weight <= 20:
            return 8 + (weight - 5) * 3
        return 8 + 45 + (weight - 20) * 5

    def express_surcharge(self, express):
        return {"none": 0, "standard": 0, "fast": 15, "overnight": 40}.get(express, 0)

    def quote(self, items, province=None, express="standard", free_threshold=None):
        w = cart_weight(items)
        fee = self.base_fee(province) + self.weight_fee(w) + self.express_surcharge(express)
        return _round2(fee)


# =============================================================================
# 促销引擎：一长串季节券 / 满减 / 满赠 / 阶梯折扣的 if/elif（典型越长越没人敢动的分支地狱）
# =============================================================================

SEASONAL_PROMOS = {
    "SPRING": {"min": 200, "off": 30},
    "SUMMER": {"min": 300, "off": 50},
    "AUTUMN": {"min": 150, "off": 20},
    "WINTER": {"min": 500, "off": 100},
    "DOUBLE11": {"min": 0, "rate": 0.7},
    "DOUBLE12": {"min": 100, "rate": 0.85},
    "NEWYEAR": {"min": 88, "off": 8},
}

TIERED_PROMO = [
    (1000, 0.8),
    (500, 0.85),
    (300, 0.9),
    (100, 0.95),
    (0, 1.0),
]


class PromoEngine:
    """促销引擎：季节券 / 阶梯 / 满赠。
    
    """

    def apply_seasonal(self, subtotal, promo_code):
        p = SEASONAL_PROMOS.get(promo_code)
        if not p:
            return subtotal, "无此促销"
        if subtotal < p.get("min", 0):
            return subtotal, "未达门槛 %s" % p.get("min")
        if "off" in p:
            return _round2(subtotal - p["off"]), "立减 %s" % p["off"]
        if "rate" in p:
            return _round2(subtotal * p["rate"]), "打折 %s" % p["rate"]
        return subtotal, "无效促销配置"

    def apply_tiered(self, subtotal):
        """满额阶梯折扣：金额越高折扣越大。返回 (折后, 命中档)。"""
        for threshold, rate in TIERED_PROMO:
            if subtotal >= threshold:
                return _round2(subtotal * rate), {"threshold": threshold, "rate": rate}
        return subtotal, {"threshold": 0, "rate": 1.0}

    def apply_bundle(self, items):
        """满件赠：同品类满 N 件送最便宜一件。"""
        by_cat = {}
        for it in items:
            by_cat.setdefault(it.get("cat"), []).append(it)
        free_value = 0.0
        for cat, group in by_cat.items():
            cnt = sum(g["qty"] for g in group)
            if cnt >= 5:
                cheapest = min(group, key=lambda g: g["price"])
                free_value += cheapest["price"]
        return _round2(free_value)

    def best_of(self, subtotal, items, promo_code=None):
        """择优：季节券 / 阶梯 / 满赠 里挑最划算的。"""
        candidates = []
        if promo_code:
            s, _ = self.apply_seasonal(subtotal, promo_code)
            candidates.append(("seasonal", s))
        tiered, _ = self.apply_tiered(subtotal)
        candidates.append(("tiered", tiered))
        bundle_free = self.apply_bundle(items)
        candidates.append(("bundle", _round2(subtotal - bundle_free)))
        candidates.append(("none", subtotal))
        best = min(candidates, key=lambda x: x[1])
        return {"method": best[0], "subtotal": best[1], "all": candidates}


# =============================================================================
# 结算 v3：用 TaxCalculator / ShippingCalculator / PromoEngine 组合的结算实现
# =============================================================================

def checkout_v3_experimental(items, user, promo_code=None, region="cn",
                             province=None, express="standard"):
    """结算 v3：
      - 用 TaxCalculator（考虑免税品类）
      - 用 ShippingCalculator（分区表）
      - 用 PromoEngine.best_of（择优）
    
    """
    # 行价
    line_amounts = []
    sub = 0.0
    for it in items:
        p = it["price"] * it["qty"]
        c = it.get("cat")
        r = CATEGORY_RULES.get(c, {})
        if c == "book":
            if it["qty"] >= 5:
                p *= 0.8
            elif it["qty"] >= 2:
                p *= 0.95
        elif "bulk_qty" in r and it["qty"] >= r["bulk_qty"]:
            p *= r["bulk_rate"]
        line_amounts.append((c, p))
        sub += p
    # VIP
    if user.get("vip"):
        sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
    # 促销择优
    pe = PromoEngine()
    promo = pe.best_of(sub, items, promo_code)
    sub2 = promo["subtotal"]
    # 税（细口径）
    tc = TaxCalculator(region)
    # 按 sub2/sub 比例缩放各行再算税
    scale = (sub2 / sub) if sub else 1.0
    scaled = [(c, a * scale) for c, a in line_amounts]
    tax = tc.order_tax(scaled)["tax"]
    # 运费（分区）
    sc = ShippingCalculator(region)
    ship = sc.quote(items, province=province, express=express)
    total = _round2(sub2 + tax + ship)
    return {"engine": "v3-exp", "subtotal": _round2(sub2), "tax": tax,
            "shipping": ship, "total": total, "promo": promo["method"]}


# =============================================================================
# 配置自检：检查若干配置表之间是否一致
# =============================================================================

def audit_config_consistency():
    issues = []
    # 1) 每个 SHIPPING_TABLE 的区域是否在 TAX_TABLE 里
    for r in SHIPPING_TABLE:
        if r not in TAX_TABLE:
            issues.append("区域 %s 有运费表但没有税率" % r)
    # 2) SHIPPING_TABLE 和 SHIPPING_ZONES 覆盖的区域是否一致
    a = set(SHIPPING_TABLE.keys())
    b = set(SHIPPING_ZONES.keys())
    if a != b:
        issues.append("运费两套表区域不一致: table-zone=%s zone-table=%s"
                      % (sorted(a - b), sorted(b - a)))
    # 3) COUPON_CATALOG 的 type 是否都被 checkout 支持
    supported = {"fixed", "percent", "firstorder", "freeship", "bogo"}
    for code, c in COUPON_CATALOG.items():
        if c.get("type") not in supported:
            issues.append("券 %s 类型 %s 不被 checkout 支持" % (code, c.get("type")))
    # 4) PROMO_STACK_MATRIX 与 checkout 的叠加实现
    if PROMO_STACK_MATRIX.get("percent", {}).get("vip"):
        issues.append("PROMO_STACK_MATRIX 声明 percent 券可与 VIP 叠加，"
                      "checkout 实现亦叠加")
    # 5) CURRENCY_RATES 覆盖
    for r in TAX_TABLE:
        if r not in CURRENCY_RATES:
            issues.append("区域 %s 缺币种配置" % r)
    return {"consistent": not issues, "issues": issues}


# =============================================================================
# 分区结算：每个区域一个几乎一模一样、又各自被改过一点的方法。
# 每个区域一个 settle_<region> 方法
# checkout 没有调它们，但历史上不同区域的渠道接的是这里的对应方法。
# =============================================================================

class RegionalSettlement:
    """按区域分别结算：settle_<region>。
    
    """

    def _lines(self, items):
        out = []
        for it in items:
            p = it["price"] * it["qty"]
            c = it.get("cat")
            if c == "fresh" and it["qty"] >= 3:
                p *= 0.9
            elif c == "book":
                if it["qty"] >= 5:
                    p *= 0.8
                elif it["qty"] >= 2:
                    p *= 0.95
            elif c == "electronics" and it["qty"] >= 2:
                p *= 0.95
            elif c == "clothing" and it["qty"] >= 4:
                p *= 0.92
            elif c == "grocery" and it["qty"] >= 6:
                p *= 0.93
            out.append((c, p))
        return out

    def settle_cn(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        tax = 0.0  # cn 不加税
        ship = 0.0 if sub >= 99 else 8 + 2 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_us(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        # us 对 grocery/book 免税（私改：这里读了 TAX_EXEMPT，cn 那个方法没读）
        taxable = sum(a for c, a in lines if c not in TAX_EXEMPT_CATEGORIES["us"])
        ratio = (taxable / sub) if sub else 0.0
        tax = sub * 0.08 * ratio
        ship = 0.0 if sub >= 150 else 12 + 3 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_eu(self, items, user):
        lines = self._lines(items)
        # eu 私改：逐行取整（和 checkout 的 EU 行为对齐，但这里没有电子双折那段）
        sub = 0.0
        for c, a in lines:
            sub += _round2(a)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        taxable = sum(a for c, a in lines if c not in TAX_EXEMPT_CATEGORIES["eu"])
        ratio = (taxable / sub) if sub else 0.0
        tax = sub * 0.2 * ratio
        ship = 0.0 if sub >= 200 else 15 + 4 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_uk(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        taxable = sum(a for c, a in lines if c not in TAX_EXEMPT_CATEGORIES["uk"])
        ratio = (taxable / sub) if sub else 0.0
        tax = sub * 0.2 * ratio
        ship = 0.0 if sub >= 200 else 14 + 4 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_jp(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        tax = sub * 0.1
        ship = 0.0 if sub >= 120 else 10 + 3 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_au(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        taxable = sum(a for c, a in lines if c not in TAX_EXEMPT_CATEGORIES["au"])
        ratio = (taxable / sub) if sub else 0.0
        tax = sub * 0.1 * ratio
        ship = 0.0 if sub >= 250 else 18 + 5 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle_sg(self, items, user):
        lines = self._lines(items)
        sub = sum(a for _, a in lines)
        if user.get("vip"):
            sub *= VIP_TIERS.get(user.get("vip_level", 1), 1.0)
        tax = sub * 0.07
        ship = 0.0 if sub >= 88 else 9 + 2 * cart_weight(items)
        return _round2(sub + tax + ship)

    def settle(self, region, items, user):
        """dispatch 到对应区域方法。新增区域要记得来这里加分支（经常被忘）。"""
        fn = {
            "cn": self.settle_cn, "us": self.settle_us, "eu": self.settle_eu,
            "uk": self.settle_uk, "jp": self.settle_jp, "au": self.settle_au,
            "sg": self.settle_sg,
        }.get(region)
        if fn is None:
            return self.settle_cn(items, user)  # 静默兜底到 cn
        return fn(items, user)


# =============================================================================
# 礼品卡 / 储值
# =============================================================================

# _GIFT_CARDS 见上方 sqlite 存储层


class GiftCardService:
    def issue(self, code, amount):
        _GIFT_CARDS[code] = _GIFT_CARDS.get(code, 0.0) + amount
        _emit("giftcard_issued", {"code": code, "amount": amount})
        return _GIFT_CARDS[code]

    def balance(self, code):
        return _GIFT_CARDS.get(code, 0.0)

    def redeem(self, code, amount):
        """用礼品卡抵扣 amount，返回实际抵扣额。余额不足只抵到 0。"""
        bal = _GIFT_CARDS.get(code, 0.0) or 0.0
        amt = amount or 0
        used = min(bal, amt)
        _GIFT_CARDS[code] = bal - used
        _audit("giftcard %s used %s remain %s" % (code, used, _GIFT_CARDS[code]))
        return used

    def apply_to_order(self, order, code):
        """把礼品卡抵扣应用到一笔已结算订单上（事后调整 total）。
        """
        used = self.redeem(code, order.get("total", 0.0))
        order["total"] = _round2(order.get("total", 0.0) - used)
        order["giftcard_used"] = used
        return order


# =============================================================================
# 订阅计费：周期性扣费，复用了一点结算逻辑，又自带一套折扣（年付折扣）
# =============================================================================

SUBSCRIPTION_PLANS = {
    "basic": {"monthly": 30, "annual_discount": 0.85},
    "pro": {"monthly": 80, "annual_discount": 0.8},
    "team": {"monthly": 200, "annual_discount": 0.75},
}


class SubscriptionBilling:
    def __init__(self, region="cn"):
        self.region = region
        self.tax = TAX_TABLE.get(region, 0.0)

    def price(self, plan, period="monthly", seats=1):
        cfg = SUBSCRIPTION_PLANS.get(plan)
        if not cfg:
            return {"ok": False, "reason": "无此套餐"}
        monthly = cfg["monthly"] * seats
        if period == "annual":
            base = monthly * 12 * cfg["annual_discount"]
        elif period == "quarterly":
            base = monthly * 3 * 0.95     # 季付私加的 0.95，文档里没写
        else:
            base = monthly
        tax = base * self.tax
        return {"ok": True, "plan": plan, "period": period, "seats": seats,
                "base": _round2(base), "tax": _round2(tax),
                "total": _round2(base + tax)}

    def renew(self, user, plan, period="monthly", seats=1):
        q = self.price(plan, period, seats)
        if not q.get("ok"):
            return q
        oid = _next_order_id()
        order = {"id": oid, "user": user.get("id"), "region": self.region,
                 "kind": "subscription", "total": q["total"], "status": "confirmed",
                 "items": [{"cat": "subscription", "line": q["base"], "qty": seats}],
                 "points_earned": int(q["base"]), "points_used": 0,
                 "breakdown": q}
        _emit("subscription_renewed", {"order_id": oid, "total": q["total"]})
        _audit("subscription renew %s %s total=%s" % (plan, period, q["total"]))
        return order


# =============================================================================
# 漏斗 / 留存分析：读事件流做统计
# =============================================================================

class Analytics:
    def funnel(self):
        """下单漏斗：尝试(confirmed+rejected+oos) → 确认。从事件流粗略推。"""
        confirmed = sum(1 for e in _EVENTS if e["kind"] == "order_confirmed")
        rejected = sum(1 for e in _EVENTS if e["kind"] == "order_rejected")
        refunded = sum(1 for e in _EVENTS if e["kind"] == "order_refunded")
        attempts = confirmed + rejected
        conv = (confirmed / attempts) if attempts else 0.0
        return {"attempts": attempts, "confirmed": confirmed,
                "rejected": rejected, "refunded": refunded,
                "conversion": _round2(conv)}

    def avg_order_value(self, store):
        confirmed = [o for o in store.values() if o.get("status") == "confirmed"]
        if not confirmed:
            return 0.0
        return _round2(sum(o["total"] for o in confirmed) / len(confirmed))

    def refund_rate(self, store):
        confirmed = sum(1 for o in store.values() if o.get("status") == "confirmed")
        refunded = sum(1 for e in _EVENTS if e["kind"] == "order_refunded")
        if not confirmed:
            return 0.0
        return _round2(refunded / confirmed)

    def category_mix(self, store):
        mix = {}
        for o in store.values():
            for li in o.get("items", []):
                c = li.get("cat") or "unknown"
                mix[c] = mix.get(c, 0) + li.get("qty", 0)
        return mix

    def top_skus(self, store, n=5):
        cnt = {}
        for o in store.values():
            for li in o.get("items", []):
                sku = li.get("sku")
                if sku:
                    cnt[sku] = cnt.get(sku, 0) + li.get("qty", 0)
        ranked = sorted(cnt.items(), key=lambda x: x[1], reverse=True)
        return ranked[:n]


# =============================================================================
# 假 DB 适配层：在 store dict 上模拟一点 SQL-ish 查询，老报表通过它取数
# =============================================================================

class LegacyDBAdapter:
    """把 store dict 当表查。很多老报表只认这个接口，所以它不能随便删。"""

    def __init__(self, store):
        self.store = store

    def select_where(self, **conds):
        out = []
        for o in self.store.values():
            ok = True
            for k, v in conds.items():
                if o.get(k) != v:
                    ok = False
                    break
            if ok:
                out.append(o)
        return out

    def count_where(self, **conds):
        return len(self.select_where(**conds))

    def sum_field(self, field, **conds):
        return _round2(sum(o.get(field, 0) for o in self.select_where(**conds)))

    def order_by(self, field, desc=False):
        return sorted(self.store.values(), key=lambda o: o.get(field, 0), reverse=desc)


# =============================================================================
# 扩展风控规则引擎：一长串确定性规则（和 OrderSystem.risk_score 又是两套打分口径）
# =============================================================================

EXT_RISK_RULES = [
    {"id": "blacklist", "weight": 100, "desc": "黑名单用户"},
    {"id": "amount_hard", "weight": 100, "desc": "金额超硬上限"},
    {"id": "amount_soft", "weight": 40, "desc": "金额超软上限"},
    {"id": "item_flood", "weight": 30, "desc": "单数过多"},
    {"id": "new_big", "weight": 25, "desc": "新客大额"},
    {"id": "luxury_big", "weight": 20, "desc": "奢品大额"},
    {"id": "many_skus", "weight": 15, "desc": "SKU 种类过多"},
    {"id": "points_drain", "weight": 10, "desc": "一次性大额用积分"},
    {"id": "midnight", "weight": 0, "desc": "（占位）下单时段——已停用，确定性引擎不看时间"},
]


class ExtRiskEngine:
    """更细的风控。每条规则单独可解释，返回命中明细。
    它和 OrderSystem.risk_score 的总分口径不同（这里把 many_skus / points_drain 也算进去），
    所以同一单两套风控分不一样——重构时要小心别把'对外暴露的那套分'换掉。
    """

    def evaluate(self, items, user, amount, points_used=0):
        hits = []

        def hit(rid):
            for r in EXT_RISK_RULES:
                if r["id"] == rid:
                    hits.append(r)
                    return

        if user.get("blacklist"):
            hit("blacklist")
        if amount > RISK_RULES["amount_hard_limit"]:
            hit("amount_hard")
        elif amount > RISK_RULES["amount_soft_limit"]:
            hit("amount_soft")
        if cart_item_count(items) > RISK_RULES["item_count_limit"]:
            hit("item_flood")
        if user.get("new") and amount > 3000:
            hit("new_big")
        for it in items:
            if it.get("cat") == "luxury" and it["price"] * it["qty"] > 5000:
                hit("luxury_big")
                break
        if len(distinct_categories(items)) >= 6:
            hit("many_skus")
        if points_used >= 5000:
            hit("points_drain")
        score = sum(h["weight"] for h in hits)
        return {"score": score, "hits": [h["id"] for h in hits],
                "reject": score >= RISK_RULES["reject_score"],
                "explain": [h["desc"] for h in hits]}


# =============================================================================
# 券生成器：按规则批量生成券码并登记到 COUPON_CATALOG（运营后台用，会改全局券目录）
# =============================================================================

class CouponIssuer:
    """生成券并写进 COUPON_CATALOG。
    
    """

    def _code(self, prefix, n):
        return "%s%04d" % (prefix, n)

    def issue_fixed(self, n, amount, threshold, start=1):
        codes = []
        for i in range(start, start + n):
            code = self._code("FX", i)
            COUPON_CATALOG[code] = {"type": "fixed", "amount": amount,
                                    "threshold": threshold}
            codes.append(code)
        _audit("issued %d fixed coupons amount=%s" % (n, amount))
        return codes

    def issue_percent(self, n, rate, threshold, start=1):
        codes = []
        for i in range(start, start + n):
            code = self._code("PC", i)
            COUPON_CATALOG[code] = {"type": "percent", "rate": rate,
                                    "threshold": threshold}
            codes.append(code)
        _audit("issued %d percent coupons rate=%s" % (n, rate))
        return codes

    def revoke(self, code):
        if code in COUPON_CATALOG:
            del COUPON_CATALOG[code]
            _audit("revoked coupon %s" % code)
            return True
        return False

    def validate_code(self, code):
        c = COUPON_CATALOG.get(code)
        if not c:
            return {"valid": False, "reason": "不存在"}
        if c.get("type") == "fixed" and c.get("amount", 0) <= 0:
            return {"valid": False, "reason": "立减额非正"}
        if c.get("type") == "percent" and not (0 < c.get("rate", 0) < 1):
            return {"valid": False, "reason": "折扣率越界"}
        return {"valid": True, "coupon": c}


# =============================================================================
# 仓储路由：根据区域 + 库存挑发货仓
# =============================================================================

WAREHOUSES = {
    "cn": ["CN-SH", "CN-GZ", "CN-CD"],
    "us": ["US-CA", "US-NJ"],
    "eu": ["EU-DE", "EU-NL"],
    "uk": ["UK-LON"],
    "jp": ["JP-TKO"],
    "au": ["AU-SYD"],
    "sg": ["SG-CTL"],
}

# 各仓的本地库存
WAREHOUSE_STOCK = {
    "CN-SH": {"SKU-FRESH-1": 40, "SKU-BOOK-1": 80, "SKU-ELEC-1": 8},
    "CN-GZ": {"SKU-FRESH-1": 35, "SKU-BOOK-1": 70, "SKU-ELEC-1": 7},
    "CN-CD": {"SKU-FRESH-1": 25, "SKU-BOOK-1": 50, "SKU-ELEC-1": 5},
    "US-CA": {"SKU-BOOK-1": 60, "SKU-ELEC-2": 3},
    "US-NJ": {"SKU-BOOK-1": 40, "SKU-ELEC-2": 2},
    "EU-DE": {"SKU-BOOK-2": 15, "SKU-CLOTH-1": 40},
    "EU-NL": {"SKU-BOOK-2": 15, "SKU-CLOTH-1": 40},
}


class WarehouseRouter:
    def candidates(self, region):
        return WAREHOUSES.get(region, WAREHOUSES["cn"])

    def can_fulfill(self, warehouse, items):
        stock = WAREHOUSE_STOCK.get(warehouse, {})
        for it in items:
            sku = it.get("sku")
            if sku is None:
                continue
            if stock.get(sku, 0) < it["qty"]:
                return False
        return True

    def route(self, region, items):
        """挑第一个能整单发出的仓；都不行就拆单。"""
        whs = self.candidates(region)
        for w in whs:
            if self.can_fulfill(w, items):
                return {"split": False, "warehouse": w}
        # 拆单：每个 sku 找一个有货的仓
        plan = {}
        unfulfilled = []
        for it in items:
            sku = it.get("sku")
            placed = False
            for w in whs:
                if WAREHOUSE_STOCK.get(w, {}).get(sku, 0) >= it["qty"]:
                    plan.setdefault(w, []).append(sku)
                    placed = True
                    break
            if not placed:
                unfulfilled.append(sku)
        return {"split": True, "plan": plan, "unfulfilled": unfulfilled}


# =============================================================================
# 完整报价装配器：把 Tax/Shipping/Promo/Warehouse/Risk 拼成一份"给前端的完整报价"。
# 它是最爱被各渠道误用的入口——因为它"看起来最全"，但口径和真实 checkout 又不一样。
# =============================================================================

class PriceQuoteBuilder:
    def __init__(self, region="cn"):
        self.region = region

    def build(self, items, user, coupon=None, promo_code=None,
              province=None, express="standard"):
        # 行价
        lines = []
        sub = 0.0
        for it in items:
            p = it["price"] * it["qty"]
            c = it.get("cat")
            r = CATEGORY_RULES.get(c, {})
            if c == "book":
                if it["qty"] >= 5:
                    p *= 0.8
                elif it["qty"] >= 2:
                    p *= 0.95
            elif "bulk_qty" in r and it["qty"] >= r["bulk_qty"]:
                p *= r["bulk_rate"]
            lines.append({"sku": it.get("sku"), "cat": c, "qty": it["qty"], "line": _round2(p)})
            sub += p
        # VIP
        vip_rate = VIP_TIERS.get(user.get("vip_level", 1), 1.0) if user.get("vip") else 1.0
        sub_vip = sub * vip_rate
        # 促销择优
        pe = PromoEngine()
        promo = pe.best_of(sub_vip, items, promo_code)
        sub_promo = promo["subtotal"]
        # 税（细口径）
        tc = TaxCalculator(self.region)
        scale = (sub_promo / sub_vip) if sub_vip else 1.0
        tax = tc.order_tax([(li["cat"], li["line"] * scale) for li in lines])["tax"]
        # 运费（分区）
        sc = ShippingCalculator(self.region)
        ship = sc.quote(items, province=province, express=express)
        # 风控（细口径）
        re = ExtRiskEngine()
        risk = re.evaluate(items, user, sub_promo + tax + ship)
        # 仓储
        wr = WarehouseRouter()
        routing = wr.route(self.region, items)
        total = _round2(sub_promo + tax + ship)
        return {
            "region": self.region,
            "lines": lines,
            "subtotal": _round2(sub),
            "subtotal_after_vip": _round2(sub_vip),
            "subtotal_after_promo": _round2(sub_promo),
            "promo_method": promo["method"],
            "tax": tax,
            "shipping": ship,
            "total": total,
            "currency": convert_currency(total, self.region),
            "risk": risk,
            "routing": routing,
            "eta_days": estimate_delivery_days(self.region, cart_weight(items)),
        }


# =============================================================================
# 多语言小票打印：把订单渲染成一长串文本小票。纯展示，但口径又自成一套。
# =============================================================================

class ReceiptPrinter:
    def __init__(self, region="cn"):
        self.region = region

    def _t(self, key):
        table = {
            "cn": {"title": "购物小票", "item": "商品", "qty": "数量",
                   "amount": "金额", "subtotal": "小计", "tax": "税",
                   "shipping": "运费", "total": "合计", "points": "获得积分"},
            "us": {"title": "RECEIPT", "item": "ITEM", "qty": "QTY",
                   "amount": "AMOUNT", "subtotal": "SUBTOTAL", "tax": "TAX",
                   "shipping": "SHIPPING", "total": "TOTAL", "points": "POINTS EARNED"},
        }
        return (table.get(self.region, table["cn"]) or {}).get(key, key) or key

    def render(self, order):
        w = 40
        out = []
        out.append(self._t("title").center(w))
        out.append("=" * w)
        out.append("%-20s %5s %12s" % (self._t("item"), self._t("qty"), self._t("amount")))
        out.append("-" * w)
        for li in order.get("items", []):
            name = str(li.get("sku") or li.get("cat") or "item")[:20]
            out.append("%-20s %5d %12s" % (
                name, li.get("qty", 0),
                format_money(li.get("line", 0.0), self.region)))
        out.append("-" * w)
        bd = order.get("breakdown", {})
        if "tax" in bd:
            out.append("%-25s %14s" % (self._t("tax"),
                                       format_money(bd.get("tax", 0.0), self.region)))
        if "shipping" in bd:
            out.append("%-25s %14s" % (self._t("shipping"),
                                       format_money(bd.get("shipping", 0.0), self.region)))
        out.append("=" * w)
        out.append("%-25s %14s" % (self._t("total"),
                                   format_money(order.get("total", 0.0), self.region)))
        if order.get("points_earned"):
            out.append("%-25s %14d" % (self._t("points"), order.get("points_earned", 0)))
        return "\n".join(out)


# =============================================================================
# 结算对账器：把同一个购物车跑过各结算实现并对比结果
# =============================================================================

class SettlementReconciler:
    """对账：checkout（god）/ calc_v0 / calc_v1 / calc_v2 / RegionalSettlement /
    checkout_v3_experimental / PriceQuoteBuilder —— 同一单，七条路，七个数。
    """

    def run(self, items, user, region="cn", coupon=None):
        results = {}

        # 1) 正式 checkout（god 方法）
        reset_state()
        s = OrderSystem(region=region)
        o = s.checkout(items, user, coupon=coupon, dry_run=True)
        results["checkout"] = o["total"]

        # 2) calc_v0（最老）
        results["calc_v0"] = calc_v0(items, user, region)

        # 3) calc_v1
        results["calc_v1"] = calc_v1(items, user, coupon, region)

        # 4) calc_v2
        results["calc_v2"] = calc_v2(items, user, coupon, region)

        # 5) RegionalSettlement（无券）
        rs = RegionalSettlement()
        results["regional"] = rs.settle(region, items, user)

        # 6) checkout_v3_experimental（无券，走 promo）
        v3 = checkout_v3_experimental(items, user, region=region)
        results["v3_exp"] = v3["total"]

        # 7) PriceQuoteBuilder
        qb = PriceQuoteBuilder(region)
        results["quote_builder"] = qb.build(items, user, coupon=coupon)["total"]

        vals = list(results.values())
        spread = _round2(max(vals) - min(vals))
        agree = len(set(vals)) == 1
        return {"results": results, "spread": spread, "all_agree": agree,
                "distinct_values": sorted(set(vals))}

    def report(self, items, user, region="cn", coupon=None):
        r = self.run(items, user, region, coupon)
        lines = ["==== 结算口径对账（region=%s, coupon=%s）====" % (region, coupon)]
        for k, v in r["results"].items():
            lines.append("  %-16s : %s" % (k, v))
        lines.append("  %-16s : %s" % ("最大极差", r["spread"]))
        lines.append("  %-16s : %s" % ("全部一致?", r["all_agree"]))
        lines.append("  %-16s : %s" % ("不同取值", r["distinct_values"]))
        return "\n".join(lines)


# =============================================================================
# 退单 / 拒付中心：处理 chargeback、争议、风控复议。直接改订单状态 + 库存 + 事件。
# =============================================================================

class DisputeCenter:
    def __init__(self, store):
        self.store = store
        self.cases = []

    def open_case(self, order_id, reason):
        order = self.store.get(order_id)
        if not order:
            return {"ok": False, "reason": "订单不存在"}
        case = {"order_id": order_id, "reason": reason, "status": "open"}
        self.cases.append(case)
        _emit("dispute_opened", {"order_id": order_id, "reason": reason})
        _audit("dispute opened for %s: %s" % (order_id, reason))
        return {"ok": True, "case": case}

    def resolve(self, order_id, outcome):
        """outcome: 'refund' / 'reject' / 'partial'。refund 会把订单标成 refunded 并补库存。"""
        order = self.store.get(order_id)
        if not order:
            return {"ok": False, "reason": "订单不存在"}
        for c in self.cases:
            if c["order_id"] == order_id and c["status"] == "open":
                c["status"] = "resolved:" + outcome
        if outcome == "refund":
            order["status"] = "refunded"
            # 补库存（按订单行）
            for li in order.get("items", []):
                sku = li.get("sku")
                if sku:
                    _INVENTORY[sku] = _INVENTORY.get(sku, 0) + li.get("qty", 0)
            _emit("order_refunded", {"order_id": order_id, "amount": order.get("total", 0.0)})
        elif outcome == "reject":
            order["status"] = "dispute_rejected"
        _audit("dispute %s resolved: %s" % (order_id, outcome))
        return {"ok": True, "status": order["status"]}

    def reconsider_risk(self, order_id, items, user):
        """风控复议：用扩展风控引擎重算，给人工一个参考。"""
        order = self.store.get(order_id)
        if not order:
            return {"ok": False, "reason": "订单不存在"}
        re = ExtRiskEngine()
        ev = re.evaluate(items, user, order.get("total", 0.0))
        return {"ok": True, "order_id": order_id, "ext_risk": ev,
                "original_status": order.get("status")}


# =============================================================================
# 组合套餐目录：把多个 sku 打包成套餐价
# =============================================================================

BUNDLES = {
    "BUNDLE-READER": {
        "items": [{"sku": "SKU-BOOK-1", "qty": 3}, {"sku": "SKU-DIGI-1", "qty": 1}],
        "price": 99,
        "tag": "读书套装",
    },
    "BUNDLE-FRESH": {
        "items": [{"sku": "SKU-FRESH-1", "qty": 5}, {"sku": "SKU-FRESH-2", "qty": 3}],
        "price": 120,
        "tag": "生鲜礼包",
    },
    "BUNDLE-GEAR": {
        "items": [{"sku": "SKU-ELEC-1", "qty": 1}, {"sku": "SKU-CLOTH-1", "qty": 2}],
        "price": 1500,
        "tag": "出行装备",
    },
}


class BundleCatalog:
    def list_bundles(self):
        return [{"code": k, "tag": v["tag"], "price": v["price"]} for k, v in BUNDLES.items()]

    def expand(self, bundle_code):
        """把套餐展开成行项目，价格按数量均摊到各行。"""
        b = BUNDLES.get(bundle_code)
        if not b:
            return None
        total_qty = sum(i["qty"] for i in b["items"])
        unit = b["price"] / total_qty if total_qty else 0.0
        lines = []
        for i in b["items"]:
            lines.append({"sku": i["sku"], "qty": i["qty"],
                          "price": _round2(unit), "cat": self._cat_of(i["sku"])})
        return {"code": bundle_code, "lines": lines, "price": b["price"]}

    def _cat_of(self, sku):
        if "BOOK" in sku:
            return "book"
        if "FRESH" in sku:
            return "fresh"
        if "ELEC" in sku:
            return "electronics"
        if "CLOTH" in sku:
            return "clothing"
        if "DIGI" in sku:
            return "digital"
        if "LUX" in sku:
            return "luxury"
        if "GROC" in sku:
            return "grocery"
        if "SUB" in sku:
            return "subscription"
        return None

    def savings(self, bundle_code):
        """套餐相比单买省多少（单价用内置表）。"""
        unit_prices = {
            "SKU-BOOK-1": 30, "SKU-DIGI-1": 20, "SKU-FRESH-1": 20,
            "SKU-FRESH-2": 25, "SKU-ELEC-1": 1000, "SKU-CLOTH-1": 200,
        }
        b = BUNDLES.get(bundle_code)
        if not b:
            return None
        regular = sum(unit_prices.get(i["sku"], 0) * i["qty"] for i in b["items"])
        return {"bundle_price": b["price"], "regular": regular,
                "save": _round2(regular - b["price"])}


# =============================================================================
# 报税汇总：按区域汇总应税额与税额
# =============================================================================

class TaxFilingReport:
    def __init__(self, store):
        self.store = store

    def by_region(self):
        agg = {}
        for o in self.store.values():
            if o.get("status") != "confirmed":
                continue
            r = o.get("region", "cn")
            bd = o.get("breakdown", {})
            agg.setdefault(r, {"taxable": 0.0, "tax": 0.0, "orders": 0})
            agg[r]["tax"] += bd.get("tax", 0.0)
            agg[r]["taxable"] += bd.get("after_coupon", o.get("total", 0.0))
            agg[r]["orders"] += 1
        for r in agg:
            agg[r]["tax"] = _round2(agg[r]["tax"])
            agg[r]["taxable"] = _round2(agg[r]["taxable"])
        return agg

    def total_tax(self):
        return _round2(sum(v["tax"] for v in self.by_region().values()))


# =============================================================================
# 旧函数名兼容层：转发到新实现
# 文档注释写得很重，因为删一个都可能炸到某个没人知道的老调用方。
# =============================================================================

def legacy_calc(items, user, coupon=None, region="cn"):
    """@deprecated 用 dispatch_checkout。保留是因为旧批处理脚本还按这个名字 import。"""
    return calc_v1(items, user, coupon, region)


def legacy_calc_total(cart, customer, region="cn"):
    """@deprecated 参数名不同的另一个老入口（cart/customer）。某个报表系统在用。"""
    return calc_v1(cart, customer, None, region)


def get_price(items, user, **kw):
    """@deprecated 含糊的老名字，内部已经换成 dispatch_checkout。别再新增调用方。"""
    r = dispatch_checkout(items, user, **kw)
    return r.get("total") if isinstance(r, dict) else r


def quick_total(items):
    """@deprecated 不带 user 的"快算"，假设非 VIP、cn 区、无券。某个看板还在用。"""
    return calc_v1(items, {}, None, "cn")


# =============================================================================
# 订单状态机：合法状态流转。checkout 直接赋 status 字符串，根本没走这台状态机——
# 所以"非法状态"在系统里其实是能出现的。状态机只在退款/争议路径被零散用到。
# =============================================================================

ORDER_STATES = ["created", "confirmed", "out_of_stock", "rejected",
                "refunded", "dispute_rejected", "closed"]

ALLOWED_TRANSITIONS = {
    "created": {"confirmed", "out_of_stock", "rejected"},
    "confirmed": {"refunded", "closed"},
    "out_of_stock": {"created", "closed"},
    "rejected": {"closed"},
    "refunded": {"closed"},
    "dispute_rejected": {"closed"},
    "closed": set(),
}


class OrderStateMachine:
    def can(self, frm, to):
        return to in ALLOWED_TRANSITIONS.get(frm, set())

    def transition(self, order, to):
        frm = order.get("status", "created")
        if not self.can(frm, to):
            return {"ok": False, "reason": "非法流转 %s -> %s" % (frm, to)}
        order["status"] = to
        _audit("state %s: %s -> %s" % (order.get("id"), frm, to))
        return {"ok": True, "status": to}

    def is_terminal(self, order):
        return order.get("status") in ("closed", "rejected", "refunded", "dispute_rejected")


# =============================================================================
# 通知分发：多渠道（站内/邮件/短信/push）路由。读 user 偏好，写全局 outbox。
# 和 OrderSystem.notify 是两套发送实现，渠道选择规则也不同。
# =============================================================================

class NotificationDispatcher:
    def __init__(self, region="cn"):
        self.region = region

    def _channels(self, user):
        prefs = user.get("notify_prefs")
        if prefs:
            return prefs
        # 默认：有手机号发短信，否则站内
        if user.get("phone"):
            return ["sms", "inapp"]
        if user.get("email"):
            return ["email", "inapp"]
        return ["inapp"]

    def dispatch(self, user, order):
        text = render_notification(order, self.region)
        sent = []
        for ch in self._channels(user):
            _audit("notify[%s] %s: %s" % (ch, user.get("id"), text))
            sent.append(ch)
        _emit("notification_sent", {"order_id": order.get("id"), "channels": sent})
        return {"sent": sent, "text": text}

    def broadcast(self, users, order):
        out = []
        for u in users:
            out.append(self.dispatch(u, order))
        return out


# =============================================================================
# 配置管理：把散落的 FEATURE_FLAGS / 表 包一层 getter/setter。
# 危险点：set_flag 直接改全局 FEATURE_FLAGS，影响所有后续 checkout——典型隐式全局耦合。
# =============================================================================

class ConfigManager:
    def get_flag(self, name, default=None):
        return FEATURE_FLAGS.get(name, default)

    def set_flag(self, name, value):
        old = FEATURE_FLAGS.get(name)
        FEATURE_FLAGS[name] = value
        _audit("flag %s: %s -> %s" % (name, old, value))
        return value

    def enable_legacy(self):
        return self.set_flag("use_legacy_v1", True)

    def disable_legacy(self):
        return self.set_flag("use_legacy_v1", False)

    def tax_rate(self, region):
        return TAX_TABLE.get(region, 0.0)

    def set_tax_rate(self, region, rate):
        """改全局税率。改完所有 OrderSystem 实例的下一单都受影响（实例 self.tax 是构造时快照，
        所以已建实例不变、新建实例才变——这个'半生效'正是隐式耦合最坑的地方）。"""
        TAX_TABLE[region] = rate
        _audit("tax %s -> %s" % (region, rate))
        return rate

    def snapshot(self):
        return {"flags": dict(FEATURE_FLAGS),
                "tax": dict(TAX_TABLE),
                "coupons": list(COUPON_CATALOG.keys())}


# =============================================================================
# 客户分层：根据历史消费/积分给客户打标，运营按标做活动。读 store + user。
# =============================================================================

class CustomerSegmentation:
    def __init__(self, store):
        self.store = store

    def lifetime_value(self, user_id):
        total = 0.0
        for o in self.store.values():
            if o.get("user") == user_id and o.get("status") == "confirmed":
                total += o.get("total", 0.0)
        return _round2(total)

    def order_count(self, user_id):
        return sum(1 for o in self.store.values()
                   if o.get("user") == user_id and o.get("status") == "confirmed")

    def segment(self, user):
        ltv = self.lifetime_value(user.get("id"))
        cnt = self.order_count(user.get("id"))
        pts = user.get("loyalty_points", 0)
        if ltv >= 50000 or pts >= 10000:
            tag = "钻石"
        elif ltv >= 10000 or pts >= 3000:
            tag = "金牌"
        elif ltv >= 2000 or cnt >= 5:
            tag = "银牌"
        elif cnt >= 1:
            tag = "普通"
        else:
            tag = "新客"
        return {"user": user.get("id"), "segment": tag, "ltv": ltv,
                "orders": cnt, "points": pts}

    def churn_risk(self, user_id):
        """流失风险（确定性占位：订单数为 0 视为高，否则低）。真实系统会看最近活跃。"""
        cnt = self.order_count(user_id)
        if cnt == 0:
            return "high"
        if cnt < 3:
            return "medium"
        return "low"


# =============================================================================
# 造数：批量生成一批订单
# =============================================================================

def seed_demo_data(store=None):
    """造一批订单（含正常单、被风控拒的单、缺货单），返回订单表。
    
    """
    reset_state()
    store = store if store is not None else _ORDERS

    scenarios = [
        # (region, items, user, coupon, use_points)
        ("cn", [{"sku": "SKU-FRESH-1", "price": 20, "qty": 3, "cat": "fresh", "weight": 1.0},
                {"sku": "SKU-BOOK-1", "price": 30, "qty": 5, "cat": "book", "weight": 0.4}],
         {"id": "alice", "vip": True, "vip_level": 2, "loyalty_points": 500}, "PCT10", 0),
        ("us", [{"sku": "SKU-ELEC-1", "price": 1000, "qty": 2, "cat": "electronics", "weight": 3.0}],
         {"id": "bob", "vip": False, "loyalty_points": 0}, None, 0),
        ("eu", [{"sku": "SKU-BOOK-2", "price": 33.33, "qty": 2, "cat": "book", "weight": 0.4},
                {"sku": "SKU-CLOTH-1", "price": 80, "qty": 4, "cat": "clothing", "weight": 1.2}],
         {"id": "carol", "vip": True, "vip_level": 4, "loyalty_points": 3000}, "FIX50", 200),
        # 黑名单用户（会被风控拒）
        ("cn", [{"sku": "SKU-ELEC-2", "price": 100, "qty": 2, "cat": "electronics", "weight": 2.0}],
         {"id": "mallory", "blacklist": True, "loyalty_points": 0}, None, 0),
        # 这一单缺货（SKU-LUX-1 只有 3 件，要 5 件）
        ("eu", [{"sku": "SKU-LUX-1", "price": 8000, "qty": 5, "cat": "luxury", "weight": 0.5}],
         {"id": "dave", "vip": True, "vip_level": 5, "loyalty_points": 8000}, None, 0),
        ("jp", [{"sku": "SKU-GROC-1", "price": 12, "qty": 8, "cat": "grocery", "weight": 0.3}],
         {"id": "erin", "vip": False, "loyalty_points": 100}, "FREESHIP", 0),
        ("sg", [{"sku": "SKU-DIGI-1", "price": 60, "qty": 1, "cat": "digital", "weight": 0.0}],
         {"id": "frank", "vip": True, "vip_level": 1, "loyalty_points": 0}, None, 0),
    ]

    for region, items, user, coupon, pts in scenarios:
        s = OrderSystem(region=region, store=store)
        s.checkout(items, user, coupon=coupon, use_points=pts)
    return store


def demo_full_pipeline():
    """把主要子系统串一遍，输出一份概览。"""
    store = seed_demo_data()
    rb = ReportBuilder(store)
    inv = InventoryAdmin()
    out = []
    out.append(rb.daily_text_report())
    out.append("")
    out.append("库存对账:")
    out.append("  " + str(inv.reconcile(store)))
    out.append("")
    out.append("两套 GMV 对账:")
    out.append("  " + str(rb.reconcile_gmv()))
    out.append("")
    out.append("配置一致性自检:")
    out.append("  " + str(audit_config_consistency()))
    return "\n".join(out)


# =============================================================================
# 支付模拟：确定性"网关"（按金额/币种规则决定成功失败，不用随机/网络）。
# 它在 checkout 之外，但很多上层流程把"支付"和"下单"耦在一起调，状态来回写。
# =============================================================================

PAYMENT_METHODS = {
    "wallet": {"max": 5000, "fee_rate": 0.0},
    "card": {"max": 100000, "fee_rate": 0.006},
    "bnpl": {"max": 3000, "fee_rate": 0.0, "new_user_blocked": True},
    "giftcard": {"max": 100000, "fee_rate": 0.0},
}

# _PAYMENTS 见上方 sqlite 存储层


class PaymentSimulator:
    """确定性支付。规则：
      - 金额超过支付方式上限 → declined
      - bnpl 对新客封禁 → declined
      - card 收 0.6% 手续费（加在订单外，单独记）
      - 其余 → captured
    没有随机、没有时间，结果确定。
    """

    def authorize(self, order, method="card", user=None):
        cfg = PAYMENT_METHODS.get(method)
        amount = order.get("total", 0.0)
        if not cfg:
            return {"ok": False, "status": "invalid_method"}
        if amount > cfg["max"]:
            rec = {"order_id": order.get("id"), "status": "declined",
                   "reason": "超过 %s 上限 %s" % (method, cfg["max"]),
                   "method": method, "amount": amount}
            _PAYMENTS[order.get("id")] = rec
            return rec
        if cfg.get("new_user_blocked") and user and user.get("new"):
            rec = {"order_id": order.get("id"), "status": "declined",
                   "reason": "新客不可用 %s" % method, "method": method, "amount": amount}
            _PAYMENTS[order.get("id")] = rec
            return rec
        fee = _round2(amount * cfg["fee_rate"])
        rec = {"order_id": order.get("id"), "status": "captured", "method": method,
               "amount": amount, "fee": fee}
        _PAYMENTS[order.get("id")] = rec
        _emit("payment_captured", {"order_id": order.get("id"), "amount": amount, "fee": fee})
        _audit("payment %s %s amount=%s fee=%s" % (order.get("id"), method, amount, fee))
        return rec

    def refund_payment(self, order_id):
        rec = _PAYMENTS.get(order_id)
        if not rec or rec.get("status") != "captured":
            return {"ok": False, "reason": "无可退支付"}
        rec["status"] = "refunded"
        _emit("payment_refunded", {"order_id": order_id, "amount": rec.get("amount")})
        return {"ok": True, "refunded": rec.get("amount")}

    def status_of(self, order_id):
        rec = _PAYMENTS.get(order_id) or {}
        return rec.get("status", "none")


# =============================================================================
# 订单仓储：在 store dict 外面再包一层"仓储"，带一点查询/分页/软删。
# =============================================================================

class OrderRepository:
    def __init__(self, store=None):
        self.store = store if store is not None else _ORDERS

    def save(self, order):
        self.store[order["id"]] = order
        return order["id"]

    def get(self, order_id):
        return self.store.get(order_id)

    def delete(self, order_id):
        """软删：打 deleted 标，不真删（怕老报表 join 不到就崩）。"""
        o = self.store.get(order_id)
        if o:
            o["_deleted"] = True
            return True
        return False

    def list(self, include_deleted=False, status=None, region=None):
        out = []
        for o in self.store.values():
            if o.get("_deleted") and not include_deleted:
                continue
            if status is not None and o.get("status") != status:
                continue
            if region is not None and o.get("region") != region:
                continue
            out.append(o)
        return out

    def page(self, page=1, size=10, **filters):
        rows = self.list(**filters)
        start = (page - 1) * size
        return {"page": page, "size": size, "total": len(rows),
                "rows": rows[start:start + size]}

    def total_amount(self, **filters):
        return _round2(sum(o.get("total", 0.0) for o in self.list(**filters)))


# =============================================================================
# CheckoutFacade：把 校验→下单→支付→通知→路由 串成一条的下单门面
# 这种"facade 比底层还多一套逻辑"的反模式，是大型遗留里最难梳理的部分之一。
# =============================================================================

class CheckoutFacade:
    def __init__(self, region="cn", store=None):
        self.region = region
        self.store = store if store is not None else _ORDERS

    def place_order(self, items, user, coupon=None, use_points=0,
                    method="card", province=None):
        # 1) 校验
        v = CartValidator(self.region).validate(items, user, coupon)
        if not v["ok"]:
            return {"ok": False, "stage": "validate", "errors": v["errors"]}
        # 2) 下单（用 god checkout）
        engine = OrderSystem(region=self.region, store=self.store)
        order = engine.checkout(items, user, coupon=coupon, use_points=use_points)
        if order["status"] != "confirmed":
            return {"ok": False, "stage": "checkout", "order": order}
        # 3) 支付
        pay = PaymentSimulator().authorize(order, method=method, user=user)
        if pay.get("status") != "captured":
            # 支付失败
            order["status"] = "payment_failed"
            return {"ok": False, "stage": "payment", "order": order, "payment": pay}
        # 4) 仓储路由
        routing = WarehouseRouter().route(self.region, items)
        order["routing"] = routing
        # 5) 通知（用 Dispatcher 而不是 OrderSystem.notify——两套发送）
        NotificationDispatcher(self.region).dispatch(user, order)
        # 6) 小票
        receipt = ReceiptPrinter(self.region).render(order)
        return {"ok": True, "order": order, "payment": pay,
                "routing": routing, "receipt": receipt}


# 模块对外想暴露的名字（其实很多调用方绕过它直接 import 内部符号——名义公共面而已）
__all__ = [
    "OrderSystem", "dispatch_checkout", "calc_v0", "calc_v1", "calc_v2",
    "checkout_v3_experimental", "CartValidator", "RefundEngine", "InventoryAdmin",
    "ReportBuilder", "LoyaltyManager", "BatchProcessor", "TaxCalculator",
    "ShippingCalculator", "PromoEngine", "RegionalSettlement", "GiftCardService",
    "SubscriptionBilling", "Analytics", "LegacyDBAdapter", "ExtRiskEngine",
    "CouponIssuer", "WarehouseRouter", "PriceQuoteBuilder", "ReceiptPrinter",
    "SettlementReconciler", "DisputeCenter", "BundleCatalog", "TaxFilingReport",
    "OrderStateMachine", "NotificationDispatcher", "ConfigManager",
    "CustomerSegmentation", "PaymentSimulator", "OrderRepository", "CheckoutFacade",
    "MoneyPolicy", "PriceExplainer", "OrderEnricher", "ShippingLabelPrinter",
    "InventoryForecast", "GiftWrapService",
    "reset_state", "seed_demo_data", "demo_full_pipeline", "format_money",
    "country_to_region", "audit_config_consistency",
]

# 模块版本（手填，和实际行为脱节，懒得维护）。重构后建议用真正的版本管理替代。
__version__ = "3.7.2-legacy"
__maintainer__ = "ordering-team"


# =============================================================================
# 金额舍入策略：不同区域历史上用过不同的舍入（四舍五入 / 银行家 / 向上取整）。
# checkout 用 round（银行家）；本类提供按区域的舍入策略。
# =============================================================================

import math as _math

ROUNDING_POLICY = {
    "cn": "half_even", "us": "half_up", "eu": "half_even",
    "uk": "half_up", "jp": "ceil", "au": "half_up", "sg": "half_even",
}


class MoneyPolicy:
    def round(self, amount, region="cn"):
        mode = ROUNDING_POLICY.get(region, "half_even")
        if mode == "half_even":
            return round(amount, 2)          # Python 默认银行家舍入
        if mode == "half_up":
            return _math.floor(amount * 100 + 0.5) / 100.0
        if mode == "ceil":
            # jp 多为 0 位小数，向上取整到整数
            return float(_math.ceil(amount))
        return round(amount, 2)

    def minor_units(self, amount, region="cn"):
        """转成最小货币单位（分/cent）。jp 没有小数位。"""
        dec = LOCALE_FORMATS.get(region, LOCALE_FORMATS["cn"])["decimals"]
        return int(round(amount * (10 ** dec)))

    def compare_to_checkout(self, amount, region):
        """对比本策略与 round() 的结果差。"""
        policy = self.round(amount, region)
        hardcoded = round(amount, 2)
        return {"region": region, "policy": policy, "checkout_round": hardcoded,
                "delta": round(policy - hardcoded, 4)}


# =============================================================================
# 明细解释器：把一笔订单的 breakdown 翻成人话，给客服/对账用。
# 它对字段的解读是"它以为的口径"，未必等于 checkout 实际算法——读它要小心。
# =============================================================================

class PriceExplainer:
    def explain(self, order):
        bd = order.get("breakdown", {})
        lines = ["订单 %s 价格解释:" % order.get("id")]
        if "subtotal_after_cat" in bd:
            lines.append("  品类折后小计: %s" % bd["subtotal_after_cat"])
        if "after_vip" in bd:
            d = bd.get("subtotal_after_cat", 0) - bd.get("after_vip", 0)
            lines.append("  VIP 优惠: -%s（折后 %s）" % (_round2(d), bd["after_vip"]))
        if "after_coupon" in bd:
            d = bd.get("after_vip", 0) - bd.get("after_coupon", 0)
            lines.append("  优惠券: -%s（券后 %s）" % (_round2(d), bd["after_coupon"]))
        if "tax" in bd:
            lines.append("  税: +%s" % bd["tax"])
        if "shipping" in bd:
            lines.append("  运费: +%s" % bd["shipping"])
        if "points_used" in bd and bd["points_used"]:
            lines.append("  积分抵扣: -%s（用了 %s 分）"
                         % (_round2(bd["points_used"] / 100.0), bd["points_used"]))
        lines.append("  合计: %s" % order.get("total"))
        if "points_earned" in bd:
            lines.append("  本单获得积分: %s" % bd["points_earned"])
        if bd.get("risk_score"):
            lines.append("  风控分: %s" % bd["risk_score"])
        return "\n".join(lines)

    def to_dict(self, order):
        bd = order.get("breakdown", {})
        return {
            "order_id": order.get("id"),
            "subtotal": bd.get("subtotal_after_cat"),
            "vip_saved": _round2(bd.get("subtotal_after_cat", 0) - bd.get("after_vip", 0)),
            "coupon_saved": _round2(bd.get("after_vip", 0) - bd.get("after_coupon", 0)),
            "tax": bd.get("tax"),
            "shipping": bd.get("shipping"),
            "total": order.get("total"),
        }


# =============================================================================
# 订单富化：给订单补上展示用字段（币种折算、预计送达、分层标签）。
# =============================================================================

class OrderEnricher:
    def __init__(self, store):
        self.store = store

    def enrich(self, order, user=None):
        region = order.get("region", "cn")
        code, local = convert_currency(order.get("total", 0.0), region)
        order["display_currency"] = code
        order["display_total"] = local
        weight = sum(li.get("qty", 0) * 0.5 for li in order.get("items", []))
        order["eta_days"] = estimate_delivery_days(region, weight)
        if user is not None:
            seg = CustomerSegmentation(self.store).segment(user)
            order["customer_segment"] = seg["segment"]
        return order


# =============================================================================
# 发货单打印 + 库存预测：两个边角子系统，读 order/全局库存，又各写一套口径。
# =============================================================================

class ShippingLabelPrinter:
    def __init__(self, region="cn"):
        self.region = region

    def render(self, order, address=None):
        whs = WarehouseRouter().route(self.region, order.get("items", []))
        w = cart_weight(order.get("items", []))
        eta = estimate_delivery_days(self.region, w)
        lines = []
        lines.append("+" + "-" * 38 + "+")
        lines.append("| 发货单 %-29s|" % order.get("id"))
        lines.append("+" + "-" * 38 + "+")
        lines.append("| 区域: %-31s|" % self.region)
        lines.append("| 重量: %-29s kg|" % round(w, 2))
        lines.append("| 仓库: %-31s|" % str(whs.get("warehouse") or whs.get("plan")))
        lines.append("| 预计: %-29s 天|" % eta)
        if address:
            lines.append("| 地址: %-31s|" % str(address)[:31])
        lines.append("+" + "-" * 38 + "+")
        return "\n".join(lines)

    def batch(self, orders):
        return "\n\n".join(self.render(o) for o in orders)


GIFT_WRAP = {"none": 0, "basic": 5, "premium": 15, "luxury": 30}


class GiftWrapService:
    """礼品包装：在订单外加价并调整 total。"""

    def price(self, style="basic", count=1):
        return _round2(GIFT_WRAP.get(style, 0) * count)

    def apply(self, order, style="basic"):
        n = sum(li.get("qty", 0) for li in order.get("items", []))
        fee = self.price(style, n)
        order["gift_wrap"] = {"style": style, "fee": fee}
        order["total"] = _round2(order.get("total", 0.0) + fee)
        _audit("giftwrap %s +%s on %s" % (style, fee, order.get("id")))
        return order


class InventoryForecast:
    """根据已确认订单的历史消耗，给出"还能撑几单"的粗略预测。读全局 _INVENTORY + store。"""

    def __init__(self, store):
        self.store = store

    def consumption(self):
        used = {}
        for o in self.store.values():
            if o.get("status") != "confirmed":
                continue
            for li in o.get("items", []):
                sku = li.get("sku")
                if sku:
                    used[sku] = used.get(sku, 0) + li.get("qty", 0)
        return used

    def days_of_supply(self, daily_rate=None):
        used = self.consumption()
        out = {}
        for sku, stock in _INVENTORY.items():
            rate = (daily_rate or {}).get(sku) or max(1, used.get(sku, 0))
            out[sku] = round(stock / rate, 1)
        return out

    def reorder_suggestions(self, target_days=14, daily_rate=None):
        dos = self.days_of_supply(daily_rate)
        sugg = []
        for sku, days in dos.items():
            if days < target_days:
                sugg.append({"sku": sku, "days_left": days,
                             "current": _INVENTORY.get(sku, 0)})
        sugg.sort(key=lambda x: x["days_left"])
        return sugg


# -----------------------------------------------------------------------------
# CHANGELOG
# -----------------------------------------------------------------------------
# v0   最初只有 calc()：品类折扣 + VIP + 税。
# v1   加了优惠券，VIP 分到 3 档。
# v1.2 加了区域税率乘子与负数兜底。
# v2   加了积分抵扣。
# v3   TaxCalculator / ShippingCalculator / PromoEngine 组合的结算实现。
# 现在线上跑的是 OrderSystem.checkout。
#
# TODO:
#   - [ ] 把 calc_v0/v1/v2 下线，统一到 checkout
#   - [ ] 把税/运费/风控从 checkout 拆出去
#   - [ ] 合并 RegionalSettlement 的 settle_*
# -----------------------------------------------------------------------------


# =============================================================================
# 一点烟雾自检：直接 `python order_system.py` 能看到几笔订单的结果
# =============================================================================

if __name__ == "__main__":
    reset_state()
    s = OrderSystem(region="cn")
    cart = [
        {"sku": "SKU-FRESH-1", "price": 20, "qty": 3, "cat": "fresh", "weight": 1.0},
        {"sku": "SKU-BOOK-1", "price": 30, "qty": 5, "cat": "book", "weight": 0.4},
    ]
    u = {"id": "u1", "vip": True, "vip_level": 2, "loyalty_points": 500, "new": False}
    o = s.checkout(cart, u, coupon="PCT10", use_points=200)
    print(o["id"], o["status"], o["total"], o["breakdown"])
    print(s.report_last())

    print("\n" + "#" * 60)
    print("# demo_full_pipeline()")
    print("#" * 60)
    print(demo_full_pipeline())

    print("\n" + "#" * 60)
    print("# 各结算实现的结果对比")
    print("#" * 60)
    print(SettlementReconciler().report(cart, u, region="eu", coupon="PCT20"))
