"""
Tests for the input-validation helpers in ``orchestrator.mcp_server.server``.

These helpers are the guard rail between an LLM-driven tool surface and the
LogQL / API layers, so malformed input is the expected case rather than an edge
case. Every test here is hermetic: the helpers under test are pure functions and
nothing in this module performs I/O, network calls, or API requests.

Tests marked ``QUIRK`` pin down surprising-but-current behaviour so a change is
noticed rather than silently shipped; they are not endorsements.
"""

import os
import sys
import types
from collections.abc import Generator

import pytest

from orchestrator.mcp_server import server as s

# =============================================================================
# _validate_time_range
# =============================================================================


class TestValidateTimeRange:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("15m", "15m"),
            ("1h", "1h"),
            ("24h", "24h"),
            ("7d", "7d"),
            ("0m", "0m"),  # zero is accepted: the regex only constrains digit count
            ("999d", "999d"),  # 3 digits is the documented maximum
            ("1H", "1h"),  # case-insensitive, normalised to lower
            ("24H", "24h"),
            ("7D", "7d"),
        ],
    )
    def test_accepts_and_normalises(self, value: str, expected: str) -> None:
        assert s._validate_time_range(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "1w",  # weeks are not a supported unit
            "1s",  # seconds are not a supported unit
            "1000m",  # 4 digits exceeds \d{1,3}
            "h",
            "1",
            "1hh",
            "-1h",
            "+1h",
            "1.5h",
            " 1h",  # leading whitespace is not stripped
            "1h ",  # trailing whitespace is not stripped
            "1 h",
            "\n1h",  # leading newline is rejected
            "1m\n1h",  # embedded newline is rejected
            "1h; DROP TABLE logs",
            "24h OR 1=1",
            '1h"} |= "secret',  # LogQL selector break-out attempt
            "1h|~`.*`",
            "../../etc/passwd",
            "🕐h",
        ],
    )
    def test_rejects_malformed(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid time_range format"):
            s._validate_time_range(value)

    def test_error_message_echoes_original_casing(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_time_range("1W")
        assert str(excinfo.value) == (
            "Invalid time_range format: 1W. Use format like '15m', '1h', '24h', '7d'"
        )

    @pytest.mark.parametrize("value", [None, 5, 1.0, True, ["1h"], b"1h", {"t": "1h"}])
    def test_rejects_non_string(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_time_range(value)  # type: ignore[arg-type]
        assert str(excinfo.value) == "time_range must be a string"

    def test_non_string_check_precedes_pattern_check(self) -> None:
        """A bytes value must not be reported as a bad *format* — it is a bad type."""
        with pytest.raises(ValueError) as excinfo:
            s._validate_time_range(b"1h")  # type: ignore[arg-type]
        assert "Invalid time_range format" not in str(excinfo.value)

    def test_quirk_trailing_newline_passes_through(self) -> None:
        """QUIRK: Python's ``$`` also matches before a trailing newline, so "1h\\n"
        is accepted and returned verbatim — a newline reaches the caller."""
        assert s._validate_time_range("1h\n") == "1h\n"

    def test_quirk_non_ascii_digits_are_accepted(self) -> None:
        """QUIRK: ``\\d`` is Unicode-aware, so non-ASCII digits pass validation and
        are handed downstream unchanged."""
        assert s._validate_time_range("١h") == "١h"  # Arabic-Indic 1
        assert s._validate_time_range("१२h") == "१२h"  # Devanagari 12

    def test_pattern_is_anchored_at_both_ends(self) -> None:
        assert s._TIME_RANGE_PATTERN.match("xx1h") is None
        assert s._TIME_RANGE_PATTERN.match("1hx") is None


# =============================================================================
# _validate_component
# =============================================================================


class TestValidateComponent:
    def test_valid_component_set_is_stable(self) -> None:
        assert s._VALID_COMPONENTS == {
            "all",
            "api",
            "runner",
            "lambda",
            "apache",
            "mysql",
            "redis",
            "php",
            "memcached",
        }

    @pytest.mark.parametrize("value", sorted(s._VALID_COMPONENTS))
    def test_accepts_every_declared_component(self, value: str) -> None:
        assert s._validate_component(value) == value

    @pytest.mark.parametrize(
        "value,expected",
        [("API", "api"), ("Runner", "runner"), ("LAMBDA", "lambda"), ("MySQL", "mysql")],
    )
    def test_normalises_case(self, value: str, expected: str) -> None:
        assert s._validate_component(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "api ",  # whitespace is not stripped
            " api",
            "api\n",
            "loki",
            "grafana",
            "postgres",
            "all,api",
            "*",
            "api|runner",
            "../api",
            "аpi",  # Cyrillic 'а' homoglyph
        ],
    )
    def test_rejects_unknown(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid component"):
            s._validate_component(value)

    def test_error_message_lists_valid_components_sorted(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_component("all; DROP TABLE logs")
        assert str(excinfo.value) == (
            "Invalid component: all; drop table logs. "
            "Valid: all, apache, api, lambda, memcached, mysql, php, redis, runner"
        )

    @pytest.mark.parametrize("value", [None, 1, 1.0, ["api"], b"api", {"c": "api"}])
    def test_rejects_non_string(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_component(value)  # type: ignore[arg-type]
        assert str(excinfo.value) == "component must be a string"


# =============================================================================
# _validate_level
# =============================================================================


class TestValidateLevel:
    def test_valid_level_set_is_stable(self) -> None:
        assert s._VALID_LEVELS == {
            "all",
            "debug",
            "info",
            "warning",
            "warn",
            "error",
            "critical",
        }

    @pytest.mark.parametrize("value", sorted(s._VALID_LEVELS))
    def test_accepts_every_declared_level(self, value: str) -> None:
        assert s._validate_level(value) == value

    def test_warn_and_warning_are_both_accepted_verbatim(self) -> None:
        """Neither alias is folded into the other — the returned value is what
        goes into the LogQL ``level=~"(?i)..."`` matcher."""
        assert s._validate_level("warn") == "warn"
        assert s._validate_level("warning") == "warning"

    @pytest.mark.parametrize(
        "value,expected",
        [("ERROR", "error"), ("Info", "info"), ("CRITICAL", "critical"), ("All", "all")],
    )
    def test_normalises_case(self, value: str, expected: str) -> None:
        assert s._validate_level(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "error ",
            "trace",
            "fatal",
            "notice",
            "err",
            "info,error",
            "error|critical",
            "(?i)error",
            "error\n",
        ],
    )
    def test_rejects_unknown(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid level"):
            s._validate_level(value)

    def test_error_message_lists_valid_levels_sorted(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_level("TRACE")
        assert str(excinfo.value) == (
            "Invalid level: trace. Valid: all, critical, debug, error, info, warn, warning"
        )

    @pytest.mark.parametrize("value", [None, 3, 1.0, ["error"], b"error"])
    def test_rejects_non_string(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_level(value)  # type: ignore[arg-type]
        assert str(excinfo.value) == "level must be a string"


# =============================================================================
# _validate_limit
# =============================================================================


class TestValidateLimit:
    def test_max_limit_constant(self) -> None:
        assert s._MAX_LIMIT == 10000

    @pytest.mark.parametrize("value", [1, 2, 50, 100, 9999, 10000])
    def test_passes_through_in_range(self, value: int) -> None:
        assert s._validate_limit(value) == value

    @pytest.mark.parametrize("value", [0, -1, -100, -(10**9)])
    def test_clamps_below_range_to_one(self, value: int) -> None:
        """Out-of-range limits are clamped, not rejected — no exception."""
        assert s._validate_limit(value) == 1

    @pytest.mark.parametrize("value", [10001, 100000, 10**9, 10**100])
    def test_clamps_above_range_to_max(self, value: int) -> None:
        assert s._validate_limit(value) == s._MAX_LIMIT

    @pytest.mark.parametrize("value", ["100", None, 1.0, 100.5, ["100"], float("inf")])
    def test_rejects_non_integer(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_limit(value)  # type: ignore[arg-type]
        assert str(excinfo.value) == "limit must be an integer"

    def test_quirk_bools_are_accepted_as_ints(self) -> None:
        """QUIRK: ``bool`` subclasses ``int``, so True/False slip past the type
        check. True (==1) is returned unchanged; False (==0) is clamped to 1."""
        assert s._validate_limit(True) is True
        assert s._validate_limit(False) == 1


# =============================================================================
# _validate_id
# =============================================================================


class TestValidateId:
    def test_max_id_constant(self) -> None:
        assert s._MAX_ID == 2147483647

    def test_none_passes_through(self) -> None:
        """None means "no filter" and must survive validation."""
        assert s._validate_id(None, "experiment_id") is None

    @pytest.mark.parametrize("value", [1, 2, 15, 999999, 2147483647])
    def test_accepts_in_range(self, value: int) -> None:
        assert s._validate_id(value, "experiment_id") == value

    @pytest.mark.parametrize("value", [0, -1, -2147483648, 2147483648, 10**18])
    def test_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_id(value, "crawler_id")
        assert str(excinfo.value) == "crawler_id must be between 1 and 2147483647"

    @pytest.mark.parametrize("value", ["15", 1.0, 15.5, ["15"], {"id": 15}, b"15"])
    def test_rejects_non_integer(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_id(value, "job_id")  # type: ignore[arg-type]
        assert str(excinfo.value) == "job_id must be an integer"

    @pytest.mark.parametrize("name", ["experiment_id", "crawler_id", "queue_id", "job_id"])
    def test_parameter_name_is_reported(self, name: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_id(0, name)
        assert str(excinfo.value).startswith(f"{name} must be")

    def test_string_id_is_not_coerced(self) -> None:
        """A numeric string must raise rather than silently become an int."""
        with pytest.raises(ValueError, match="must be an integer"):
            s._validate_id("15", "experiment_id")  # type: ignore[arg-type]

    def test_quirk_bools_are_accepted_as_ints(self) -> None:
        """QUIRK: True (==1) validates as a legal id; False (==0) fails the range
        check rather than the type check."""
        assert s._validate_id(True, "experiment_id") is True
        with pytest.raises(ValueError, match="must be between 1 and"):
            s._validate_id(False, "experiment_id")


# =============================================================================
# _validate_search
# =============================================================================


class TestValidateSearch:
    def test_max_search_length_constant(self) -> None:
        assert s._MAX_SEARCH_LENGTH == 500

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "error",
            "job failed",
            "exp-15|cr-3|q-7|j-42",
            "  padded  ",
        ],
    )
    def test_passes_ordinary_strings_through_unchanged(self, value: str) -> None:
        assert s._validate_search(value) == value

    def test_strips_null_bytes_only(self) -> None:
        assert s._validate_search("a\x00b") == "ab"
        assert s._validate_search("\x00\x00error\x00") == "error"
        assert s._validate_search("\x00") == ""

    def test_does_not_strip_other_control_characters(self) -> None:
        assert s._validate_search("a\nb\tc\r") == "a\nb\tc\r"

    def test_accepts_exactly_max_length(self) -> None:
        value = "x" * s._MAX_SEARCH_LENGTH
        assert s._validate_search(value) == value

    def test_rejects_one_over_max_length(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_search("x" * (s._MAX_SEARCH_LENGTH + 1))
        assert str(excinfo.value) == "search string too long (max 500 characters)"

    def test_rejects_absurdly_long_string(self) -> None:
        with pytest.raises(ValueError, match="search string too long"):
            s._validate_search("x" * 100000)

    def test_quirk_length_is_checked_before_null_stripping(self) -> None:
        """QUIRK: null bytes count toward the length budget, so a payload that
        would be short enough after sanitisation is still rejected."""
        value = "x" * 400 + "\x00" * 200  # 600 raw chars, 400 after stripping
        with pytest.raises(ValueError, match="search string too long"):
            s._validate_search(value)

    @pytest.mark.parametrize(
        "value",
        [
            'error" or 1=1 --',
            '"} |= "password',
            "|~ `.*`",
            "\\x00",
            '{job="api"}',
            "'; DROP TABLE logs; --",
            "$(whoami)",
            "../../etc/passwd",
            "café über 中文 \U0001f6a8",
        ],
    )
    def test_injectionish_and_unicode_input_is_returned_verbatim(self, value: str) -> None:
        """_validate_search deliberately does NOT escape: LogQL escaping is the
        caller's job (``escape_logql_string``). Pin that contract so nobody
        assumes this helper sanitises metacharacters."""
        assert s._validate_search(value) == value

    def test_length_limit_counts_characters_not_bytes(self) -> None:
        value = "\U0001f6a8" * s._MAX_SEARCH_LENGTH  # 500 chars, 2000 UTF-8 bytes
        assert s._validate_search(value) == value

    @pytest.mark.parametrize("value", [None, 5, 1.0, ["error"], b"error", {"q": "error"}])
    def test_rejects_non_string(self, value: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_search(value)  # type: ignore[arg-type]
        assert str(excinfo.value) == "search must be a string"

    def test_non_string_check_precedes_length_check(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            s._validate_search(b"x" * 1000)  # type: ignore[arg-type]
        assert str(excinfo.value) == "search must be a string"


# =============================================================================
# _unwrap
# =============================================================================


class TestUnwrap:
    def test_unwraps_data_envelope(self) -> None:
        inner = {"id": 15, "name": "exp"}
        assert s._unwrap({"data": inner}) is inner

    def test_unwraps_list_payload(self) -> None:
        assert s._unwrap({"data": [1, 2, 3]}) == [1, 2, 3]

    def test_unwraps_scalar_payload(self) -> None:
        assert s._unwrap({"data": 42}) == 42
        assert s._unwrap({"data": "ok"}) == "ok"

    def test_plain_dict_passes_through(self) -> None:
        payload = {"id": 15, "name": "exp"}
        assert s._unwrap(payload) is payload

    def test_empty_dict_passes_through(self) -> None:
        assert s._unwrap({}) == {}

    def test_only_one_envelope_level_is_removed(self) -> None:
        assert s._unwrap({"data": {"data": {"id": 1}}}) == {"data": {"id": 1}}

    def test_sibling_keys_are_discarded_with_the_envelope(self) -> None:
        """Pagination metadata alongside ``data`` is dropped by design."""
        assert s._unwrap({"data": [1], "meta": {"total": 1}}) == [1]

    def test_list_input_passes_through(self) -> None:
        payload = [{"id": 1}, {"id": 2}]
        assert s._unwrap(payload) is payload  # type: ignore[arg-type]

    def test_none_passes_through(self) -> None:
        assert s._unwrap(None) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["raw", 7, 0, False, ()])
    def test_non_dict_passes_through(self, value: object) -> None:
        assert s._unwrap(value) == value  # type: ignore[arg-type]

    def test_quirk_null_data_yields_none(self) -> None:
        """QUIRK: an explicit ``{"data": null}`` returns None rather than the
        envelope, so callers doing ``_unwrap(r).get(...)`` would raise."""
        assert s._unwrap({"data": None}) is None

    def test_falsy_data_is_not_replaced_by_the_envelope(self) -> None:
        """``.get(key, default)`` semantics: an empty list stays an empty list."""
        assert s._unwrap({"data": []}) == []
        assert s._unwrap({"data": {}}) == {}


# =============================================================================
# _valid_job_api_types
# =============================================================================


class TestValidJobApiTypes:
    def test_returns_registry_api_types(self) -> None:
        from experiment_designer.spec.job_registry import JobRegistry

        result = s._valid_job_api_types()
        expected = [j.api_type for j in JobRegistry.list_all()]
        assert result == expected
        assert len(result) == len(expected) > 0

    def test_contains_known_api_types(self) -> None:
        result = s._valid_job_api_types()
        for api_type in ("visitjob", "waitjob", "clickjob", "lambdasingleselectjob"):
            assert api_type in result

    def test_returns_api_types_not_yaml_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The API enum values (``visitjob``) are returned, not the YAML DSL
        names (``visit``)."""
        from experiment_designer.spec.job_registry import JobRegistry

        fake = [
            types.SimpleNamespace(yaml_name="visit", api_type="visitjob"),
            types.SimpleNamespace(yaml_name="wait", api_type="waitjob"),
        ]
        monkeypatch.setattr(JobRegistry, "list_all", classmethod(lambda cls: fake))
        assert s._valid_job_api_types() == ["visitjob", "waitjob"]

    def test_all_entries_are_lowercase_strings(self) -> None:
        result = s._valid_job_api_types()
        assert all(isinstance(t, str) and t == t.lower() and t for t in result)

    def test_returns_empty_list_when_import_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blocking the module in sys.modules makes the inner import raise
        ImportError, which the helper swallows."""
        monkeypatch.setitem(sys.modules, "experiment_designer.spec.job_registry", None)
        assert s._valid_job_api_types() == []

    def test_returns_empty_list_when_registry_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from experiment_designer.spec.job_registry import JobRegistry

        def boom(cls):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(JobRegistry, "list_all", classmethod(boom))
        assert s._valid_job_api_types() == []

    def test_registry_is_restored_after_monkeypatching(self) -> None:
        """Guard against a leaked patch making later tests meaningless."""
        assert len(s._valid_job_api_types()) > 2


# =============================================================================
# _get_mcp_config
# =============================================================================


@pytest.fixture
def clean_mcp_env(monkeypatch: pytest.MonkeyPatch) -> Generator[pytest.MonkeyPatch, None, None]:
    """Neutral argv/env baseline so ambient settings cannot skew the assertions."""
    for var in ("MCP_TRANSPORT", "MCP_PORT", "MCP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["mcp-server"])
    yield monkeypatch


class TestGetMcpConfig:
    def test_defaults_are_stdio_and_loopback(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        assert s._get_mcp_config() == ("stdio", "127.0.0.1", 8100)

    def test_http_flag_switches_transport(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--http"])
        transport, host, port = s._get_mcp_config()
        assert transport == "streamable-http"
        assert (host, port) == ("127.0.0.1", 8100)

    def test_transport_env_var_switches_transport(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setenv("MCP_TRANSPORT", "http")
        assert s._get_mcp_config()[0] == "streamable-http"

    @pytest.mark.parametrize("value", ["stdio", "HTTP", "streamable-http", "", "sse"])
    def test_transport_env_var_only_matches_exact_http(
        self, clean_mcp_env: pytest.MonkeyPatch, value: str
    ) -> None:
        """Only the literal string "http" flips the transport."""
        clean_mcp_env.setenv("MCP_TRANSPORT", value)
        assert s._get_mcp_config()[0] == "stdio"

    def test_port_flag(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--http", "--port", "9000"])
        assert s._get_mcp_config() == ("streamable-http", "127.0.0.1", 9000)

    @pytest.mark.parametrize("bad", ["abc", "", "80.5", "-"])
    def test_non_numeric_port_flag_falls_back_to_default(
        self, clean_mcp_env: pytest.MonkeyPatch, bad: str
    ) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--port", bad])
        assert s._get_mcp_config()[2] == 8100

    def test_trailing_port_flag_falls_back_to_default(
        self, clean_mcp_env: pytest.MonkeyPatch
    ) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--port"])
        assert s._get_mcp_config()[2] == 8100

    def test_port_env_var(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setenv("MCP_PORT", "7000")
        assert s._get_mcp_config()[2] == 7000

    def test_port_env_var_overrides_port_flag(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        """Env is applied after argv, so it wins — worth pinning because the
        opposite precedence is the more common convention."""
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--port", "9000"])
        clean_mcp_env.setenv("MCP_PORT", "7000")
        assert s._get_mcp_config()[2] == 7000

    def test_invalid_port_env_var_keeps_flag_value(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--port", "9000"])
        clean_mcp_env.setenv("MCP_PORT", "not-a-port")
        assert s._get_mcp_config()[2] == 9000

    def test_empty_port_env_var_is_ignored(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setenv("MCP_PORT", "")
        assert s._get_mcp_config()[2] == 8100

    def test_negative_and_huge_ports_are_not_range_checked(
        self, clean_mcp_env: pytest.MonkeyPatch
    ) -> None:
        """QUIRK: no port range validation — the failure surfaces later at bind."""
        clean_mcp_env.setenv("MCP_PORT", "-1")
        assert s._get_mcp_config()[2] == -1
        clean_mcp_env.setenv("MCP_PORT", "999999")
        assert s._get_mcp_config()[2] == 999999

    def test_host_flag_allows_external_bind(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--http", "--host", "0.0.0.0"])
        assert s._get_mcp_config() == ("streamable-http", "0.0.0.0", 8100)

    def test_trailing_host_flag_keeps_loopback_default(
        self, clean_mcp_env: pytest.MonkeyPatch
    ) -> None:
        """Security-relevant: a malformed --host must never widen the bind."""
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--host"])
        assert s._get_mcp_config()[1] == "127.0.0.1"

    def test_host_env_var(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setenv("MCP_HOST", "10.0.0.5")
        assert s._get_mcp_config()[1] == "10.0.0.5"

    def test_host_env_var_overrides_host_flag(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--host", "192.168.1.1"])
        clean_mcp_env.setenv("MCP_HOST", "10.0.0.5")
        assert s._get_mcp_config()[1] == "10.0.0.5"

    def test_empty_host_env_var_is_ignored(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setenv("MCP_HOST", "")
        assert s._get_mcp_config()[1] == "127.0.0.1"

    def test_quirk_host_flag_swallows_a_following_flag(
        self, clean_mcp_env: pytest.MonkeyPatch
    ) -> None:
        """QUIRK: the next argv token is taken verbatim, even when it is another
        flag, so ``--host --http`` binds to the literal host "--http"."""
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--host", "--http"])
        transport, host, _port = s._get_mcp_config()
        assert host == "--http"
        assert transport == "streamable-http"  # --http still counted as present

    def test_all_options_combined(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(
            sys, "argv", ["mcp-server", "--http", "--host", "0.0.0.0", "--port", "8123"]
        )
        assert s._get_mcp_config() == ("streamable-http", "0.0.0.0", 8123)

    def test_is_pure_and_repeatable(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["mcp-server", "--http", "--port", "9001"])
        assert s._get_mcp_config() == s._get_mcp_config()

    def test_unrelated_argv_is_ignored(self, clean_mcp_env: pytest.MonkeyPatch) -> None:
        clean_mcp_env.setattr(sys, "argv", ["pytest", "-q", "--tb=short", "orchestrator/tests"])
        assert s._get_mcp_config() == ("stdio", "127.0.0.1", 8100)

    def test_module_level_config_is_resolved_at_import(self) -> None:
        """The module calls the helper once at import time and binds the result."""
        assert s._transport in ("stdio", "streamable-http")
        assert isinstance(s._port, int)
        if not os.environ.get("MCP_HOST") and "--host" not in sys.argv:
            # Without an explicit override the import-time bind must be loopback.
            assert s._host == "127.0.0.1"
