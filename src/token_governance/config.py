from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    PersistenceMode,
    ProtectedContentBehavior,
    STRATEGY_RECOGNITION_ORDER,
    Strategy,
)


DEFAULT_LEDGER_PATH = Path(".tgl/ledger.sqlite")
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_STORED_ORIGINAL_BYTES = 1024 * 1024
DEFAULT_HOOK_DEADLINE_MS = 2000
DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS = 10
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_HOOK_DEADLINE_MS = 60_000
MAX_LITERAL_SECRET_MARKERS = 32
MAX_LITERAL_SECRET_MARKER_LENGTH = 256


class ConfigError(ValueError):
    pass


def _copy_strategy_tuple(value: Sequence[Strategy]) -> tuple[Strategy, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("enabled_strategies must be a sequence of Strategy values")
    copied = tuple(value)
    if not all(
        isinstance(item, Strategy) and item in STRATEGY_RECOGNITION_ORDER
        for item in copied
    ):
        raise TypeError("enabled_strategies must be a sequence of Strategy values")
    return copied


def _copy_string_tuple(name: str, value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    copied = tuple(value)
    if not all(isinstance(item, str) for item in copied):
        raise TypeError(f"{name} must be a sequence of strings")
    return copied


def _copy_unique_string_tuple(name: str, value: Sequence[str]) -> tuple[str, ...]:
    copied = _copy_string_tuple(name, value)
    if len(copied) != len(set(copied)):
        raise ValueError(f"{name} must not contain duplicates")
    return copied


@dataclass(frozen=True)
class LedgerConfig:
    path: Path
    retention_days: int | None


@dataclass(frozen=True)
class PolicyConfig:
    enabled_strategies: tuple[Strategy, ...]
    protected_content_behavior: ProtectedContentBehavior
    persistence_mode: PersistenceMode
    max_payload_bytes: int
    max_stored_original_bytes: int
    hook_deadline_ms: int
    literal_secret_markers: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "enabled_strategies",
            _copy_strategy_tuple(self.enabled_strategies),
        )
        if not isinstance(
            self.protected_content_behavior,
            ProtectedContentBehavior,
        ):
            raise TypeError(
                "protected_content_behavior must be a ProtectedContentBehavior"
            )
        if not isinstance(self.persistence_mode, PersistenceMode):
            raise TypeError("persistence_mode must be a PersistenceMode")
        object.__setattr__(
            self,
            "literal_secret_markers",
            _copy_string_tuple(
                "literal_secret_markers",
                self.literal_secret_markers,
            ),
        )


@dataclass(frozen=True)
class GatewayBackendConfig:
    name: str
    command: str
    args: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _copy_string_tuple("args", self.args))


@dataclass(frozen=True)
class GatewayToolPolicyConfig:
    allow: tuple[str, ...]
    deny: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allow",
            _copy_unique_string_tuple("allow", self.allow),
        )
        object.__setattr__(
            self,
            "deny",
            _copy_unique_string_tuple("deny", self.deny),
        )


@dataclass(frozen=True)
class GatewayConfig:
    request_timeout_seconds: int
    backends: tuple[GatewayBackendConfig, ...]
    tool_policy: GatewayToolPolicyConfig

    def __post_init__(self) -> None:
        if isinstance(self.backends, (str, bytes)) or not isinstance(
            self.backends,
            Sequence,
        ):
            raise TypeError("backends must be a sequence of GatewayBackendConfig values")
        backends = tuple(self.backends)
        if not all(isinstance(item, GatewayBackendConfig) for item in backends):
            raise TypeError("backends must be a sequence of GatewayBackendConfig values")
        object.__setattr__(self, "backends", backends)


@dataclass(frozen=True)
class GovernanceConfig:
    ledger: LedgerConfig
    policy: PolicyConfig
    gateway: GatewayConfig
    config_path: Path

    @classmethod
    def load(
        cls,
        config_path: str | Path,
        *,
        cli_overrides: Mapping[str, Any] | None = None,
    ) -> "GovernanceConfig":
        path = Path(config_path).expanduser().resolve()
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8-sig"),
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("Unable to load config file") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Config root must be an object")

        config_value = _copy_mapping(raw)
        _validate_schema(config_value)
        if cli_overrides is not None:
            if not isinstance(cli_overrides, Mapping):
                raise ConfigError("CLI overrides must be an object")
            override_value = _copy_mapping(cli_overrides)
            _validate_schema(override_value)
            config_value = _merge(config_value, override_value)

        return _parse_config(config_value, config_path=path)


