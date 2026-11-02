import asyncio
import httpx
import inspect
import logging
from typing import List, Optional
from models import StreamEvent, WebhookConfig

logger = logging.getLogger(__name__)


class EventManager:
    def __init__(self):
        self.webhooks: List[WebhookConfig] = []
        self.event_queue = asyncio.Queue()
        self.event_handlers: List[callable] = []
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the event manager worker."""
        self._running = True
        self._worker_task = asyncio.create_task(self._process_events())
        logger.info("Event manager started")

    async def stop(self):
        """Stop the event manager."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Event manager stopped")

    def add_webhook(self, webhook: WebhookConfig):
        """Add a webhook configuration."""
        self.webhooks.append(webhook)
        logger.info(f"Added webhook for {webhook.url}")

    def remove_webhook(self, webhook_url: str) -> bool:
        """Remove a webhook by URL."""
        initial_count = len(self.webhooks)
        self.webhooks = [wh for wh in self.webhooks if str(wh.url) != webhook_url]
        removed = len(self.webhooks) != initial_count
        if removed:
            logger.info(f"Removed webhook {webhook_url}")
        return removed

    def add_handler(self, handler: callable):
        """Add an event handler function."""
        self.event_handlers.append(handler)
        logger.info(f"Added event handler: {handler.__name__}")

    async def emit_event(self, event: StreamEvent):
        """Emit an event to be processed."""
        await self.event_queue.put(event)
        logger.debug(f"Emitted event: {event.event_type} for stream {event.stream_id}")

    async def _process_events(self):
        """Process events from the queue."""
        while self._running:
            try:
                # Wait for event with timeout to allow checking _running
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Process event
                await self._handle_event(event)

            except Exception as e:
                logger.error(f"Error processing event: {e}")

    async def _handle_event(self, event: StreamEvent):
        """Handle a single event."""
        # Call registered handlers
        for handler in self.event_handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Error in event handler {handler.__name__}: {e}")

        # Send to webhooks
        await self._send_webhooks(event)

    async def _send_webhooks(self, event: StreamEvent):
        """Send event to configured webhooks."""
        tasks = []

        for webhook in self.webhooks:
            if event.event_type in webhook.events:
                task = asyncio.create_task(self._send_webhook(webhook, event))
                tasks.append(task)

        if tasks:
            # Wait for all webhooks to complete
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_webhook(self, webhook: WebhookConfig, event: StreamEvent) -> bool:
        """Send event to a single webhook. Returns True once an attempt succeeds."""
        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "stream_id": event.stream_id,
            "timestamp": event.timestamp.isoformat(),
            "data": event.data,
        }

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "M3U-Proxy-Webhook/1.0",
            **webhook.headers,
        }

        for attempt in range(webhook.retry_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=webhook.timeout) as client:
                    response = await client.post(
                        str(webhook.url), json=payload, headers=headers
                    )
                    if response.status_code < 400:
                        logger.debug(f"Webhook sent successfully to {webhook.url}")
                        return True
                    else:
                        logger.warning(
                            f"Webhook failed with status {response.status_code}: {webhook.url}"
                        )

            except Exception as e:
                logger.warning(
                    f"Webhook attempt {attempt + 1} failed for {webhook.url}: {e}"
                )

            # Wait before retry (except on last attempt)
            if attempt < webhook.retry_attempts:
                await asyncio.sleep(2**attempt)  # Exponential backoff

        logger.error(f"All webhook attempts failed for {webhook.url}")
        return False
