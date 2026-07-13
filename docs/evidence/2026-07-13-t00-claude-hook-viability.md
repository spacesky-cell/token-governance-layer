# T00 Claude Hook Replacement Viability

Date: 2026-07-13

## Result

`PASS`

Claude Code 2.1.206 accepted a Bash-shaped
`PostToolUse.hookSpecificOutput.updatedToolOutput`. The structured tool result
presented to the model contained the Token Governance receipt and excluded the
runtime-generated middle sentinel. The original output remained retrievable
from the temporary local ledger.

## Official Contract

The Claude Code Hooks reference documents that `updatedToolOutput` must match
the output object for the tool being replaced. For Bash, the required fields
are `stdout`, `stderr`, `interrupted`, and `isImage`.

Reference: <https://code.claude.com/docs/en/hooks> (accessed 2026-07-13,
verified against Claude Code 2.1.206).

Minimal contract excerpt recorded from the reference:

> `updatedToolOutput`: Replaces the tool's output in the conversation. The
> value must match the tool's output schema. Bash output contains `stdout`,
> `stderr`, `interrupted`, and `isImage`.

Observed adapter correction:

```json
{
  "stdout": "<governed output and receipt>",
  "stderr": "<original stderr>",
  "interrupted": false,
  "isImage": false
}
```

## Test-Driven Adapter Fix

Initial regression test:
`tests/test_claude_hook.py::test_post_tool_use_preserves_bash_output_shape`

Initial RED evidence before the adapter correction:

```text
TypeError: string indices must be integers
1 failed
```

Initial GREEN evidence after the shape correction:

```text
python -m pytest -q tests/test_claude_hook.py
7 passed
```

The code-quality review then identified three fail-open gaps: string-shaped
Bash aliases, stderr-only Bash results, and an unverified PowerShell schema.
The additional tests produced the expected RED:

```text
python -m pytest -q tests/test_claude_hook.py
3 failed, 6 passed
```

After the minimal pre-engine fail-open guards and Bash-only formatter change:

```text
python -m pytest -q tests/test_claude_hook.py
9 passed

python -m pytest -q
41 passed
```

## Real Session Evidence

The user authorized at most two minimal real Claude model calls.

### Call 1: Negative control

- Claude Code: 2.1.206
- Session SHA-256:
  `b39943096589145c0d210ac572eb46f8491ff96383064b5510f29960830bd684`
- Result: Hook executed, but the string-shaped replacement was ignored.
- Machine-visible `tool_result`: contained the original runtime sentinel and
  did not contain the receipt.
- Root cause: Bash replacement output did not match the documented Bash output
  object.

### Call 2: Corrected output shape

- Claude Code: 2.1.206
- Session SHA-256:
  `d4166494db3300232c0923300ffcdbe6f25d73f9bdb95262882db94b986258d3`
- Command tool invocation: `python generate.py`
- Generated sentinel length: 45 characters
- Receipt ID SHA-256:
  `bfbbde7a855c367194894e305b00afcc15bfc6104f86d0d9acf878ea97e3caea`
- Restored original length: 4091 characters

The prompt did not contain the sentinel. `generate.py` generated it at runtime,
wrote it to the temporary `.tgl/sentinel.txt`, and printed it at line 61 of 120.

Exact generator source:

```python
from pathlib import Path
from secrets import token_hex


sentinel = f"T00_SENTINEL_{token_hex(16)}"
state_dir = Path(".tgl")
state_dir.mkdir(exist_ok=True)
(state_dir / "sentinel.txt").write_text(sentinel, encoding="utf-8")

for index in range(120):
    if index == 60:
        print(sentinel)
    else:
        print(f"INFO repeated viability output {index % 3}")
```

The call was executed with project and user settings, one allowed Bash tool,
structured stream output, hook events, and a nominal `$0.25` budget limit. The
provider reported `$0.36515` and returned `error_max_budget_usd` after the model
had already emitted its final answer. This budget behavior is recorded as an
environment/provider anomaly; it does not affect the directly captured Hook or
tool-result evidence. No further Claude calls are authorized or required.

