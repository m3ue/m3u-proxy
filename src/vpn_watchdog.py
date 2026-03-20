"""
VPN Watchdog — proactive VPN health monitoring integrated into m3u-proxy.

Monitors VPN connectivity via the Gluetun Control API, correlates stream failures
with VPN health to distinguish VPN issues from provider issues, and triggers
VPN rotation only when genuinely needed. Reports status via new API endpoints
and emits events for the m3u-editor Stream Monitor.

State Machine:
  HEALTHY → DEGRADED (2+ consecutive bad checks)
  DEGRADED → UNHEALTHY (4+ consecutive bad checks)
  UNHEALTHY → DOWN (VPN disconnected or unreachable)
  Any → HEALTHY (2+ consecutive good checks, with hysteresis)
"""

import asyncio
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx

from config import settings
from models import EventType, StreamEvent

logger = logging.getLogger(__name__)


class VPNHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DOWN = "down"
    UNKNOWN = "unknown"


class FailureCategory(str, Enum):
    VPN_ISSUE = "vpn_issue"
    PROVIDER_ISSUE = "provider_issue"
    BANDWIDTH_ISSUE = "bandwidth_issue"
    TRANSIENT = "transient"


@dataclass
class VPNCheckResult:
    """Result of a single VPN health check cycle."""

    timestamp: float = field(default_factory=time.time)
    vpn_connected: bool = False
    public_ip: Optional[str] = None
    dns_ok: bool = False
    dns_latency_ms: Optional[float] = None
    http_ok: bool = False
    http_latency_ms: Optional[float] = None
    gluetun_reachable: bool = False
    error: Optional[str] = None

    @property
    def is_good(self) -> bool:
        """Check considered good if VPN connected + DNS works + HTTP reachable."""
        return self.vpn_connected and self.dns_ok and self.http_ok

    @property
    def is_partial(self) -> bool:
        """Partial: VPN connected but high latency or DNS issues."""
        return self.vpn_connected and (not self.dns_ok or not self.http_ok)


@dataclass
class RotationRecord:
    """Record of a VPN rotation event."""

    timestamp: str
    reason: str
    old_ip: Optional[str]
    new_ip: Optional[str]
    success: bool
    duration_seconds: float
    state_before: str
    state_after: str


@dataclass
class StreamFailureRecord:
    """Record of a stream failure for correlation analysis."""

    timestamp: float
    stream_id: str
    event_type: str
    reason: str


