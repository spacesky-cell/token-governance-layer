import json
import re
from pathlib import Path

import pytest

import token_governance.contracts as contracts
from token_governance.config import (
    ConfigError,
    GatewayBackendConfig,
    GatewayConfig,
    GatewayToolPolicyConfig,
    GovernanceConfig,
    PolicyConfig,
    load_config,
)
from token_governance.contracts import (
    PersistenceMode,
    ProtectedContentBehavior,
    Strategy,
)


ROOT = Path(__file__).resolve().parents[1]


def write_config(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_defaults_are_typed_and_resolve_from_config_directory(tmp_path):
    config_path = write_config(tmp_path / "token-governance.config.json", {})

    config = GovernanceConfig.load(config_path)

    assert config.ledger.path == (tmp_path / ".tgl" / "ledger.sqlite").resolve()
    assert config.ledger.retention_days == 30
    assert config.policy.enabled_strategies == (
        Strategy.TEST_OUTPUT,
        Strategy.BUILD_OUTPUT,
        Strategy.REPETITIVE_LOG,
    )
    assert config.policy.protected_content_behavior is ProtectedContentBehavior.PASSTHROUGH
    assert config.policy.max_payload_bytes == 2 * 1024 * 1024
    assert config.policy.max_stored_original_bytes == 1024 * 1024
    assert config.policy.hook_deadline_ms == 2000
    assert config.gateway.request_timeout_seconds == 10


def test_strategy_recognition_order_has_one_contract_source():
    assert contracts.STRATEGY_RECOGNITION_ORDER == (
        Strategy.TEST_OUTPUT,
        Strategy.BUILD_OUTPUT,
        Strategy.REPETITIVE_LOG,
    )
    assert not hasattr(contracts, "BUILT_IN_STRATEGIES")


def test_loads_supported_config_and_example_file(tmp_path):
    config_path = write_config(
        tmp_path / "token-governance.config.json",
        {
            "ledger": {"path": "state/custom.sqlite", "retention_days": None},
            "policy": {
                "enabled_strategies": ["test_output", "repetitive_log"],
                "protected_content_behavior": "passthrough",
                "max_payload_bytes": 4096,
                "max_stored_original_bytes": 2048,
                "hook_deadline_ms": 750,
            },
            "gateway": {
                "request_timeout_seconds": 12,
                "tool_policy": {"allow": ["code::search"], "deny": []},
                "backends": [
                    {"name": "code", "command": "python", "args": ["server.py"]}
                ],
            },
        },
    )

    config = GovernanceConfig.load(config_path)

    assert config.ledger.path == (tmp_path / "state" / "custom.sqlite").resolve()
    assert config.ledger.retention_days is None
    assert config.policy.enabled_strategies == (
        Strategy.TEST_OUTPUT,
        Strategy.REPETITIVE_LOG,
    )
    assert config.gateway.request_timeout_seconds == 12
    assert config.gateway.backends[0].args == ("server.py",)
    assert config.gateway.tool_policy.allow == ("code::search",)
    example = GovernanceConfig.load(ROOT / "token-governance.config.example.json")
    assert example.policy.enabled_strategies == contracts.STRATEGY_RECOGNITION_ORDER
    assert example.gateway.request_timeout_seconds == 10


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"extra": True}, "Unknown config field: extra"),
        (
            {"ledger": {"path": "ledger.sqlite", "surprise": True}},
            "Unknown config field: ledger.surprise",
        ),
        (
            {"policy": {"unknown": True}},
            "Unknown config field: policy.unknown",
        ),
        (
            {"gateway": {"tool_policy": {"other": []}}},
            "Unknown config field: gateway.tool_policy.other",
        ),
        (
            {"gateway": {"backends": [{"name": "x", "command": "x", "other": 1}]}},
            "Unknown config field: gateway.backends[0].other",
        ),
    ],
)
def test_rejects_unknown_fields_with_fixed_errors(tmp_path, value, message):
    config_path = write_config(tmp_path / "config.json", value)

    with pytest.raises(ConfigError, match=f"^{re.escape(message)}$"):
        GovernanceConfig.load(config_path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("policy", "max_payload_bytes", 0),
        ("policy", "max_payload_bytes", 16 * 1024 * 1024 + 1),
        ("policy", "max_stored_original_bytes", 0),
        ("policy", "hook_deadline_ms", 0),
        ("policy", "hook_deadline_ms", 60_001),
        ("ledger", "retention_days", 0),
        ("ledger", "retention_days", 3651),
        ("gateway", "request_timeout_seconds", 0),
        ("gateway", "request_timeout_seconds", 121),
    ],
)
def test_rejects_out_of_range_numbers(tmp_path, section, field, value):
    config_path = write_config(tmp_path / "config.json", {section: {field: value}})

    with pytest.raises(ConfigError, match=rf"^Invalid config value: {section}\.{field}$"):
        GovernanceConfig.load(config_path)