Exact successful-call command, with the temporary npm prefix prepended to
`PATH` and TLS bypass removed from the child environment:

```powershell
$env:PATH = '<repo>/.tgl/t00-viability/npm-prefix;' + $env:PATH
Remove-Item Env:NODE_TLS_REJECT_UNAUTHORIZED -ErrorAction SilentlyContinue
claude -p 'Use the Bash tool exactly once to run python generate.py in the current directory. After it finishes, answer only OK. Do not read files and do not call any other tool.' --output-format stream-json --verbose --include-hook-events --allowedTools Bash --permission-mode bypassPermissions --max-budget-usd 0.25 --setting-sources 'user,project' | Tee-Object -FilePath '../second-call.stream.jsonl'
```

The command ran with cwd `<repo>/.tgl/t00-viability/project`. `<repo>` is a
redaction of the machine-specific absolute checkout path, not an omitted
argument.

## Machine Assertions

The JSONL stream was parsed as structured events. The following assertions all
evaluated to `true`:

```json
{
  "hook_shape_is_object": true,
  "hook_stdout_has_receipt": true,
  "tool_result_has_receipt": true,
  "tool_result_has_receipt_id": true,
  "tool_result_excludes_sentinel": true,
  "restored_contains_sentinel": true
}
```

The redacted machine result is committed as
`docs/evidence/2026-07-13-t00-claude-hook-assertions.json`. Its stream digest
binds these assertions to the raw ignored JSONL used during verification.

Exact parser and retrieval command:

```powershell
$events = Get-Content '<repo>/.tgl/t00-viability/second-call.stream.jsonl' |
  ForEach-Object { $_ | ConvertFrom-Json }
$hook = $events | Where-Object {
  $_.type -eq 'system' -and $_.subtype -eq 'hook_response'
} | Select-Object -Last 1
$updated = (($hook.output | ConvertFrom-Json).hookSpecificOutput.updatedToolOutput)
$toolResult = $events.message.content |
  Where-Object { $_.type -eq 'tool_result' -and ([string]$_.content).Contains('[Token Governance Receipt]') } |
  Select-Object -First 1 -ExpandProperty content
$sentinel = (Get-Content -Raw '<repo>/.tgl/t00-viability/project/.tgl/sentinel.txt').Trim()
$receipt = [regex]::Match($toolResult, 'receipt_id:\s*(tgl_[0-9a-f]+)').Groups[1].Value
$restored = (& '<repo>/.tgl/t00-viability/npm-prefix/tgl.cmd' --db '<repo>/.tgl/t00-viability/project/.tgl/claude-ledger.sqlite' retrieve $receipt) -join "`n"

$updated -is [pscustomobject]
([string]$updated.stdout).Contains('[Token Governance Receipt]')
$toolResult.Contains('[Token Governance Receipt]')
-not $toolResult.Contains($sentinel)
$restored.Contains($sentinel)
```

Retrieval integrity evidence:

```text
receipt_id_sha256: bfbbde7a855c367194894e305b00afcc15bfc6104f86d0d9acf878ea97e3caea
restored_length: 4091
original_hash: 7c01e13bef7c87e06646806f954cc8d0026d21a8798647acb40248ae4f6c56bb
```

Evidence fields:

- `system.hook_response.output.hookSpecificOutput.updatedToolOutput` was an
  object with the Bash output fields.
- The following `user.message.content[].type == "tool_result"` contained
  `[Token Governance Receipt]` and `receipt_id:`.
- That tool result did not contain the runtime sentinel.
- `tgl --db <temporary-ledger> retrieve <receipt-id>` returned the exact
  original containing the sentinel.

## Temporary Artifacts

Raw stream output, the temporary npm prefix, temporary project, sentinel,
ledger, and packed 0.1.0 tarball were created under ignored
`.tgl/t00-viability/`. After their hashes and redacted assertions were recorded,
the directory was recursively deleted. A process query confirmed there was no
remaining process whose command line referenced the temporary path.

No global Claude settings were modified. TLS verification was enabled for the
successful call by removing `NODE_TLS_REJECT_UNAUTHORIZED` from the child
process environment.