class VPNWatchdog:
    """
    Proactive VPN health monitor integrated into m3u-proxy.

    Follows the same lifecycle pattern as StreamManager and BroadcastManager:
    - async start() / async stop()
    - set_event_manager() for dependency injection
    - Background asyncio.Task for periodic monitoring
    """

    def __init__(self):
        self.event_manager = None
        self.stream_manager = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # State machine
        self._state = VPNHealthState.UNKNOWN
        self._consecutive_bad = 0
        self._consecutive_good = 0
        self._last_check: Optional[VPNCheckResult] = None
        self._check_history: deque[VPNCheckResult] = deque(maxlen=60)

        # IP tracking
        self._current_ip: Optional[str] = None
        self._ip_changes: int = 0

        # Rotation state
        self._last_rotation_time: float = 0.0
        self._rotation_in_progress = False
        self._rotation_history: deque[RotationRecord] = deque(maxlen=50)

        # Stream failure correlation
        self._recent_failures: deque[StreamFailureRecord] = deque(maxlen=100)
        self._failure_lock = asyncio.Lock()

        # HTTP client (reused across checks)
        self._http_client: Optional[httpx.AsyncClient] = None

    def set_event_manager(self, event_manager) -> None:
        """Inject EventManager for emitting VPN events."""
        self.event_manager = event_manager

    def set_stream_manager(self, stream_manager) -> None:
        """Inject StreamManager for direct stats access."""
        self.stream_manager = stream_manager

    async def start(self) -> None:
        """Start VPN watchdog background monitoring."""
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        # Register event handler for stream failure correlation
        if self.event_manager:
            self.event_manager.add_handler(self._on_stream_event)

        logger.info(
            "VPN watchdog started "
            f"(interval={settings.VPN_WATCHDOG_CHECK_INTERVAL}s, "
            f"gluetun={settings.VPN_WATCHDOG_GLUETUN_URL})"
        )

    async def stop(self) -> None:
        """Stop VPN watchdog gracefully."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("VPN watchdog stopped")

    # ─── Public API (called by api.py endpoints) ──────────────────────

    def get_status(self) -> dict:
        """Return current VPN watchdog status for API response."""
        last = self._last_check
        return {
            "enabled": True,
            "state": self._state.value,
            "current_ip": self._current_ip,
            "last_check": {
                "timestamp": datetime.fromtimestamp(
                    last.timestamp, tz=timezone.utc
                ).isoformat()
                if last
                else None,
                "vpn_connected": last.vpn_connected if last else None,
                "dns_ok": last.dns_ok if last else None,
                "dns_latency_ms": round(last.dns_latency_ms, 1)
                if last and last.dns_latency_ms is not None
                else None,
                "http_ok": last.http_ok if last else None,
                "http_latency_ms": round(last.http_latency_ms, 1)
                if last and last.http_latency_ms is not None
                else None,
                "gluetun_reachable": last.gluetun_reachable if last else None,
                "error": last.error if last else None,
            },
            "consecutive_bad_checks": self._consecutive_bad,
            "consecutive_good_checks": self._consecutive_good,
            "ip_changes_detected": self._ip_changes,
            "rotation_in_progress": self._rotation_in_progress,
            "last_rotation": self._rotation_history[-1].__dict__
            if self._rotation_history
            else None,
            "total_rotations": len(self._rotation_history),
            "check_interval": self._get_current_interval(),
            "config": {
                "failure_threshold": settings.VPN_WATCHDOG_FAILURE_THRESHOLD,
                "failure_window": settings.VPN_WATCHDOG_FAILURE_WINDOW,
                "rotation_cooldown": settings.VPN_WATCHDOG_ROTATION_COOLDOWN,
                "latency_warn_ms": settings.VPN_WATCHDOG_LATENCY_WARN_MS,
                "latency_critical_ms": settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS,
            },
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        """Return VPN rotation history for API response."""
        records = list(self._rotation_history)[-limit:]
        return [r.__dict__ for r in reversed(records)]

    # ─── Main Monitor Loop ────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Periodic VPN health monitoring with adaptive interval."""
        # Initial delay to let the proxy and Gluetun stabilize
        await asyncio.sleep(10)

        while self._running:
            try:
                check = await self._perform_health_check()
                self._check_history.append(check)
                self._last_check = check

                old_state = self._state
                self._update_state(check)

                if old_state != self._state:
                    logger.warning(
                        f"VPN state changed: {old_state.value} → {self._state.value}"
                    )
                    await self._emit_event(
                        EventType.VPN_HEALTH_CHANGED,
                        {
                            "old_state": old_state.value,
                            "new_state": self._state.value,
                            "ip": self._current_ip,
                            "check": {
                                "vpn_connected": check.vpn_connected,
                                "dns_ok": check.dns_ok,
                                "http_ok": check.http_ok,
                                "dns_latency_ms": check.dns_latency_ms,
                                "http_latency_ms": check.http_latency_ms,
                            },
                        },
                    )

                # Check if rotation is needed
                if self._should_rotate():
                    asyncio.create_task(self._rotate_vpn("health_check_threshold"))

                interval = self._get_current_interval()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"VPN watchdog monitor error: {e}")
                await asyncio.sleep(settings.VPN_WATCHDOG_CHECK_INTERVAL)

    # ─── Health Check Implementation ──────────────────────────────────

    async def _perform_health_check(self) -> VPNCheckResult:
        """Run a complete VPN health check cycle."""
        result = VPNCheckResult()

        # 1. Check Gluetun VPN status
        try:
            gluetun_url = settings.VPN_WATCHDOG_GLUETUN_URL.rstrip("/")
            headers = {}
            if settings.VPN_WATCHDOG_GLUETUN_API_KEY:
                headers["X-API-Key"] = settings.VPN_WATCHDOG_GLUETUN_API_KEY

            resp = await self._http_client.get(
                f"{gluetun_url}/v1/vpn/status", headers=headers
            )
            if resp.status_code == 200:
                data = resp.json()
                result.gluetun_reachable = True
                result.vpn_connected = data.get("status") == "running"
            else:
                result.gluetun_reachable = True
                result.vpn_connected = False
                result.error = f"Gluetun status endpoint returned {resp.status_code}"
        except httpx.ConnectError:
            result.error = "Gluetun API unreachable"
            return result
        except Exception as e:
            result.error = f"Gluetun check failed: {e}"
            return result

        # 2. Get public IP
        if result.vpn_connected:
            try:
                resp = await self._http_client.get(
                    f"{gluetun_url}/v1/publicip/ip", headers=headers
                )
                if resp.status_code == 200:
                    data = resp.json()
                    new_ip = data.get("public_ip")
                    if new_ip and self._current_ip and new_ip != self._current_ip:
                        if not self._rotation_in_progress:
                            self._ip_changes += 1
                            logger.warning(
                                f"VPN IP changed unexpectedly: "
                                f"{self._current_ip} → {new_ip}"
                            )
                    if new_ip:
                        self._current_ip = new_ip
                    result.public_ip = new_ip
            except Exception as e:
                logger.debug(f"Failed to get public IP: {e}")

        # 3. DNS resolution test
        if result.vpn_connected:
            try:
                dns_host = settings.VPN_WATCHDOG_DNS_TEST_HOST
                start = time.monotonic()
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.getaddrinfo(dns_host, 80, family=socket.AF_INET),
                    timeout=5.0,
                )
                elapsed_ms = (time.monotonic() - start) * 1000
                result.dns_ok = True
                result.dns_latency_ms = elapsed_ms
            except (asyncio.TimeoutError, socket.gaierror) as e:
                result.dns_ok = False
                result.dns_latency_ms = None
                logger.debug(f"DNS check failed: {e}")

        # 4. HTTP connectivity / latency test
        if result.vpn_connected and result.dns_ok:
            try:
                test_url = settings.VPN_WATCHDOG_HTTP_TEST_URL
                start = time.monotonic()
                resp = await self._http_client.get(test_url, timeout=8.0)
                elapsed_ms = (time.monotonic() - start) * 1000
                result.http_ok = resp.status_code < 400
                result.http_latency_ms = elapsed_ms
            except Exception as e:
                result.http_ok = False
                logger.debug(f"HTTP connectivity check failed: {e}")

        return result

    # ─── State Machine ────────────────────────────────────────────────

    def _update_state(self, check: VPNCheckResult) -> None:
        """Update health state based on check result with hysteresis."""
        if check.is_good:
            self._consecutive_bad = 0
            self._consecutive_good += 1
        else:
            self._consecutive_good = 0
            self._consecutive_bad += 1

        # Determine new state
        if not check.gluetun_reachable or not check.vpn_connected:
            self._state = VPNHealthState.DOWN
        elif self._consecutive_bad >= 4:
            self._state = VPNHealthState.UNHEALTHY
        elif self._consecutive_bad >= 2:
            self._state = VPNHealthState.DEGRADED
        elif self._consecutive_good >= 2:
            self._state = VPNHealthState.HEALTHY
        # Otherwise keep current state (hysteresis / anti-flapping)

        # Also check latency thresholds when VPN is connected
        if check.is_good and check.http_latency_ms is not None:
            if check.http_latency_ms > settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS:
                if self._state == VPNHealthState.HEALTHY:
                    self._state = VPNHealthState.DEGRADED
            elif check.http_latency_ms > settings.VPN_WATCHDOG_LATENCY_WARN_MS:
                if self._state == VPNHealthState.HEALTHY and self._consecutive_bad >= 1:
                    self._state = VPNHealthState.DEGRADED

    def _get_current_interval(self) -> float:
        """Return adaptive check interval based on current state."""
        if self._state in (VPNHealthState.UNHEALTHY, VPNHealthState.DOWN):
            return max(5, settings.VPN_WATCHDOG_CHECK_INTERVAL // 3)
        if self._state == VPNHealthState.DEGRADED:
            return max(5, settings.VPN_WATCHDOG_CHECK_INTERVAL // 2)
        return settings.VPN_WATCHDOG_CHECK_INTERVAL

    # ─── Stream Failure Correlation ───────────────────────────────────

    async def _on_stream_event(self, event: StreamEvent) -> None:
        """Event handler registered with EventManager for stream failure correlation."""
        if event.event_type not in (
            EventType.STREAM_FAILED,
            EventType.FAILOVER_TRIGGERED,
        ):
            return

        record = StreamFailureRecord(
            timestamp=time.time(),
            stream_id=event.stream_id,
            event_type=event.event_type.value,
            reason=event.data.get("reason", "unknown"),
        )

        async with self._failure_lock:
            self._recent_failures.append(record)

        category = self._classify_failure(record)
        logger.info(
            f"Stream failure classified as {category.value}: "
            f"stream={event.stream_id[:12]}... vpn_state={self._state.value}"
        )

        if category == FailureCategory.VPN_ISSUE and self._should_rotate():
            asyncio.create_task(
                self._rotate_vpn(f"correlated_stream_failure:{record.reason}")
            )

    def _classify_failure(self, record: StreamFailureRecord) -> FailureCategory:
        """Classify a stream failure based on VPN health correlation."""
        # If VPN is down, it's definitely a VPN issue
        if self._state == VPNHealthState.DOWN:
            return FailureCategory.VPN_ISSUE

        # Check for correlated failures (multiple streams failing in short window)
        now = time.time()
        window = settings.VPN_WATCHDOG_FAILURE_WINDOW
        recent = [r for r in self._recent_failures if now - r.timestamp < window]

        # Count unique streams affected in the window
        unique_streams = len({r.stream_id for r in recent})

        # Multiple unique streams failing = likely VPN issue
        if unique_streams >= 2 and self._state != VPNHealthState.HEALTHY:
            return FailureCategory.VPN_ISSUE

        # Many failures from different streams even with healthy VPN
        if unique_streams >= 3:
            return FailureCategory.VPN_ISSUE

        # VPN degraded + stream failure = likely VPN related
        if self._state in (VPNHealthState.DEGRADED, VPNHealthState.UNHEALTHY):
            failure_count = len(recent)
            if failure_count >= settings.VPN_WATCHDOG_FAILURE_THRESHOLD:
                return FailureCategory.VPN_ISSUE
            return FailureCategory.BANDWIDTH_ISSUE

        # Single stream failure with healthy VPN = provider issue
        if self._state == VPNHealthState.HEALTHY and unique_streams <= 1:
            return FailureCategory.PROVIDER_ISSUE

        return FailureCategory.TRANSIENT

    # ─── Rotation Logic ───────────────────────────────────────────────

    def _should_rotate(self) -> bool:
        """Determine if VPN rotation should be triggered."""
        if self._rotation_in_progress:
            return False

        # Respect cooldown
        now = time.time()
        if now - self._last_rotation_time < settings.VPN_WATCHDOG_ROTATION_COOLDOWN:
            return False

        # Only rotate when state is actually bad
        if self._state in (VPNHealthState.UNHEALTHY, VPNHealthState.DOWN):
            return True

        # Check failure threshold with correlation
        window = settings.VPN_WATCHDOG_FAILURE_WINDOW
        recent = [r for r in self._recent_failures if now - r.timestamp < window]
        if (
            len(recent) >= settings.VPN_WATCHDOG_FAILURE_THRESHOLD
            and self._state != VPNHealthState.HEALTHY
        ):
            return True

        return False

    async def _rotate_vpn(self, reason: str) -> bool:
        """Execute VPN rotation via Gluetun Stop/Start."""
        if self._rotation_in_progress:
            logger.info("VPN rotation already in progress, skipping")
            return False

        self._rotation_in_progress = True
        start_time = time.time()
        old_ip = self._current_ip
        state_before = self._state.value

        logger.warning(f"=== VPN rotation started (reason: {reason}) ===")

        await self._emit_event(
            EventType.VPN_ROTATION_STARTED,
            {"reason": reason, "current_ip": old_ip, "state": state_before},
        )

        try:
            gluetun_url = settings.VPN_WATCHDOG_GLUETUN_URL.rstrip("/")
            headers = {"Content-Type": "application/json"}
            if settings.VPN_WATCHDOG_GLUETUN_API_KEY:
                headers["X-API-Key"] = settings.VPN_WATCHDOG_GLUETUN_API_KEY

            # Stop VPN
            logger.info("Stopping VPN...")
            await self._http_client.put(
                f"{gluetun_url}/v1/vpn/status",
                json={"status": "stopped"},
                headers=headers,
            )

            await asyncio.sleep(settings.VPN_WATCHDOG_RECONNECT_DELAY)

            # Start VPN
            logger.info("Starting VPN...")
            await self._http_client.put(
                f"{gluetun_url}/v1/vpn/status",
                json={"status": "running"},
                headers=headers,
            )

            # Wait for connection to establish
            await asyncio.sleep(settings.VPN_WATCHDOG_RECONNECT_DELAY)

            # Post-rotation validation
            post_check = await self._perform_health_check()
            new_ip = post_check.public_ip
            duration = time.time() - start_time
            success = post_check.vpn_connected and new_ip and new_ip != old_ip

            if success:
                self._current_ip = new_ip
                self._consecutive_bad = 0
                self._consecutive_good = 1
                self._state = VPNHealthState.HEALTHY

                # Clear failure records after successful rotation
                async with self._failure_lock:
                    self._recent_failures.clear()

            state_after = self._state.value

            record = RotationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                reason=reason,
                old_ip=old_ip,
                new_ip=new_ip,
                success=success,
                duration_seconds=round(duration, 1),
                state_before=state_before,
                state_after=state_after,
            )
            self._rotation_history.append(record)
            self._last_rotation_time = time.time()

            if success:
                logger.warning(
                    f"=== VPN rotation completed: {old_ip} → {new_ip} "
                    f"({duration:.1f}s) ==="
                )
            else:
                logger.error(
                    f"=== VPN rotation may have failed: "
                    f"old={old_ip} new={new_ip} connected={post_check.vpn_connected} "
                    f"({duration:.1f}s) ==="
                )

            await self._emit_event(
                EventType.VPN_ROTATION_COMPLETED,
                {
                    "success": success,
                    "reason": reason,
                    "old_ip": old_ip,
                    "new_ip": new_ip,
                    "duration_seconds": round(duration, 1),
                    "state_before": state_before,
                    "state_after": state_after,
                },
            )

            return success

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"VPN rotation failed with error: {e}")

            record = RotationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                reason=reason,
                old_ip=old_ip,
                new_ip=None,
                success=False,
                duration_seconds=round(duration, 1),
                state_before=state_before,
                state_after=self._state.value,
            )
            self._rotation_history.append(record)
            self._last_rotation_time = time.time()

            await self._emit_event(
                EventType.VPN_ROTATION_FAILED,
                {
                    "reason": reason,
                    "error": str(e),
                    "duration_seconds": round(duration, 1),
                },
            )

            return False

        finally:
            self._rotation_in_progress = False

    async def rotate(self, reason: str = "manual") -> dict:
        """Public method for manual rotation via API endpoint."""
        if not self._running:
            return {
                "success": False,
                "message": "Watchdog is not running",
            }

        if self._rotation_in_progress:
            return {
                "success": False,
                "message": "Rotation already in progress",
            }

        now = time.time()
        cooldown_remaining = settings.VPN_WATCHDOG_ROTATION_COOLDOWN - (
            now - self._last_rotation_time
        )
        if cooldown_remaining > 0:
            return {
                "success": False,
                "message": f"Cooldown active, {int(cooldown_remaining)}s remaining",
            }

        success = await self._rotate_vpn(reason)
        return {
            "success": success,
            "message": "Rotation completed" if success else "Rotation may have failed",
            "new_ip": self._current_ip,
            "state": self._state.value,
        }

    # ─── Event Emission ───────────────────────────────────────────────

    async def _emit_event(self, event_type: EventType, data: dict) -> None:
        """Emit VPN event through the event manager."""
        if not self.event_manager:
            return
        try:
            event = StreamEvent(
                event_type=event_type,
                stream_id="vpn-watchdog",
                data=data,
            )
            await self.event_manager.emit_event(event)
        except Exception as e:
            logger.error(f"Failed to emit VPN event: {e}")
