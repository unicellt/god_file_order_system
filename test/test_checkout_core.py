from helpers import IsolatedOrderSystemTestCase, osys


class CheckoutCoreCharacterizationTests(IsolatedOrderSystemTestCase):
    def test_confirmed_order_locks_current_totals_and_side_effects(self):
        user = {"id": "u1", "vip": True, "vip_level": 2, "loyalty_points": 500, "new": False}
        order = osys.OrderSystem(region="cn").checkout(
            self.base_cart(),
            user,
            coupon="PCT10",
            use_points=200,
        )

        self.assertEqual("ORD-1001", order["id"])
        self.assertEqual("confirmed", order["status"])
        self.assertEqual("CNY", order["currency"])
        self.assertEqual(146.77, order["total"])
        self.assertEqual(
            [
                {"sku": "SKU-FRESH-1", "cat": "fresh", "line": 54.0, "qty": 3},
                {"sku": "SKU-BOOK-1", "cat": "book", "line": 120.0, "qty": 5},
            ],
            order["items"],
        )
        self.assertEqual(174.0, order["breakdown"]["subtotal_after_cat"])
        self.assertAlmostEqual(165.29999999999998, order["breakdown"]["after_vip"])
        self.assertAlmostEqual(148.76999999999998, order["breakdown"]["after_coupon"])
        self.assertEqual(200, order["breakdown"]["points_used"])
        self.assertEqual(163, order["breakdown"]["points_earned"])
        self.assertEqual(0, order["breakdown"]["risk_score"])

        self.assertEqual(463, user["loyalty_points"])
        self.assertEqual(97, osys._INVENTORY["SKU-FRESH-1"])
        self.assertEqual(195, osys._INVENTORY["SKU-BOOK-1"])
        self.assertEqual(
            {"ORD-1001": {"SKU-FRESH-1": 3, "SKU-BOOK-1": 5}},
            dict(osys._RESERVATIONS.items()),
        )
        self.assertEqual(order, osys._ORDERS["ORD-1001"])
        self.assertEqual(
            [{"kind": "order_confirmed", "payload": {"order_id": "ORD-1001", "total": 146.77}}],
            list(osys._EVENTS),
        )
        audit = list(osys._AUDIT_LOG)
        self.assertEqual(3, len(audit))
        self.assertIn("reserved {'SKU-FRESH-1': 3, 'SKU-BOOK-1': 5} for ORD-1001", audit)
        self.assertIn("saved order ORD-1001 total=146.77", audit)
        self.assertIn("notify u1: 订单 ORD-1001 已确认，应付 146.77", audit)

    def test_dry_run_still_advances_seq_and_session_status_quo(self):
        seq_before = osys._conn().execute(
            "SELECT val FROM seq WHERE name='order'"
        ).fetchone()[0]

        order = osys.OrderSystem(region="cn").checkout(
            self.base_cart(),
            {"id": "dry", "vip": False, "loyalty_points": 0},
            dry_run=True,
        )

        seq_after = osys._conn().execute(
            "SELECT val FROM seq WHERE name='order'"
        ).fetchone()[0]
        self.assertEqual("confirmed", order["status"])
        self.assertEqual("ORD-1001", order["id"])
        self.assertEqual(seq_before + 1, seq_after)
        self.assertEqual("dry", osys._SESSION["current_user"])
        self.assertEqual(0, len(osys._ORDERS))
        self.assertEqual(0, len(osys._EVENTS))
        self.assertEqual(0, len(osys._AUDIT_LOG))
        self.assertEqual(100, osys._INVENTORY["SKU-FRESH-1"])
