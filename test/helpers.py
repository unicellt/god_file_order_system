import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import order_system as osys  # noqa: E402


class IsolatedOrderSystemTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._feature_flags = copy.deepcopy(osys.FEATURE_FLAGS)
        self._tax_table = copy.deepcopy(osys.TAX_TABLE)
        self._coupon_catalog = copy.deepcopy(osys.COUPON_CATALOG)

        osys.configure_db(os.path.join(self.tmp.name, "order_system_test.db"))
        osys.reset_state()
        osys.FEATURE_FLAGS.update({
            "use_legacy_v1": False,
            "enable_loyalty": True,
            "enable_risk": True,
            "enable_notify": True,
            "round_eu_per_item": True,
        })

    def tearDown(self):
        osys.FEATURE_FLAGS.clear()
        osys.FEATURE_FLAGS.update(self._feature_flags)
        osys.TAX_TABLE.clear()
        osys.TAX_TABLE.update(self._tax_table)
        osys.COUPON_CATALOG.clear()
        osys.COUPON_CATALOG.update(self._coupon_catalog)
        if osys._db_state["conn"] is not None:
            osys._db_state["conn"].close()
            osys._db_state["conn"] = None
        self.tmp.cleanup()

    def base_cart(self):
        return [
            {"sku": "SKU-FRESH-1", "price": 20, "qty": 3, "cat": "fresh", "weight": 1.0},
            {"sku": "SKU-BOOK-1", "price": 30, "qty": 5, "cat": "book", "weight": 0.4},
        ]