def test_rejects_stored_original_limit_above_payload_limit(tmp_path):
    config_path = write_config(
        tmp_path / "config.json",
        {
            "policy": {
                "max_payload_bytes": 1024,
                "max_stored_original_bytes": 2048,
            }
        },
    )

    with pytest.raises(
        ConfigError,
        match=r"^Invalid config value: policy\.max_stored_original_bytes$",
    ):
        GovernanceConfig.load(config_path)


def test_rejects_unregistered_and_passthrough_strategies(tmp_path):
    for strategy in ("head_tail", "passthrough", "custom_strategy"):
        config_path = write_config(
            tmp_path / f"{strategy}.json",
            {"policy": {"enabled_strategies": [strategy]}},
        )

        with pytest.raises(
            ConfigError,
            match=r"^Invalid config value: policy\.enabled_strategies$",
        ):
            GovernanceConfig.load(config_path)


def test_relative_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config_path = write_config(
        project / "config.json",
        {"ledger": {"path": "data/ledger.sqlite"}},
    )
    monkeypatch.chdir(elsewhere)

    config = GovernanceConfig.load(config_path)

    assert config.ledger.path == (project / "data" / "ledger.sqlite").resolve()


def test_relative_backend_command_resolves_from_config_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config_path = write_config(
        project / "config.json",
        {
            "gateway": {
                "backends": [
                    {
                        "name": "local",
                        "command": "./backend/server.py",
                        "args": [],
                    }
                ]
            }
        },
    )
    monkeypatch.chdir(elsewhere)

    config = GovernanceConfig.load(config_path)

    assert config.gateway.backends[0].command == str(
        (project / "backend" / "server.py").resolve()
    )


def test_backend_args_preserve_order_and_allow_repeated_flags(tmp_path):
    args = ["--header", "A", "--header", "B"]
    config_path = write_config(
        tmp_path / "config.json",
        {
            "gateway": {
                "backends": [
                    {
                        "name": "docs",
                        "command": "docs-server",
                        "args": args,
                    }
                ]
            }
        },
    )

    config = GovernanceConfig.load(config_path)

    assert config.gateway.backends[0].args == tuple(args)


def test_direct_backend_config_allows_repeated_args():
    args = ["--header", "A", "--header", "B"]

    backend = GatewayBackendConfig(
        name="docs",
        command="docs-server",
        args=args,
    )

    assert backend.args == tuple(args)


@pytest.mark.parametrize("field", ["allow", "deny"])
def test_gateway_tool_policy_rejects_repeated_entries(tmp_path, field):
    config_path = write_config(
        tmp_path / "config.json",
        {
            "gateway": {
                "tool_policy": {
                    field: ["docs::search", "docs::search"],
                }
            }
        },
    )

    with pytest.raises(
        ConfigError,
        match=rf"^Invalid config value: gateway\.tool_policy\.{field}$",
    ):
        GovernanceConfig.load(config_path)


@pytest.mark.parametrize("field", ["allow", "deny"])
def test_direct_gateway_tool_policy_rejects_repeated_entries(field):
    values = {"allow": [], "deny": []}
    values[field] = ["docs::search", "docs::search"]

    with pytest.raises(ValueError, match=rf"^{field} must not contain duplicates$"):
        GatewayToolPolicyConfig(**values)


def test_cli_overrides_explicit_config_and_defaults(tmp_path):
    config_path = write_config(
        tmp_path / "config.json",
        {
            "ledger": {"path": "configured.sqlite"},
            "policy": {"hook_deadline_ms": 1500},
        },
    )

    config = GovernanceConfig.load(
        config_path,
        cli_overrides={
            "ledger": {"path": "override.sqlite"},
            "policy": {
                "max_payload_bytes": 8192,
                "max_stored_original_bytes": 4096,
            },
        },
    )

    assert config.ledger.path == (tmp_path / "override.sqlite").resolve()
    assert config.policy.hook_deadline_ms == 1500
    assert config.policy.max_payload_bytes == 8192
    assert config.policy.max_stored_original_bytes == 4096


