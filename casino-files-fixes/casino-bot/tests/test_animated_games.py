import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main


class _FakeDiceBot:
    def __init__(self, token, *, error=None):
        self.token = token
        self.error = error
        self.calls = []

    async def send_dice(self, *, chat_id, emoji):
        self.calls.append((chat_id, emoji))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(dice=SimpleNamespace(value=4))


class AnimatedGameReliabilityTests(unittest.TestCase):
    def test_dealer_failure_does_not_create_duplicate_fallback_roll(self):
        original_dealers = dict(main.dealer_bots)
        failing_dealer = _FakeDiceBot("dealer-1", error=RuntimeError("network error"))
        fallback_dealer = _FakeDiceBot("dealer-2")
        main_bot = _FakeDiceBot("main")
        main.dealer_bots.clear()
        main.dealer_bots.update(
            {"dealer_1": failing_dealer, "dealer_2": fallback_dealer}
        )

        try:
            result = asyncio.run(
                main.dealer_send_dice(
                    chat_id=123,
                    emoji="🎲",
                    main_bot=main_bot,
                    chat_type="group",
                    dealer_name="dealer_1",
                )
            )
        finally:
            main.dealer_bots.clear()
            main.dealer_bots.update(original_dealers)

        self.assertIsNone(result)
        self.assertEqual(len(failing_dealer.calls), 1)
        self.assertEqual(
            fallback_dealer.calls,
            [],
            "a failed request must not be replayed through another bot",
        )

    def test_visible_dice_serializes_same_chat_but_not_other_chats(self):
        async def scenario():
            original_interval = main._DICE_CHAT_MIN_INTERVAL_SECONDS
            main._DICE_CHAT_MIN_INTERVAL_SECONDS = 0
            main._dice_chat_locks.clear()
            main._dice_chat_last_send.clear()
            active_by_chat = {}
            active_total = 0
            max_by_chat = {}
            max_total = 0

            async def fake_send_dice(*, chat_id, **kwargs):
                nonlocal active_total, max_total
                active_by_chat[chat_id] = active_by_chat.get(chat_id, 0) + 1
                active_total += 1
                max_by_chat[chat_id] = max(
                    max_by_chat.get(chat_id, 0), active_by_chat[chat_id]
                )
                max_total = max(max_total, active_total)
                await asyncio.sleep(0.01)
                active_by_chat[chat_id] -= 1
                active_total -= 1
                return SimpleNamespace(dice=SimpleNamespace(value=4))

            try:
                with patch.object(
                    main, "dealer_send_dice", side_effect=fake_send_dice
                ):
                    await asyncio.gather(
                        main._send_visible_dice(
                            chat_id=1,
                            emoji="🎲",
                            main_bot=None,
                            chat_type="group",
                        ),
                        main._send_visible_dice(
                            chat_id=1,
                            emoji="🎲",
                            main_bot=None,
                            chat_type="group",
                        ),
                        main._send_visible_dice(
                            chat_id=2,
                            emoji="🎲",
                            main_bot=None,
                            chat_type="group",
                        ),
                    )
                return max_by_chat, max_total
            finally:
                main._DICE_CHAT_MIN_INTERVAL_SECONDS = original_interval
                main._dice_chat_locks.clear()
                main._dice_chat_last_send.clear()

        max_by_chat, max_total = asyncio.run(scenario())
        self.assertEqual(max_by_chat[1], 1)
        self.assertEqual(max_by_chat[2], 1)
        self.assertGreaterEqual(
            max_total,
            2,
            "different chats should be able to animate concurrently",
        )

    def test_rapid_dice_updates_wait_for_the_same_game_lock(self):
        async def scenario():
            user_id = "animated-user"
            original_game = main.active_games.get(user_id)
            main.active_games[user_id] = {"type": "dice"}
            main._animated_roll_locks.clear()
            running = 0
            max_running = 0
            calls = 0

            async def fake_locked(update, context):
                nonlocal calls, running, max_running
                calls += 1
                running += 1
                max_running = max(max_running, running)
                await asyncio.sleep(0.01)
                running -= 1

            message = SimpleNamespace(
                chat_id=1,
                from_user=SimpleNamespace(id=user_id),
                dice=SimpleNamespace(value=4),
            )
            update = SimpleNamespace(message=message)
            try:
                with patch.object(
                    main, "_handle_dice_message_locked", side_effect=fake_locked
                ):
                    await asyncio.gather(
                        main.handle_dice_message(update, None),
                        main.handle_dice_message(update, None),
                    )
                return calls, max_running
            finally:
                main._animated_roll_locks.clear()
                if original_game is None:
                    main.active_games.pop(user_id, None)
                else:
                    main.active_games[user_id] = original_game

        calls, max_running = asyncio.run(scenario())
        self.assertEqual(calls, 2)
        self.assertEqual(
            max_running,
            1,
            "rapid rolls must queue instead of racing shared game state",
        )


if __name__ == "__main__":
    unittest.main()