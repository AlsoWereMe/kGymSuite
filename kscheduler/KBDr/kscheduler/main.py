# main.py
import asyncio, os, time, uvicorn
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
from .scheduler_server import SchedulerServer
from fastapi.middleware.cors import CORSMiddleware
from .config import SchedulerConfig
import aio_pika

logger = logging.getLogger('uvicorn.error')

MQ_STARTUP_TIMEOUT_SECONDS = 300
MQ_RECONNECT_DELAY_MAX = 30

class SystemInfo(BaseModel):
    deploymentName: str

class SchedulerApplication:
    def __init__(self, config: SchedulerConfig):
        self._config = config
        from .backend import SchedulerBackend
        self._backend = SchedulerBackend(config)
        self._scheduler_server: SchedulerServer = None

    async def mount_apis(self, app: FastAPI):
        @app.get('/system/info')
        async def get_system_info() -> SystemInfo:
            return SystemInfo(deploymentName=self._config.deploymentName)

    async def _connect_mq(self) -> aio_pika.abc.AbstractRobustConnection:
        startup_deadline = time.monotonic() + MQ_STARTUP_TIMEOUT_SECONDS
        attempt = 0
        while True:
            try:
                return await aio_pika.connect_robust(os.environ['KGYM_MQ_CONN_URL'])
            except aio_pika.exceptions.AMQPConnectionError as exc:
                if time.monotonic() >= startup_deadline:
                    raise
                attempt += 1
                delay = min(2 ** attempt, MQ_RECONNECT_DELAY_MAX)
                logger.warning(
                    f'MQ connection attempt {attempt} failed: {exc}; retrying in {delay}s'
                )
                await asyncio.sleep(delay)

    def main(self):
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await self._backend.start()
            mq_conn = await self._connect_mq()
            self._scheduler_server = SchedulerServer(mq_conn, self._backend)
            await self._backend.mount_apis(app)
            await self._scheduler_server.mount_apis(app)
            await self.mount_apis(app)
            await self._scheduler_server.start()
            yield
            await self._backend.stop()
        self._api = FastAPI(lifespan=lifespan)
        self._api.add_middleware(
            CORSMiddleware,
            allow_origins=self._config.allowedOrigins
        )
        uvicorn.run(self._api, host=self._config.listen, port=self._config.listenPort)
