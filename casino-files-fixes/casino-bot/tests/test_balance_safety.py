import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import main


class BalanceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.user_balances = dict(main.user_balances)
        self.crypto_house_balances = dict(main.crypto_house_balances)
        self.house_balance = main.house_balance
        self.casino_balance_usd = main.casino_balance_usd
        self.use_sandbox = main.USE_SANDBOX
        main.user_balances.clear()
        main.crypto_house_balances.clear()
        main.crypto_house_balances.update({"USDT": 100.0, "BTC": 1.0})
        main.house_balance = 100.0
        main.casino_balance_usd = 100.0

    def tearDown(self):
        main.user_balances.clear()
        main.user_balances.update(self.user_balances)
        main.crypto_house_balances.clear()
        main.crypto_house_balances.update(self.crypto_house_balances)
        main.house_balance = self.house_balance
        main.casino_balance_usd = self.casino_balance_usd
        main.USE_SANDBOX = self.use_sandbox

    def _quiet_balance_side_effects(self):
        return patch.multiple(
            main,
            save_data_critical=lambda: None,
            _flush_balances_backup=lambda: None,
            log_transaction=lambda *args, **kwargs: None,
        )

    def test_crypto_house_debit_rejects_overdraft_and_accepts_valid_reservation(self):
        with self._quiet_balance_side_effects():
            self.assertFalse(main.deduct_crypto_house_balance("BTC", 1.1))
            self.assertEqual(main.get_crypto_house_balance("BTC"), 1.0)
            self.assertTrue(main.deduct_crypto_house_balance("BTC", 0.25))
            self.assertAlmostEqual(main.get_crypto_house_balance("BTC"), 0.75)

    def test_usd_house_debit_rejects_overdraft(self):
        with self._quiet_balance_side_effects():
            self.assertFalse(main.deduct_house_balance(100.01))
            self.assertEqual(main.house_balance, 100.0)
            self.assertTrue(main.deduct_house_balance(25.0))
            self.assertEqual(main.house_balance, 75.0)

    def test_set_user_balance_rejects_negative_values(self):
        with self.assertRaises(ValueError):
            main.set_user_balance("negative-user", -1.0)
        self.assertNotIn("negative-user", main.user_balances)

    def test_concurrent_balance_debits_do_not_overdraw_or_lose_updates(self):
        main.user_balances["race-user"] = 100.0
        with self._quiet_balance_side_effects(), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _unused: main.ultra_secure_deduct_user_balance(
                    "race-user", 1.0, "concurrency_test"
                ),
                range(100),
            ))

        self.assertEqual(sum(bool(result) for result in results), 100)
        self.assertEqual(main.user_balances["race-user"], 0.0)

    def test_successful_crypto_withdrawal_debits_user_and_house_once(self):
        main.user_balances["123456"] = 10.0
        main.USE_SANDBOX = True

        with self._quiet_balance_side_effects(), patch.object(
            main, "can_withdraw", return_value=(True, "")
        ), patch.object(main, "convert_usd_to_crypto", return_value=0.25):
            result = main.create_crypto_withdrawal(
                "123456", "BTC", 5.0, "bc1qvalidwithdrawaladdress"
            )

        self.assertTrue(result["success"])
        self.assertEqual(main.user_balances["123456"], 5.0)
        self.assertAlmostEqual(main.get_crypto_house_balance("BTC"), 0.75)

    def test_failed_crypto_payout_refunds_user_and_house_reservations(self):
        main.user_balances["123456"] = 10.0
        main.USE_SANDBOX = False

        with self._quiet_balance_side_effects(), patch.object(
            main, "can_withdraw", return_value=(True, "")
        ), patch.object(main, "convert_usd_to_crypto", return_value=0.25), patch.object(
            main.crypto_api,
            "create_payout",
            return_value=(None, "provider rejected payout"),
        ):
            result = main.create_crypto_withdrawal(
                "123456", "BTC", 5.0, "bc1qvalidwithdrawaladdress"
            )

        self.assertFalse(result["success"])
        self.assertEqual(main.user_balances["123456"], 10.0)
        self.assertAlmostEqual(main.get_crypto_house_balance("BTC"), 1.0)


if __name__ == "__main__":
    unittest.main()