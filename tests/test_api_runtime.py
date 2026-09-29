"""真实 ASGI 并发回归：耗时生产入口不能饿死页面状态查询。"""

import asyncio
import threading
import unittest

import httpx

from api.main import create_app
from tests.test_api import StubHttpService


class ApiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_research_does_not_block_health_or_status(self):
        """研究停在受控屏障时，独立请求仍须返回；用屏障而不是延时猜测执行顺序。"""
        entered, release = threading.Event(), threading.Event()

        class Service(StubHttpService):
            def start_research(self, workspace_id, event_id, *, retry):
                entered.set()
                release.wait(3)
                return super().start_research(workspace_id, event_id, retry=retry)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Service())), base_url="http://test") as client:
            request = asyncio.create_task(client.post("/api/workspaces/default/events/evt/research", json={"retry": False}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                status = await asyncio.wait_for(client.get("/api/workspaces/default/events/evt/research"), 0.8)
                self.assertEqual(status.status_code, 200)
                self.assertFalse(request.done(), "查询完成时研究仍应停在屏障，证明没有阻塞主循环")
            finally:
                release.set()
                await request

    async def test_scan_stays_queryable_and_rejects_duplicate_workspace_scan(self):
        entered, release = threading.Event(), threading.Event()

        class Service(StubHttpService):
            async def scan(self, workspace_id):
                entered.set()
                release.wait(3)
                return await super().scan(workspace_id)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Service())), base_url="http://test") as client:
            request = asyncio.create_task(client.post("/api/workspaces/default/scan"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                duplicate = await asyncio.wait_for(client.post("/api/workspaces/default/scan"), 0.8)
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual((await client.get("/api/health")).status_code, 200)
                self.assertFalse(request.done())
            finally:
                release.set()
                await request
