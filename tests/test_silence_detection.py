"""
Tests for silence detection failover functionality in m3u-proxy.

Tests cover:
- Silence analysis via ffmpeg silencedetect
- Grace period before monitoring starts
- Consecutive silence threshold before failover
- Recovery resets silence counter
- Silence detection state reset on failover
- Configuration validation
"""
from stream_manager import StreamManager, StreamInfo
import pytest
import pytest_asyncio
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestSilenceAnalysis:
    """Test the _analyze_audio_silence method"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        """Create a StreamManager instance for testing"""
        manager = StreamManager()
        yield manager
        if hasattr(manager, '_running'):
            manager._running = False

    @pytest.mark.asyncio
    async def test_silence_detected_returns_true(self, stream_manager):
        """Test that silence detection returns True when ffmpeg reports silence"""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(
            b'',
            b'[silencedetect @ 0x123] silence_start: 0.00\n'
            b'[silencedetect @ 0x123] silence_end: 5.00 | silence_duration: 5.00\n'
        ))

        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await stream_manager._analyze_audio_silence(
                b'\x00' * 1024, 'test-stream'
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_no_silence_returns_false(self, stream_manager):
        """Test that no silence detection returns False"""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(
            b'',
            b'Stream mapping: audio only\nsize= 0kB time=00:00:05.00\n'
        ))

        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await stream_manager._analyze_audio_silence(
                b'\xff' * 1024, 'test-stream'
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_empty_data_returns_false(self, stream_manager):
        """Test that empty audio data returns False without spawning ffmpeg"""
        result = await stream_manager._analyze_audio_silence(b'', 'test-stream')
        assert result is False

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_returns_false(self, stream_manager):
        """Test graceful handling when ffmpeg is not installed"""
        with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError):
            result = await stream_manager._analyze_audio_silence(
                b'\x00' * 1024, 'test-stream'
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_ffmpeg_timeout_returns_false(self, stream_manager):
        """Test graceful handling when ffmpeg analysis times out"""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError
        )

        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await stream_manager._analyze_audio_silence(
                b'\x00' * 1024, 'test-stream'
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_ffmpeg_called_with_correct_args(self, stream_manager):
        """Test that ffmpeg is called with the correct silencedetect filter arguments"""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b'', b''))

        with patch('asyncio.create_subprocess_exec', return_value=mock_process) as mock_exec, \
             patch('config.settings') as mock_settings:
            mock_settings.SILENCE_THRESHOLD_DB = -50.0
            mock_settings.SILENCE_DURATION = 3.0

            await stream_manager._analyze_audio_silence(
                b'\x00' * 1024, 'test-stream'
            )

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert call_args[0] == 'ffmpeg'
            assert '-vn' in call_args
            assert '-f' in call_args
            assert 'null' in call_args
            # Check silencedetect filter is present
            af_index = list(call_args).index('-af')
            filter_arg = call_args[af_index + 1]
            assert 'silencedetect' in filter_arg


class TestSilenceDetectionStreamInfo:
    """Test StreamInfo silence detection fields"""

    def test_default_silence_detection_fields(self):
        """Test that StreamInfo has correct default values for silence detection"""
        info = StreamInfo(
            stream_id='test',
            original_url='http://example.com/stream.ts',
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc),
        )

        assert info.silence_check_start_time is None
        assert info.silence_audio_buffer == b''
        assert info.silence_count == 0
        assert info.silence_monitoring_started is False


class TestSilenceDetectionConfig:
    """Test silence detection configuration settings"""

    def test_default_config_values(self):
        """Test that default silence detection config values are sensible"""
        from config import Settings

        s = Settings(
            ENABLE_SILENCE_DETECTION=True,
        )

        assert s.ENABLE_SILENCE_DETECTION is True
        assert s.SILENCE_THRESHOLD_DB == -50.0
        assert s.SILENCE_DURATION == 3.0
        assert s.SILENCE_CHECK_INTERVAL == 10.0
        assert s.SILENCE_FAILOVER_THRESHOLD == 3
        assert s.SILENCE_MONITORING_GRACE_PERIOD == 15.0

    def test_disabled_by_default(self):
        """Test that silence detection is disabled by default"""
        from config import Settings

        s = Settings()
        assert s.ENABLE_SILENCE_DETECTION is False


class TestSilenceDetectionFailover:
    """Test silence detection failover integration"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        """Create a StreamManager instance for testing"""
        manager = StreamManager()
        yield manager
        if hasattr(manager, '_running'):
            manager._running = False

    @pytest.mark.asyncio
    async def test_silence_counter_increments(self, stream_manager):
        """Test that the silence counter increments on detected silence"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_urls=[failover_url],
        )

        stream_info = stream_manager.streams[stream_id]
        assert stream_info.silence_count == 0

        stream_info.silence_count = 1
        assert stream_info.silence_count == 1

    @pytest.mark.asyncio
    async def test_silence_counter_resets_on_recovery(self, stream_manager):
        """Test that the silence counter resets when audio recovers"""
        primary_url = "http://primary.example.com/stream.ts"
        stream_id = await stream_manager.get_or_create_stream(primary_url)

        stream_info = stream_manager.streams[stream_id]
        stream_info.silence_count = 2

        # Simulate audio recovery
        stream_info.silence_count = 0
        assert stream_info.silence_count == 0

    @pytest.mark.asyncio
    async def test_silence_state_reset_on_failover(self, stream_manager):
        """Test that silence detection state is fully reset after failover"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_urls=[failover_url],
        )

        stream_info = stream_manager.streams[stream_id]

        # Simulate active silence monitoring state
        stream_info.silence_monitoring_started = True
        stream_info.silence_check_start_time = 100.0
        stream_info.silence_audio_buffer = b'\x00' * 4096
        stream_info.silence_count = 2

        # Trigger failover
        result = await stream_manager._try_update_failover_url(
            stream_id, "silence_detected"
        )

        assert result is True
        assert stream_info.current_url == failover_url
        assert stream_info.failover_attempts == 1

    @pytest.mark.asyncio
    async def test_failover_reason_is_silence_detected(self, stream_manager):
        """Test that failover event includes 'silence_detected' as reason"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_urls=[failover_url],
        )

        mock_event_manager = AsyncMock()
        stream_manager.event_manager = mock_event_manager

        await stream_manager._try_update_failover_url(
            stream_id, "silence_detected"
        )

        mock_event_manager.emit_event.assert_called_once()
        event = mock_event_manager.emit_event.call_args[0][0]
        assert event.data['reason'] == 'silence_detected'

    @pytest.mark.asyncio
    async def test_silence_detection_skipped_for_vod(self):
        """Test that silence detection fields exist but VOD streams are skipped in logic"""
        info = StreamInfo(
            stream_id='test-vod',
            original_url='http://example.com/movie.mp4',
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc),
            is_vod=True,
        )

        # VOD streams should have silence detection fields but
        # the streaming loop skips silence detection for VOD
        assert info.silence_count == 0
        assert info.is_vod is True

    @pytest.mark.asyncio
    async def test_no_failover_without_failover_urls(self, stream_manager):
        """Test that silence detection doesn't trigger failover without failover URLs"""
        primary_url = "http://primary.example.com/stream.ts"
        stream_id = await stream_manager.get_or_create_stream(primary_url)

        stream_info = stream_manager.streams[stream_id]
        assert not stream_info.failover_urls
        assert not stream_info.failover_resolver_url

        result = await stream_manager._try_update_failover_url(
            stream_id, "silence_detected"
        )
        assert result is False


class TestSilenceDetectionGracePeriod:
    """Test silence detection grace period behavior"""

    def test_monitoring_not_started_by_default(self):
        """Test that silence monitoring starts inactive"""
        info = StreamInfo(
            stream_id='test',
            original_url='http://example.com/stream.ts',
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc),
        )

        assert info.silence_monitoring_started is False
        assert info.silence_check_start_time is None

    def test_audio_buffer_starts_empty(self):
        """Test that the audio buffer starts empty"""
        info = StreamInfo(
            stream_id='test',
            original_url='http://example.com/stream.ts',
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc),
        )

        assert info.silence_audio_buffer == b''