def test_load_config_wrapper_has_parser_parity(tmp_path):
    config_path = write_config(
        tmp_path / "config.json",
        {
            "ledger": {"path": "configured.sqlite", "retention_days": 7},
            "policy": {"enabled_strategies": ["build_output"]},
        },
    )
    overrides = {"ledger": {"path": "override.sqlite"}}

    assert load_config(config_path, cli_overrides=overrides) == GovernanceConfig.load(
        config_path,
        cli_overrides=overrides,
    )

    invalid_path = write_config(tmp_path / "invalid.json", {"unknown": True})
    with pytest.raises(ConfigError) as wrapper_error:
        load_config(invalid_path)
    with pytest.raises(ConfigError) as classmethod_error:
        GovernanceConfig.load(invalid_path)
    assert str(wrapper_error.value) == str(classmethod_error.value)


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"policy": {}, "policy": {}}',
        '{"policy": {"hook_deadline_ms": 1000, "hook_deadline_ms": 2000}}',
    ],
)
def test_rejects_duplicate_json_keys_at_any_depth_with_fixed_error(
    tmp_path,
    raw_json,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(raw_json, encoding="utf-8")

    with pytest.raises(ConfigError, match="^Duplicate config key$"):
        GovernanceConfig.load(config_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": {}, 1: {}},
        {"policy": {"unknown": True, 1: True}},
    ],
)
def test_rejects_non_string_mapping_keys_with_fixed_error(tmp_path, overrides):
    config_path = write_config(tmp_path / "config.json", {})

    with pytest.raises(ConfigError, match="^Config object keys must be strings$"):
        GovernanceConfig.load(config_path, cli_overrides=overrides)


def test_direct_config_construction_copies_collection_fields_to_tuples(tmp_path):
    strategies = [Strategy.TEST_OUTPUT]
    markers = ["PRIVATE_MARKER"]
    args = ["--serve"]
    allow = ["code::search"]
    deny = ["code::delete"]
    backend = GatewayBackendConfig(name="code", command="python", args=args)
    backends = [backend]

    policy = PolicyConfig(
        enabled_strategies=strategies,
        protected_content_behavior=ProtectedContentBehavior.PASSTHROUGH,
        persistence_mode=PersistenceMode.TRANSFORMED_ONLY,
        max_payload_bytes=4096,
        max_stored_original_bytes=2048,
        hook_deadline_ms=1000,
        literal_secret_markers=markers,
    )
    tool_policy = GatewayToolPolicyConfig(allow=allow, deny=deny)
    gateway = GatewayConfig(
        request_timeout_seconds=10,
        backends=backends,
        tool_policy=tool_policy,
    )

    strategies.append(Strategy.BUILD_OUTPUT)
    markers.append("LATE_MARKER")
    args.append("--late")
    allow.append("code::late")
    deny.append("code::late")
    backends.clear()

    assert policy.enabled_strategies == (Strategy.TEST_OUTPUT,)
    assert policy.literal_secret_markers == ("PRIVATE_MARKER",)
    assert backend.args == ("--serve",)
    assert tool_policy.allow == ("code::search",)
    assert tool_policy.deny == ("code::delete",)
    assert gateway.backends == (backend,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled_strategies": ["test_output"]},
        {"protected_content_behavior": "passthrough"},
        {"persistence_mode": "transformed_only"},
    ],
)
def test_direct_policy_config_rejects_free_form_enum_values(overrides):
    values = {
        "enabled_strategies": [Strategy.TEST_OUTPUT],
        "protected_content_behavior": ProtectedContentBehavior.PASSTHROUGH,
        "persistence_mode": PersistenceMode.TRANSFORMED_ONLY,
        "max_payload_bytes": 4096,
        "max_stored_original_bytes": 2048,
        "hook_deadline_ms": 1000,
        "literal_secret_markers": [],
    }
    values.update(overrides)

    with pytest.raises(TypeError):
        PolicyConfig(**values)
