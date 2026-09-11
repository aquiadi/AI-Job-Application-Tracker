from __future__ import annotations

import pytest

from jobtrack_core.logs import _BANNED_KEYS, _reject_pii, _to_cloud_logging


class TestPiiRejection:
    def test_a_clean_event_passes_through_untouched(self) -> None:
        event = {"event": "job_extracted", "job_id": "j-1", "requirement_count": 12}
        assert _reject_pii(None, "info", dict(event)) == event

    @pytest.mark.parametrize("key", sorted(_BANNED_KEYS))
    def test_every_banned_key_drops_the_event(self, key: str) -> None:
        result = _reject_pii(None, "info", {"event": "extract_failed", key: "sensitive"})

        assert result["event"] == "log_call_dropped_for_pii"
        assert result["offending_keys"] == [key]
        assert "sensitive" not in str(result)

    def test_the_dropped_event_names_the_original_so_the_bug_is_findable(self) -> None:
        result = _reject_pii(None, "info", {"event": "tailoring_done", "bullets": ["..."]})

        assert result["origin_event"] == "tailoring_done"
        assert result["level"] == "error"

    def test_all_offending_keys_are_reported_not_just_the_first(self) -> None:
        result = _reject_pii(None, "info", {"event": "x", "email": "a@b.c", "phone": "555"})

        assert result["offending_keys"] == ["email", "phone"]

    def test_resume_text_is_banned(self) -> None:
        # The single most damaging thing this system could log.
        assert "resume_text" in _BANNED_KEYS
        assert "jd_text" in _BANNED_KEYS


class TestCloudLoggingShape:
    def test_level_becomes_severity_and_event_becomes_message(self) -> None:
        # Cloud Logging reads `severity` and `message`. Anything else lands as
        # untyped text at INFO, which hides errors in the console.
        result = _to_cloud_logging(None, "error", {"event": "relay_failed", "level": "error"})

        assert result["severity"] == "ERROR"
        assert result["message"] == "relay_failed"
        assert "level" not in result
        assert "event" not in result

    def test_exception_maps_to_error_severity(self) -> None:
        result = _to_cloud_logging(None, "exception", {"event": "boom", "level": "exception"})
        assert result["severity"] == "ERROR"

    def test_an_unknown_level_degrades_to_info_rather_than_raising(self) -> None:
        result = _to_cloud_logging(None, "notice", {"event": "x", "level": "notice"})
        assert result["severity"] == "INFO"
