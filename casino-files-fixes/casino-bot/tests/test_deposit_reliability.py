import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from blockchain_deposit_monitor import (
    AddressScan,
    ChainTransaction,
    _normalise_scan_asset,
    _scan_ton,
)


class DepositReliabilityTests(unittest.TestCase):
    """Chain-authoritative deposit tests with no live credentials or explorers."""

    def setUp(self):
        self.pending = dict(main.nowpayments_pending_deposits)
        self.processed = set(main.processed_payment_ids)
        self.txids = set(main.processed_deposit_txids)
        main.nowpayments_pending_deposits.clear()
        main.processed_payment_ids.clear()
        main.processed_deposit_txids.clear()

    def tearDown(self):
        main.nowpayments_pending_deposits.clear()
        main.nowpayments_pending_deposits.update(self.pending)
        main.processed_payment_ids.clear()
        main.processed_payment_ids.update(self.processed)
        main.processed_deposit_txids.clear()
        main.processed_deposit_txids.update(self.txids)

    @staticmethod
    def _pending(payment_id="p-1", address="TEXACT", currency="usdttrc20"):
        return {
            "user_id": "123456",
            "crypto": currency,
            "pay_currency": currency,
            "network": "TRX",
            "order_id": "dep_123456_1",
            "payment_id": payment_id,
            "pay_address": address,
            "amount_usd": 10.0,
            "expected_coin_amount": 10.0,
            "created_at": time.time() - 5,
            "expires_at": time.time() + 3600,
            "status": "waiting",
            "state": "WAITING_FOR_PAYMENT",
        }

    @staticmethod
    def _provider(
        payment_id="p-1",
        address="TEXACT",
        currency="usdttrc20",
        network="TRX",
        usd=10.0,
        status="finished",
    ):
        return {
            "payment_id": payment_id,
            "order_id": "dep_123456_1",
            "payment_status": status,
            "pay_address": address,
            "pay_currency": currency,
            "network": network,
            "actually_paid": 1.0,
            "pay_amount": 10.0,
            "outcome": {"amount_received_usd": usd, "txid": "tx-1"},
            "payin_hash": "tx-1",
        }

    def test_received_amount_uses_actual_under_and_overpayment(self):
        under = main._nowpayments_received_amount(
            self._provider(usd=4.25), self._pending()
        )
        over = main._nowpayments_received_amount(
            self._provider(usd=25.75), self._pending()
        )
        self.assertEqual(under[0], 4.25)
        self.assertEqual(over[0], 25.75)

        no_amount = dict(self._provider(usd=0))
        no_amount["actually_paid"] = 0
        no_amount["outcome"] = {"txid": "tx-1"}
        self.assertEqual(
            main._nowpayments_received_amount(no_amount, self._pending())[0], 0.0
        )
        self.assertFalse(main._nowpayments_has_received_transaction(no_amount))

    def test_legacy_coin_selection_registers_complete_tracking_record(self):
        class FakeMessage:
            chat_id = 123456

            async def reply_photo(self, **kwargs):
                return None

            async def delete(self):
                return None

        class FakeQuery:
            from_user = SimpleNamespace(id=123456, username="player")
            message = FakeMessage()

            async def answer(self):
                return None

        payment = {
            "payment_id": "legacy-p-1",
            "pay_address": "bc1qexact",
            "pay_amount": 0.0001,
            "price_amount": 1.0,
        }
        with patch.object(
            main, "nowpayments_create_payment", return_value=(payment, None)
        ), patch.object(main, "save_data_critical"):
            asyncio.run(
                main.handle_crypto_deposit_selection(
                    FakeQuery(), SimpleNamespace(), "BTC"
                )
            )

        tracked = main.nowpayments_pending_deposits["legacy-p-1"]
        self.assertEqual(tracked["user_id"], "123456")
        self.assertEqual(tracked["pay_currency"], "btc")
        self.assertEqual(tracked["network"], "BTC")
        self.assertEqual(tracked["expected_coin_amount"], 0.0001)
        self.assertGreater(tracked["expires_at"], tracked["created_at"])
        self.assertEqual(tracked["state"], "WAITING_FOR_PAYMENT")

    def test_exact_poll_credits_confirmed_payment_once_and_persists_state(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        calls = []
        chain_tx = ChainTransaction(
            txid="chain-tx-1",
            address="TEXACT",
            amount=12.0,
            coin="USDT",
            network="TRX",
            confirmations=20,
            confirmed=True,
        )

        with patch.object(main, "scan_deposit_address", return_value=AddressScan((chain_tx,))), \
             patch.object(main, "_process_confirmed_deposit", side_effect=lambda **kwargs: calls.append(kwargs) or True), \
             patch.object(main, "_tg_send_deposit_processing_notification", return_value=True), \
             patch.object(main, "save_data_critical"):
            first = asyncio.run(main._run_nowpayments_monitor_once())
            main.processed_payment_ids.add("p-1")
            second = asyncio.run(main._run_nowpayments_monitor_once())

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["user_id"], "123456")
        self.assertEqual(calls[0]["usd_amount"], 12.0)
        self.assertEqual(main.nowpayments_pending_deposits["p-1"]["state"], "CREDITED")

    def test_poll_timeout_leaves_payment_retryable(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        with patch.object(
            main, "scan_deposit_address", return_value=AddressScan(error="timeout")
        ), patch.object(main, "_process_confirmed_deposit") as processor, patch.object(
            main, "save_data_critical"
        ):
            self.assertEqual(asyncio.run(main._run_nowpayments_monitor_once()), 0)
        processor.assert_not_called()
        self.assertEqual(main.nowpayments_pending_deposits["p-1"]["status"], "waiting")

    def test_poll_rejects_address_and_network_mismatches(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        wrong_address = ChainTransaction(
            txid="wrong-address",
            address="TWRONG",
            amount=1.0,
            coin="USDT",
            network="TRX",
            confirmations=20,
            confirmed=True,
        )
        with patch.object(main, "scan_deposit_address", return_value=AddressScan((wrong_address,))), \
             patch.object(main, "_process_confirmed_deposit") as processor, \
             patch.object(main, "save_data_critical"):
            asyncio.run(main._run_nowpayments_monitor_once())
        processor.assert_not_called()
        self.assertEqual(
            main.nowpayments_pending_deposits["p-1"]["status"], "address_mismatch"
        )

        main.nowpayments_pending_deposits["p-1"] = self._pending()
        with patch.object(
            main,
            "scan_deposit_address",
            return_value=AddressScan(error="TRX explorer unavailable"),
        ), patch.object(
            main, "nowpayments_get_payment_status",
            side_effect=AssertionError("provider status must never be used"),
        ), \
             patch.object(main, "_process_confirmed_deposit") as processor, \
             patch.object(main, "save_data_critical"):
            asyncio.run(main._run_nowpayments_monitor_once())
        processor.assert_not_called()
        self.assertEqual(main.nowpayments_pending_deposits["p-1"]["status"], "waiting")
        self.assertIn("chain_scan_failed", main.nowpayments_pending_deposits["p-1"]["last_error"])

    def test_nowpayments_ipn_never_settles_chain_deposit(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        payload = {
            "payment_id": "p-1",
            "order_id": "dep_123456_1",
            "payment_status": "finished",
        }
        calls = []
        with patch.object(
            main, "_process_confirmed_deposit",
            side_effect=lambda **kwargs: calls.append(kwargs) or True,
        ), patch.object(main, "nowpayments_get_payment_status") as provider:
            response = main.app.test_client().post(
                "/nowpayments_callback",
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ignored_chain_monitor_authoritative")
        self.assertEqual(calls, [])
        provider.assert_not_called()

    def test_legacy_order_key_is_migrated_to_payment_key(self):
        pending = self._pending()
        pending.pop("payment_id")
        main.nowpayments_pending_deposits[pending["order_id"]] = pending

        found = main._find_pending_nowpayments_deposit(
            "p-1", pending["order_id"]
        )

        self.assertIs(found, pending)
        self.assertNotIn(pending["order_id"], main.nowpayments_pending_deposits)
        self.assertIs(main.nowpayments_pending_deposits["p-1"], pending)
        self.assertEqual(pending["payment_id"], "p-1")

    def test_confirmation_notification_retries_without_recrediting(self):
        pending = self._pending()
        pending.update(
            {
                "status": "credited",
                "confirmation_notification": {
                    "usd_amount": 12.0,
                    "fee_amount": 0.0,
                    "pay_currency": "USDTTRC20",
                    "coin_amount": 1.0,
                    "txid": "tx-1",
                },
            }
        )
        main.nowpayments_pending_deposits["p-1"] = pending

        with patch.object(
            main, "_tg_send_deposit_notification", return_value=True
        ) as send_confirmation, patch.object(
            main, "_process_confirmed_deposit"
        ) as processor:
            self.assertTrue(main._retry_pending_deposit_notifications(pending))
            self.assertFalse(main._retry_pending_deposit_notifications(pending))

        send_confirmation.assert_called_once()
        processor.assert_not_called()
        self.assertTrue(pending.get("confirmation_notified_at"))

    def test_chain_scan_is_preferred_when_provider_status_is_unavailable(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending(
            address="TEXACT" * 7
        )
        chain_tx = ChainTransaction(
            txid="chain-tx-1",
            address="TEXACT" * 7,
            amount=1.0,
            coin="USDT",
            network="TRX",
            confirmations=20,
            confirmed=True,
        )
        calls = []
        with patch.object(
            main,
            "scan_deposit_address",
            return_value=AddressScan((chain_tx,)),
        ), patch.object(
            main,
            "nowpayments_get_payment_status",
            side_effect=AssertionError("provider status should not be required"),
        ), patch.object(
            main,
            "_process_confirmed_deposit",
            side_effect=lambda **kwargs: calls.append(kwargs) or True,
        ), patch.object(
            main, "_tg_send_deposit_processing_notification", return_value=True
        ), patch.object(
            main, "_tg_send_deposit_notification", return_value=True
        ), patch.object(
            main, "save_data_critical"
        ):
            self.assertEqual(asyncio.run(main._run_nowpayments_monitor_once()), 1)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["txid"], "chain-tx-1")
        self.assertEqual(calls[0]["pay_currency"], "USDTTRC20")
        self.assertEqual(main.nowpayments_pending_deposits["p-1"]["state"], "CREDITED")

    def test_processing_precedes_credit_and_confirmation(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending(
            address="TEXACT" * 7
        )
        events = []
        with patch.object(
            main,
            "_tg_send_deposit_processing_notification",
            side_effect=lambda *args, **kwargs: events.append("processing") or True,
        ), patch.object(
            main,
            "ultra_secure_add_user_balance",
            side_effect=lambda *args, **kwargs: events.append("credit"),
        ), patch.object(
            main,
            "_tg_send_deposit_notification",
            side_effect=lambda *args, **kwargs: events.append("confirmed") or True,
        ), patch.object(
            main, "_persist_payment_id"
        ), patch.object(
            main, "_persist_deposit_txid"
        ), patch.object(
            main, "save_data_critical"
        ), patch.object(
            main, "add_house_balance"
        ), patch.object(
            main, "add_deposit_wagering_requirement"
        ), patch.object(
            main, "_ensure_deposit_consistency"
        ), patch.object(
            main, "_EVENTS_OK", False
        ), patch.object(
            main, "player_data", {"123456": {}}
        ), patch.object(
            main, "user_profiles", {"123456": {"username": "player"}}
        ), patch.object(
            main, "user_deposit_totals", {}
        ), patch.object(
            main, "user_deposit_history", {}
        ), patch.object(
            main, "user_last_deposit_ts", {}
        ), patch.object(
            main, "user_tip_received", {}
        ), patch.object(
            main, "user_pending_bonus_claims", {}
        ), patch.object(
            main, "user_wagering_requirements", {}
        ):
            self.assertTrue(
                main._process_confirmed_deposit(
                    user_id="123456",
                    usd_amount=12.50,
                    credited_amount=12.50,
                    fee_amount=0.0,
                    pay_currency="USDTTRC20",
                    payment_id="p-1",
                    source="chain_monitor",
                    coin_amount=12.50,
                    txid="tx-1",
                )
            )

        self.assertEqual(events, ["processing", "credit", "confirmed"])
        self.assertIn("p-1", main.processed_payment_ids)
        self.assertIn("tx-1", main.processed_deposit_txids)

    def test_processing_and_confirmation_render_actual_amount_emoji_and_explorer_link(self):
        class FakeResponse:
            status_code = 200
            text = ""

        posted = []

        def fake_post(_url, json=None, **_kwargs):
            posted.append(json or {})
            return FakeResponse()

        with patch.dict(main.os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False), \
             patch.object(main.requests, "post", side_effect=fake_post), \
             patch.object(main, "get_user_balance", return_value=19.50), \
             patch.object(main, "user_profiles", {"123456": {"username": "player"}}), \
             patch.object(main, "CASINO_GROUP_CHAT_ID", None):
            self.assertTrue(
                main._tg_send_deposit_processing_notification(
                    "123456",
                    "gram",
                    coin_amount=0.3572,
                    txid="gram-tx-123",
                    network="TON",
                )
            )
            self.assertTrue(
                main._tg_send_deposit_notification(
                    "123456",
                    20.0,
                    "gram",
                    coin_amount=0.3572,
                    fee_amount=0.50,
                    txid="gram-tx-123",
                    network="TON",
                )
            )

        processing_text = posted[0]["text"]
        confirmation_text = posted[1]["text"]
        self.assertIn("0.3572 GRAM", processing_text)
        self.assertIn('emoji-id="6235568867637207626"', processing_text)
        self.assertEqual(
            processing_text,
            '<tg-emoji emoji-id="6235568867637207626">🌀</tg-emoji> '
            '<b>Processing payment Of 0.3572 GRAM</b>',
        )
        self.assertIn("$20.00", confirmation_text)
        self.assertIn("$19.50", confirmation_text)
        self.assertNotIn("$19.00", confirmation_text)
        self.assertIn('emoji-id="6305056190036451310"', confirmation_text)
        self.assertIn('emoji-id="6235568867637207626"', confirmation_text)
        self.assertIn("https://tonviewer.com/transaction/gram-tx-123", confirmation_text)

        posted.clear()
        with patch.dict(main.os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False), \
             patch.object(main.requests, "post", side_effect=fake_post), \
             patch.object(main, "CASINO_GROUP_CHAT_ID", None):
            self.assertTrue(
                main._tg_send_deposit_processing_notification(
                    "123456",
                    "ton",
                    coin_amount=0.3572,
                    txid="ton-tx-123",
                    network="TON",
                )
            )

        self.assertIn("0.3572 GRAM", posted[0]["text"])
        self.assertIn('emoji-id="6235568867637207626"', posted[0]["text"])

    def test_confirmed_processor_passes_gross_usd_to_confirmation_renderer(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        with patch.object(main, "_tg_send_deposit_processing_notification", return_value=True), \
             patch.object(main, "_tg_send_deposit_notification", return_value=True) as send_confirmation, \
             patch.object(main, "ultra_secure_add_user_balance"), \
             patch.object(main, "_persist_payment_id"), \
             patch.object(main, "_persist_deposit_txid"), \
             patch.object(main, "save_data_critical"), \
             patch.object(main, "add_house_balance"), \
             patch.object(main, "add_deposit_wagering_requirement"), \
             patch.object(main, "_ensure_deposit_consistency"), \
             patch.object(main, "_EVENTS_OK", False), \
             patch.object(main, "player_data", {"123456": {}}), \
             patch.object(main, "user_profiles", {"123456": {}}), \
             patch.object(main, "user_deposit_totals", {}), \
             patch.object(main, "user_deposit_history", {}), \
             patch.object(main, "user_last_deposit_ts", {}), \
             patch.object(main, "user_tip_received", {}), \
             patch.object(main, "user_pending_bonus_claims", {}), \
             patch.object(main, "user_wagering_requirements", {}):
            self.assertTrue(
                main._process_confirmed_deposit(
                    user_id="123456",
                    usd_amount=20.0,
                    credited_amount=19.5,
                    fee_amount=0.5,
                    pay_currency="GRAM",
                    payment_id="p-1",
                    source="chain_monitor",
                    coin_amount=0.3572,
                    txid="gram-tx-123",
                )
            )

        self.assertEqual(send_confirmation.call_args.args[1], 20.0)
        self.assertEqual(send_confirmation.call_args.kwargs["fee_amount"], 0.5)

    def test_telegram_gift_confirmation_uses_premium_stars_layout(self):
        class FakeResponse:
            status_code = 200
            text = ""

        posted = []

        def fake_post(_url, json=None, **_kwargs):
            posted.append(json or {})
            return FakeResponse()

        with patch.dict(main.os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False), \
             patch.object(main.requests, "post", side_effect=fake_post), \
             patch.object(main, "user_profiles", {"123456": {"username": "player4555"}}), \
             patch.object(main, "CASINO_GROUP_CHAT_ID", -100123):
            self.assertTrue(
                main._tg_send_deposit_notification(
                    "123456",
                    0.60,
                    "TELEGRAM_GIFT",
                    coin_amount=50,
                    fee_amount=0.0,
                )
            )

        self.assertEqual(len(posted), 2)
        for payload in posted:
            text = payload["text"]
            self.assertIn("<b>Deposit confirmed</b>", text)
            self.assertIn('emoji-id="6303251320224622462"', text)
            self.assertIn('emoji-id="6305280073796689362"', text)
            self.assertIn("Stars received:</b> <b>50</b>", text)
            self.assertIn("USDT value:</b> <b>$0.60</b>", text)
            self.assertIn("Credited:</b> <b>$0.60</b>", text)
            self.assertIn(">player4555</a>", text)
            self.assertNotIn("@player4555", text)
            self.assertIn("Good luck at Rollers Casino!", text)
            self.assertNotIn("Txid-", text)

    def test_telegram_gift_confirmation_uses_name_when_username_is_missing(self):
        class FakeResponse:
            status_code = 200
            text = ""

        posted = []

        def fake_post(_url, json=None, **_kwargs):
            posted.append(json or {})
            return FakeResponse()

        with patch.dict(main.os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False), \
             patch.object(main.requests, "post", side_effect=fake_post), \
             patch.object(
                 main,
                 "user_profiles",
                 {"123456": {"username": "", "first_name": "Player", "last_name": "4555"}},
             ), \
             patch.object(main, "CASINO_GROUP_CHAT_ID", None):
            self.assertTrue(
                main._tg_send_deposit_notification(
                    "123456",
                    0.18,
                    "TELEGRAM_GIFT",
                    coin_amount=15,
                    fee_amount=0.0,
                )
            )

        self.assertEqual(len(posted), 2)
        for payload in posted:
            self.assertIn(">Player 4555</a>", payload["text"])
            self.assertNotIn("@Player 4555", payload["text"])

    def test_nowpayments_network_currency_alias_is_normalized_for_chain_scan(self):
        self.assertEqual(
            _normalise_scan_asset(
                {"pay_currency": "usdttrc20", "network": "TRX"}
            ),
            ("USDT", "TRX"),
        )
        self.assertEqual(
            _normalise_scan_asset(
                {"pay_currency": "avaxc", "network": "CCHAIN"}
            ),
            ("AVAX", "AVAX"),
        )
        self.assertAlmostEqual(main._resolve_deposit_rate("gram"), main.CURRENCY_RATES["TON"])

    def test_native_gram_uses_ton_account_transactions_and_is_creditable(self):
        address = "EQNativeTonDepositAddress123456789"
        transaction = {
            "transaction_id": {"hash": "gram-tx-123"},
            "account": address,
            "in_msg": {
                "source": "EQSenderAddress123456789",
                "destination": address,
                "value": "357200000",
            },
            "description": {
                "aborted": False,
                "compute_phase": {"exit_code": 0},
                "action_phase": {"result_code": 0},
            },
        }
        with patch(
            "blockchain_deposit_monitor._get_json",
            return_value={"result": [transaction]},
        ):
            scan = _scan_ton(
                {
                    "pay_address": address,
                    "pay_currency": "gram",
                    "network": "TON",
                    "required_confirmations": 2,
                },
                "GRAM",
            )

        self.assertEqual(scan.error, "")
        self.assertEqual(len(scan.transactions), 1)
        self.assertEqual(scan.transactions[0].coin, "GRAM")
        self.assertAlmostEqual(scan.transactions[0].amount, 0.3572)
        self.assertTrue(scan.transactions[0].confirmed)

    def test_ipn_processor_is_not_called_for_chain_deposits(self):
        main.nowpayments_pending_deposits["p-1"] = self._pending()
        payload = {
            "payment_id": "p-1",
            "order_id": "dep_123456_1",
            "payment_status": "finished",
        }
        with patch.object(main, "_process_confirmed_deposit") as processor:
            response = main.app.test_client().post(
                "/nowpayments_callback",
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        processor.assert_not_called()


if __name__ == "__main__":
    unittest.main()