def load_config(
    config_path: str | Path,
    *,
    cli_overrides: Mapping[str, Any] | None = None,
) -> GovernanceConfig:
    return GovernanceConfig.load(config_path, cli_overrides=cli_overrides)


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items()}


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConfigError("Duplicate config key")
        value[key] = item
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge(existing, value)
        else:
            merged[key] = value
    return merged


def _unknown_fields(value: Mapping[str, Any], allowed: set[str], prefix: str) -> None:
    if not all(isinstance(key, str) for key in value):
        raise ConfigError("Config object keys must be strings")
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise ConfigError(f"Unknown config field: {prefix}{unknown[0]}")


def _expect_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid config value: {path}")
    return value


def _validate_schema(value: dict[str, Any]) -> None:
    _unknown_fields(value, {"ledger", "policy", "gateway"}, "")
    if "ledger" in value:
        ledger = _expect_object(value["ledger"], "ledger")
        _unknown_fields(ledger, {"path", "retention_days"}, "ledger.")
    if "policy" in value:
        policy = _expect_object(value["policy"], "policy")
        _unknown_fields(
            policy,
            {
                "enabled_strategies",
                "protected_content_behavior",
                "persistence_mode",
                "max_payload_bytes",
                "max_stored_original_bytes",
                "hook_deadline_ms",
                "literal_secret_markers",
            },
            "policy.",
        )
    if "gateway" in value:
        gateway = _expect_object(value["gateway"], "gateway")
        _unknown_fields(
            gateway,
            {"request_timeout_seconds", "backends", "tool_policy"},
            "gateway.",
        )
        if "tool_policy" in gateway:
            tool_policy = _expect_object(gateway["tool_policy"], "gateway.tool_policy")
            _unknown_fields(tool_policy, {"allow", "deny"}, "gateway.tool_policy.")
        if "backends" in gateway:
            backends = gateway["backends"]
            if not isinstance(backends, list):
                raise ConfigError("Invalid config value: gateway.backends")
            for index, backend_value in enumerate(backends):
                backend = _expect_object(backend_value, f"gateway.backends[{index}]")
                _unknown_fields(
                    backend,
                    {"name", "command", "args"},
                    f"gateway.backends[{index}].",
                )


def _parse_config(value: dict[str, Any], *, config_path: Path) -> GovernanceConfig:
    config_dir = config_path.parent
    ledger_value = value.get("ledger", {})
    policy_value = value.get("policy", {})
    gateway_value = value.get("gateway", {})

    ledger_path = _parse_path(ledger_value.get("path", str(DEFAULT_LEDGER_PATH)), config_dir)
    retention_days = _parse_optional_bounded_int(
        ledger_value.get("retention_days", DEFAULT_RETENTION_DAYS),
        "ledger.retention_days",
        1,
        3650,
    )

    enabled_strategies = _parse_enabled_strategies(
        policy_value.get(
            "enabled_strategies",
            [item.value for item in STRATEGY_RECOGNITION_ORDER],
        )
    )
    protected_content_behavior = _parse_enum(
        policy_value.get("protected_content_behavior", ProtectedContentBehavior.PASSTHROUGH.value),
        ProtectedContentBehavior,
        "policy.protected_content_behavior",
    )
    persistence_mode = _parse_enum(
        policy_value.get("persistence_mode", PersistenceMode.TRANSFORMED_ONLY.value),
        PersistenceMode,
        "policy.persistence_mode",
    )
    max_payload_bytes = _parse_bounded_int(
        policy_value.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES),
        "policy.max_payload_bytes",
        1,
        MAX_PAYLOAD_BYTES,
    )
    max_stored_original_bytes = _parse_bounded_int(
        policy_value.get(
            "max_stored_original_bytes", DEFAULT_MAX_STORED_ORIGINAL_BYTES
        ),
        "policy.max_stored_original_bytes",
        1,
        MAX_PAYLOAD_BYTES,
    )
    if max_stored_original_bytes > max_payload_bytes:
        raise ConfigError("Invalid config value: policy.max_stored_original_bytes")
    hook_deadline_ms = _parse_bounded_int(
        policy_value.get("hook_deadline_ms", DEFAULT_HOOK_DEADLINE_MS),
        "policy.hook_deadline_ms",
        1,
        MAX_HOOK_DEADLINE_MS,
    )
    literal_secret_markers = _parse_literal_markers(
        policy_value.get("literal_secret_markers", [])
    )

    gateway = _parse_gateway(gateway_value, config_dir=config_dir)
    return GovernanceConfig(
        ledger=LedgerConfig(path=ledger_path, retention_days=retention_days),
        policy=PolicyConfig(
            enabled_strategies=enabled_strategies,
            protected_content_behavior=protected_content_behavior,
            persistence_mode=persistence_mode,
            max_payload_bytes=max_payload_bytes,
            max_stored_original_bytes=max_stored_original_bytes,
            hook_deadline_ms=hook_deadline_ms,
            literal_secret_markers=literal_secret_markers,
        ),
        gateway=gateway,
        config_path=config_path,
    )


