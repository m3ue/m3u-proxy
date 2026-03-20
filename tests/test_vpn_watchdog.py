"""
Tests for the VPN Watchdog module.
"""

import asyncio
import sys
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vpn_watchdog import VPNWatchdog, VPNHealthState


class TestVPNWatchdogInit:
    """Test VPN Watchdog initialization and configuration."""

    def test_default_state_is_unknown(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            assert watchdog._state == VPNHealthState.UNKNOWN

    def test_get_status_before_start(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            status = watchdog.get_status()
            assert status["state"] == "unknown"
            assert status["current_ip"] is None
            assert status["total_rotations"] == 0

    def test_get_history_empty(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            history = watchdog.get_history()
            assert history == []


class TestVPNWatchdogLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_monitor_task(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            await watchdog.start()
            assert watchdog._running is True
            assert watchdog._monitor_task is not None
            await watchdog.stop()
            assert watchdog._running is False

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            # Stop without start should not raise
            await watchdog.stop()
            assert watchdog._running is False


class TestVPNWatchdogManualRotate:
    """Test manual VPN rotation."""

    @pytest.mark.asyncio
    async def test_rotate_when_not_running(self):
        with patch("vpn_watchdog.settings") as mock_settings:
            mock_settings.VPN_WATCHDOG_GLUETUN_URL = "http://127.0.0.1:8000"
            mock_settings.VPN_WATCHDOG_GLUETUN_API_KEY = ""
            mock_settings.VPN_WATCHDOG_CHECK_INTERVAL = 15
            mock_settings.VPN_WATCHDOG_FAILURE_THRESHOLD = 3
            mock_settings.VPN_WATCHDOG_FAILURE_WINDOW = 300
            mock_settings.VPN_WATCHDOG_ROTATION_COOLDOWN = 600
            mock_settings.VPN_WATCHDOG_RECONNECT_DELAY = 5
            mock_settings.VPN_WATCHDOG_LATENCY_WARN_MS = 200
            mock_settings.VPN_WATCHDOG_LATENCY_CRITICAL_MS = 500
            mock_settings.VPN_WATCHDOG_DNS_TEST_HOST = "google.com"
            mock_settings.VPN_WATCHDOG_HTTP_TEST_URL = "http://httpbin.org/ip"

            watchdog = VPNWatchdog()
            result = await watchdog.rotate(reason="test")
            assert result["success"] is False
            assert "not running" in result["message"].lower()


class TestVPNHealthState:
    """Test VPN health state enum."""

    def test_all_states_exist(self):
        assert VPNHealthState.HEALTHY.value == "healthy"
        assert VPNHealthState.DEGRADED.value == "degraded"
        assert VPNHealthState.UNHEALTHY.value == "unhealthy"
        assert VPNHealthState.DOWN.value == "down"
        assert VPNHealthState.UNKNOWN.value == "unknown"


class TestVPNEventTypes:
    """Test VPN event types are registered."""

    def test_vpn_event_types_exist(self):
        from models import EventType

        assert EventType.VPN_HEALTH_CHANGED.value == "vpn_health_changed"
        assert EventType.VPN_ROTATION_STARTED.value == "vpn_rotation_started"
        assert EventType.VPN_ROTATION_COMPLETED.value == "vpn_rotation_completed"
        assert EventType.VPN_ROTATION_FAILED.value == "vpn_rotation_failed"
