import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from async_github_repo_summary import AsyncGitHubRepoSummary


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(
            AsyncGitHubRepoSummary, "GITHUB_API_TOKENS", ["test"]
        ):
            self.summary = AsyncGitHubRepoSummary()
        self.now = datetime(2026, 10, 21, 5, 22, 23, tzinfo=timezone.utc)
        self.timestamp = self.now.timestamp()
        self.summary.expo_wait_last_update = self.timestamp - 11
        self.clock = patch("async_github_repo_summary.datetime")
        self.mock_datetime = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.mock_datetime.now.return_value = self.now
        self.sleeper = patch(
            "async_github_repo_summary.asyncio.sleep", new_callable=AsyncMock
        )
        self.sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)

    def response(self, status=403, **headers):
        response = MagicMock()
        response.status = status
        response.headers = headers
        return response

    async def test_concurrent_workers_only_double_backoff_once(self):
        response = self.response(
            **{
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(self.timestamp + 772)),
            }
        )
        await asyncio.gather(
            *(self.summary.check_api_rate(response) for _ in range(64))
        )
        self.assertEqual(self.summary.expo_wait_time, 2)
        self.assertEqual(self.summary.expo_wait_last_update, self.timestamp)
        self.assertEqual(self.sleep.await_count, 64)
        for call in self.sleep.await_args_list:
            self.assertEqual(call.args, (774,))

    async def test_backoff_doubles_again_after_interval(self):
        response = self.response(**{"retry-after": "30"})
        await self.summary.check_api_rate(response)
        self.mock_datetime.now.return_value = datetime.fromtimestamp(
            self.timestamp + 11, timezone.utc
        )
        await self.summary.check_api_rate(response)
        self.assertEqual(self.summary.expo_wait_time, 4)
        self.sleep.assert_awaited_with(34)

    async def test_backoff_never_exceeds_cap(self):
        self.summary.expo_wait_time = 2048
        response = self.response(status=429, **{"retry-after": "30"})
        await self.summary.check_api_rate(response)
        self.assertEqual(self.summary.expo_wait_time, 3600)
        self.sleep.assert_awaited_once_with(3630)

    async def test_recent_backoff_is_not_doubled(self):
        self.summary.expo_wait_last_update = self.timestamp - 5
        response = self.response(status=429, **{"retry-after": "30"})
        await self.summary.check_api_rate(response)
        self.sleep.assert_awaited_once_with(31)
        self.assertEqual(self.summary.expo_wait_time, 1)

    async def test_expired_reset_only_waits_for_backoff(self):
        response = self.response(
            **{
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(self.timestamp - 60)),
            }
        )
        await self.summary.check_api_rate(response)
        self.sleep.assert_awaited_once_with(2)

    async def test_non_rate_limit_forbidden_still_raises(self):
        response = self.response(**{"x-ratelimit-remaining": "10"})
        with self.assertRaisesRegex(ValueError, "Invalid 403 response"):
            await self.summary.check_api_rate(response)
        self.sleep.assert_not_awaited()

    async def test_success_resets_backoff_and_timestamp(self):
        self.summary.expo_wait_time = 2048
        self.summary.github_api_sem = asyncio.Semaphore(1)
        response = self.response(status=200)
        response.json = AsyncMock(return_value={"id": 42})
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch(
            "async_github_repo_summary.aiohttp.ClientSession"
        ) as session_class, patch(
            "async_github_repo_summary.aiohttp.TCPConnector"
        ):
            session_class.return_value.__aenter__ = AsyncMock(
                return_value=session
            )
            result = await self.summary.send_github_api_request(
                "https://api.github.com/user/42"
            )
        self.assertEqual(result, {"id": 42})
        self.assertEqual(self.summary.expo_wait_time, 1)
        self.assertEqual(self.summary.expo_wait_last_update, self.timestamp)
        self.sleep.assert_not_awaited()