def _parse_path(value: Any, config_dir: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Invalid config value: ledger.path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def _parse_bounded_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"Invalid config value: {path}")
    return value


def _parse_optional_bounded_int(
    value: Any, path: str, minimum: int, maximum: int
) -> int | None:
    if value is None:
        return None
    return _parse_bounded_int(value, path, minimum, maximum)


def _parse_enum(value: Any, enum_type: type[Any], path: str) -> Any:
    if not isinstance(value, str):
        raise ConfigError(f"Invalid config value: {path}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid config value: {path}") from exc


def _parse_enabled_strategies(value: Any) -> tuple[Strategy, ...]:
    if not isinstance(value, list):
        raise ConfigError("Invalid config value: policy.enabled_strategies")
    parsed: list[Strategy] = []
    for item in value:
        try:
            strategy = Strategy(item) if isinstance(item, str) else None
        except ValueError:
            strategy = None
        if strategy not in STRATEGY_RECOGNITION_ORDER or strategy in parsed:
            raise ConfigError("Invalid config value: policy.enabled_strategies")
        parsed.append(strategy)
    return tuple(parsed)


def _parse_literal_markers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LITERAL_SECRET_MARKERS:
        raise ConfigError("Invalid config value: policy.literal_secret_markers")
    markers: list[str] = []
    for marker in value:
        if (
            not isinstance(marker, str)
            or not marker
            or len(marker) > MAX_LITERAL_SECRET_MARKER_LENGTH
            or marker in markers
        ):
            raise ConfigError("Invalid config value: policy.literal_secret_markers")
        markers.append(marker)
    return tuple(markers)


def _parse_ordered_string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"Invalid config value: {path}")
    return tuple(value)


def _parse_unique_string_list(value: Any, path: str) -> tuple[str, ...]:
    parsed = _parse_ordered_string_list(value, path)
    if len(parsed) != len(set(parsed)):
        raise ConfigError(f"Invalid config value: {path}")
    return parsed


def _parse_gateway(value: dict[str, Any], *, config_dir: Path) -> GatewayConfig:
    request_timeout_seconds = _parse_bounded_int(
        value.get("request_timeout_seconds", DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS),
        "gateway.request_timeout_seconds",
        1,
        120,
    )
    raw_backends = value.get("backends", [])
    backends: list[GatewayBackendConfig] = []
    names: set[str] = set()
    for index, backend in enumerate(raw_backends):
        name = backend.get("name")
        command = backend.get("command")
        args = backend.get("args", [])
        if not isinstance(name, str) or not name or name in names:
            raise ConfigError(f"Invalid config value: gateway.backends[{index}].name")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"Invalid config value: gateway.backends[{index}].command")
        parsed_args = _parse_ordered_string_list(
            args,
            f"gateway.backends[{index}].args",
        )
        names.add(name)
        backends.append(
            GatewayBackendConfig(
                name=name,
                command=_resolve_backend_command(command, config_dir),
                args=parsed_args,
            )
        )

    raw_tool_policy = value.get("tool_policy", {})
    tool_policy = GatewayToolPolicyConfig(
        allow=_parse_unique_string_list(
            raw_tool_policy.get("allow", []),
            "gateway.tool_policy.allow",
        ),
        deny=_parse_unique_string_list(
            raw_tool_policy.get("deny", []),
            "gateway.tool_policy.deny",
        ),
    )
    return GatewayConfig(
        request_timeout_seconds=request_timeout_seconds,
        backends=tuple(backends),
        tool_policy=tool_policy,
    )


def _resolve_backend_command(command: str, config_dir: Path) -> str:
    if not (
        command.startswith((".", "~"))
        or "/" in command
        or "\\" in command
        or Path(command).is_absolute()
    ):
        return command
    path = Path(command).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return str(path.resolve())
