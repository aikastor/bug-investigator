# Handoff — verify an AI-generated fix for a reproduced Docker incident

**Deliverable:** Extend the existing LangGraph workflow so one failed booking produces a report with a candidate diff and a before-and-after Docker test. The demo deadline is **23:59 on 24 November 2026, Bishkek time**. Prioritize the two-seat booking case.

## Where to start

The workspace has two sibling folders. `faulty-app/` contains The Lantern seat-booking app and its Playwright tests. `bug-investigator/` contains the Docker watcher, SQLite jobs, GPT-6.1 Sol graph and HTML reports. A failed checkout automatically starts an investigation. The reporter already displays the diagnosis, cited source, tool calls and baseline observation.

In the verified pricing run, the model selected `two_seat_fee`. An isolated browser selected A1 and A2, saw **$28**, submitted the booking and received **HTTP 409**. The expected total is **$26**: two $12 tickets plus one $2 fee. These seat IDs describe the controlled replay. The original trace did not record the customer's seat selection. [Saved investigation](http://127.0.0.1:8174/investigations/1350b9e7347923997e51b536b60096a41d766d905b2d5565d6fa61b69f3df659)

| Entry point | Why it matters |
| --- | --- |
| [graph.py](../investigator/graph.py) | Start at `verify_baseline`. Its outgoing edge currently ends the run. |
| [baseline.py](../investigator/baseline.py) | Runs the configured test on a private Docker network and records the app and runner image IDs. It currently selects the app image by inspecting the watched container. |
| [targets.toml](../targets.toml) | Owns the target-specific test command. Keep seat names, prices and frontend details in the target and its tests. |
| [seats.test.js](../../faulty-app/tests/browser/seats.test.js) | Contains the two-seat regression, single-seat controls and two other deliberately failing scenarios. |
| [jobs.py](../investigator/jobs.py), [report.html](../investigator/templates/report.html) | Persist the new outcome alongside the existing diagnosis and display it in the same report. |

## Implementation task

1. Route a **reproduced** baseline to a patch-proposal node. Give GPT the diagnosis, relevant source and observed assertion. Ask for a minimal unified diff. A passing or inconclusive baseline should end with its existing evidence.
2. Apply the diff to a disposable copy of the app. Keep the live demo available for further investigations. Check that the source used to build the candidate corresponds to the baseline. The graph currently reads a local checkout while the baseline runs a Docker image, and those can drift.
3. Build a candidate image and rerun the selected scenario with the **same test command, inputs and runner image ID**. Run the existing suite on both app images and compare individual outcomes. Save the diff, both observations, and any newly failing tests.
4. If the candidate fails a behavioral check, return its actual assertion to GPT for **one revision**. Record infrastructure failures as inconclusive. Release temporary Docker resources on success, failure and timeout.
5. Show the outcome near the top of the HTML report and in its JSON representation. Preserve the report when the patch fails so the reviewer can see why. Human approval, GitHub publication, Streamlit and LangSmith are separate follow-up tasks.

## Acceptance checks

| Scenario | Required evidence |
| --- | --- |
| Two-seat baseline | A1 + A2 displays $28 and checkout returns the incident's HTTP 409 price mismatch. |
| Two-seat candidate | The same test displays $26, submits the booking and sees a confirmation. |
| Existing controls | One seat still costs $14 and can be reserved. The patched suite introduces no new failures compared with baseline. C4 and D2 have known independent bugs. |
| Failed candidate or unavailable Docker runner | The report distinguishes a failed assertion from an inconclusive run and retains diagnostics. |
| End-to-end demo | A fresh checkout error creates one investigation whose report contains the diagnosis, candidate diff and verification result. Historical diagnosis-only reports still open. |

## Local checks

Run the investigator tests from the workspace root using the existing virtual environment:

```sh
cd bug-investigator
../.venv/bin/python -m unittest discover -s tests -v
```

To refresh the test image, run `docker compose --profile tests build browser-tests` from `faulty-app/`. To trigger the demo, open the [seat map](http://127.0.0.1:4173), select A1 and A2, click **Reserve seats**, then open the newest [investigation](http://127.0.0.1:8174/investigations). The worker already reads its local OpenAI credentials. Use a scripted model for routine tests and one live run for final verification.
