# Case Management Console — real-browser end-to-end drive (record mode)

This runbook reproduces, **through a real browser**, the same terminal outcome the
pytest test asserts:

> `tests/test_ui_service.py::test_case_management_recorded_migration_reaches_ready_with_all_real_checks`

It drives the **production** Agent UI (`serve_ui`) wrapped by
`tooling/e2e/record_mode_serve.py`, which swaps only the loopback Ollama client
for the recorded model double (`tests/ui_test_doubles.py::make_ollama_client_test_double`)
and keeps the **real** local Salesforce validation toolchain (pinned Jest, etc.).
No live model, no network, no org, no deployment.

The recorded Case candidate passes every required check on attempt 1, so **no
correction gate** appears.

## Prerequisites

- macOS host with `sandbox-exec` available (the Salesforce local checks run in a
  sandbox). If unavailable the run cannot execute the real checks.
- The controller-pinned Jest install present at `tooling/lwc-jest/node_modules`
  (the `salesforce-lwc-jest` / `salesforce-lwc-controller-jest` checks need it).
- `uv` for launching the server; `npx` for the Playwright CLI wrapper.
- Playwright CLI wrapper at
  `/Users/gurleen.singh/.codex/skills/playwright/scripts/playwright_cli.sh`.

## 1. Boot the record-mode server (background)

```bash
cd /Users/gurleen.singh/Documents/Codex/2026-07-30/thi/repo
uv run python tooling/e2e/record_mode_serve.py \
    --scenario-id case-management-console --port 8901
```

Wait until it prints (pick another port if 8901 is busy):

```
[record_mode_serve] scenario_id='case-management-console' model_id='qwen3:8b' ... (recorded double active; no live model, no network)
event=ui.server.ready host="127.0.0.1" port=8901
Agent UI available at http://127.0.0.1:8901/
```

Sanity check the scenario is surfaced by the API the UI renders from:

```bash
curl -s http://127.0.0.1:8901/api/scenarios | jq '.scenarios[].scenario_id'
# expect: "salesforce-vf-to-lwc", "case-management-console", "mulesoft-mule3-to-mule4"
```

## 2. Drive the browser

All commands are prefixed with:

```bash
PW=/Users/gurleen.singh/.codex/skills/playwright/scripts/playwright_cli.sh
SES="--session capstone-case-e2e"
```

The CLI `click`/`fill` `<target>` is an **element ref taken from the latest
`snapshot`** (refs like `e52` are re-numbered on every snapshot, so always
`snapshot` first and read the ref for the labelled control). The button/textbox
**accessible labels** below are stable; the refs shown are what this run used.

| # | Command | Real control label (getByRole locator the CLI ran) |
|---|---------|-----------------------------------------------------|
| 1 | `$PW $SES open http://127.0.0.1:8901/` | navigate |
| 2 | `$PW $SES snapshot` | confirm scenario buttons rendered from `GET /api/scenarios` |
| 3 | `$PW $SES click e52` | button **"SF Case Management Console"** → `getByRole('button', { name: 'SF Case Management Console' })`. Textbox auto-fills with the Case canonical request; `state.selectedScenarioId = case-management-console`. |
| 4 | `$PW $SES fill e56 "The Case Management Console scope looks right. I'm ready for the Controller's canonical launch gate."` | textbox **"Message for the local Architect model"** → `getByRole('textbox', { name: 'Message for the local' })` |
| 5 | `$PW $SES click e60` | button **"Send"** → `getByRole('button', { name: 'Send' })`. Opens the **"Start this migration?"** launch gate bound to *Case Management Console* (Source: `LegacyCaseManagementConsole.page + LegacyCaseManagementConsoleController.cls + LegacyCaseQueryService.cls`). |
| 6 | `$PW $SES click e161` | button **"Start migration"** → `getByRole('button', { name: 'Start migration' })`. Opens the manifest approval gate: 11-path manifest, 7 required local checks, reviewer ID pre-filled `capstone-author`. |
| 7 | `$PW $SES click e328` | button **"Approve & create candidate"** → `getByRole('button', { name: 'Approve & create candidate' })`. Runs the Engineer recorded output + the **real** local validator (all 7 checks). Takes several seconds. |
| 8 | `$PW $SES snapshot` | assert terminal disposition (see assertions below) — **no correction gate must appear** |
| 9 | `$PW $SES click e473` | button **"Request final review"** → `getByRole('button', { name: 'Request final review' })`. Opens the final-review decision gate, reviewer pre-filled `independent-reviewer`. |
| 10 | `$PW $SES click e679` | button **"Accept candidate"** → `getByRole('button', { name: 'Accept candidate' })` |
| 11 | `$PW $SES snapshot` | capture the accepted terminal state |

> Note on `aria-pressed`: both Salesforce scenario buttons show `[pressed]` after
> selecting either one — `app.js` sets `aria-pressed` by *platform*, not scenario.
> The actually-selected scenario is confirmed by the textbox content and by the
> launch/manifest gates naming *Case Management Console*.

## 3. Expected completion assertions (quote these from the final snapshot)

- Conversation status: `attempt 1 · workflow completed · disposition: ready for human review`
- Validator line: `Authoritative deterministic evidence · 7 passed, 0 failed, 0 unavailable`
- The 7 required Salesforce local checks all pass: `salesforce-candidate-contract`,
  `salesforce-dependency-closure`, `salesforce-toolchain-contract`,
  `salesforce-jest-sandbox-probe`, `salesforce-lwc-jest`,
  `salesforce-lwc-controller-jest`, `salesforce-workspace-fingerprint`.
- Candidate = 11 additive files (the `caseManagementConsole` LWC + `__tests__`,
  `CaseManagementConsoleController` + test + meta, `CaseManagementConsoleUser`
  permission set, `manifest/package.xml`). Legacy Apex/Visualforce preserved.
- Final review panel: `Designated reviewer: independent-reviewer`, `Status: accepted`;
  status line: `Final human review: accepted. External actions remain unauthorized.`
- Export controls present and **enabled** (not `[disabled]`):
  **"↓ Download review candidate"** and **"↳ Save review candidate"**.

If a correction gate appears, or any check reports `failed`/`unavailable`, or the
disposition is not `ready_for_human_review`, STOP — the run diverged; do not
treat it as success.

## 4. Cleanup

```bash
$PW $SES close
$PW $SES list                      # confirm no session named capstone-case-e2e remains

# Kill the server (uv wrapper + child python) and confirm the port is dead:
pkill -f "record_mode_serve.py --scenario-id case-management-console --port 8901"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8901/ || echo "port 8901 dead"
```

This drive touches only an isolated candidate workspace; `git status` for `src/`
and `tests/` must be unchanged by running it.
