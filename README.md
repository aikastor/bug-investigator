# Bug investigator

The FastAPI collector watches Docker containers listed in [targets.toml](targets.toml). It stores browser errors and unsuccessful exits in SQLite and maps stack frames to the configured source checkout. The included target is the sibling [seat-booking app](https://github.com/aikastor/faulty-app).

The baseline rejects A1 and A2 at $28 with HTTP 409. The candidate confirms the same seats at $26 with HTTP 200 and confirms A1 at $14. The full suite improves from three passes to four out of six. C4 and D2 retain their independent failures. The human review in Streamlit opened [faulty-app PR #1](https://github.com/aikastor/faulty-app/pull/1) with the verified one-line fix.

## Run the demo

Install Git, Docker Compose, and Python 3.14. PR publication also requires the GitHub CLI. Clone both repositories into the same directory so the configured source path resolves.

```sh
git clone https://github.com/aikastor/faulty-app.git
git clone https://github.com/aikastor/bug-investigator.git
cd faulty-app
docker compose up -d --build buggy-react
docker compose --profile tests build browser-tests
cd ../bug-investigator
```

From the investigator directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn investigator.api:app --port 8174
```

In a second terminal, start the operator UI from the investigator directory:

```sh
.venv/bin/python -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8514
```

Open the [Streamlit review app](http://127.0.0.1:8514) to follow a job, compare the verified diff and tests, and decide whether to publish it. Set `INVESTIGATOR_API` on the Streamlit process or `INVESTIGATOR_REVIEW_UI` on the API process if you change the addresses.

Open the [API explorer](http://127.0.0.1:8174/docs). `GET /targets` shows collector status and `GET /incidents` returns the latest 100 records. The watcher polls every two seconds and scans up to 250 retained log lines from the current run. Keep exited containers until collection. Set `INVESTIGATOR_CONFIG` and `INVESTIGATOR_DB` to override the default paths. Run checks with `.venv/bin/python -m unittest discover -s tests`.

## Automatic LangGraph investigations

Each **newly collected** Docker incident is saved and queued in one SQLite transaction. The incident ID is the unique job ID. Old records already in the database are retained as history. Run the app and the collector with the same source version.

Put `OPENAI_API_KEY=your-key` in a private `.env` beside `targets.toml`, or export it before starting FastAPI. The worker checks the file on each poll. Jobs stay queued while no key is configured. Once a key is present, new incidents automatically send their trace and model-selected source excerpts to OpenAI using `gpt-6.1-sol`.

```mermaid
flowchart LR
  D[Docker incident] --> Q[SQLite queue]
  Q --> S[Snapshot local source]
  S --> A[Ask GPT-6.1]
  A --> T[Execute chosen tool]
  T -->|search or read result| A
  T -->|diagnosis with a matching test| V[Replay baseline in Docker]
  V -->|reproduced| P[Ask GPT for a source diff]
  P --> C[Build and test a disposable candidate]
  C -->|first behavioral failure| P
  C -->|verified| H[Human review in Streamlit]
  H -->|approve| G[Open GitHub pull request]
  H -->|reject| R[Save decision]
  C -->|failed or inconclusive| R
  V -->|passed or inconclusive| R
  T -->|no matching test or insufficient evidence| R
```

To try it, open the seat map, select A1 and A2, and reserve them. Open [investigations](http://127.0.0.1:8174/investigations) and select an incident to read its diagnosis and cited source lines. Expand the stack trace or tool calls for details. Use **View raw JSON** for the API representation. A supported diagnosis cites lines returned by `read_source`. The graph allows up to seven source-tool calls and one final response. It can finish early or report insufficient evidence. Provider failures are recorded on the job.

The worker claims one job at a time. Shutdown requeues active work. An expired lease lets a worker recover after a process crash. Recovery reruns the graph under the same investigation ID and may repeat a model request.

The source tools read a local in-memory snapshot of common code file types. They skip dependencies, hidden paths, symlinks and large files. The fingerprint identifies the snapshot used for a run. The configured GitHub repository supplies the publication base after approval.

### Docker baseline

For a diagnosed incident the model may select a test from the target’s configured cases. The runner accepts only those configured commands. Build the browser test image with `docker compose --profile tests build browser-tests` from the app folder. The worker runs the selected case against a private copy of the watched container’s **current image**, using image defaults and a temporary internal Docker network. No replay container is watched or exposed on a host port.

The booking test records its own seat inputs, expected and actual total, and checkout response. A matching error and assertion failure produce **Bug reproduced** in the report. A passing test or an inconclusive run is recorded separately. The image IDs and observations are available in the JSON result. The controlled replay cannot identify the original customer's selection.

### Automatic patch verification

A reproduced baseline on a target with `patch_sources` and a `suite_command` starts a GPT patch proposal. The prompt includes the diagnosis, inspected source lines, observation and failed assertion. Each candidate starts from a stopped copy of the baseline image ID. The worker checks the configured source files against their hashes in the diagnosis snapshot, applies the diff in a temporary directory and copies the changed files into that container. Docker commits its filesystem as a temporary local candidate image, preserving the baseline dependencies and runtime.

The selected test runs on the candidate with the baseline command, app URL and browser-runner image ID. A fresh app instance also runs the same full suite on each side. The target’s reporter emits one `TEST_RESULT` JSON record per test, a final `TEST_PLAN` count, and `BASELINE_RESULT` observations from the booking tests. Assertions count as behavioral failures. Browser startup errors, timeouts, source mismatches and incomplete suite results are inconclusive.

Verification requires the selected test to pass in both candidate runs and every previously passing suite test to keep passing. A behavioral failure sends the diff and actual assertions back to GPT for one revision against the original source. The worker removes temporary images, containers and networks after use.

Open the investigation to compare observations, suite results and proposed diffs. The JSON representation includes the original replay in `result.baseline`, the suite in `result.verification.baseline_suite` and candidate runs in `result.verification.attempts`. Historical records retain their original fields.


### Review and submit a fix

In Streamlit, select a verified investigation and inspect its diff and browser suite. The review graph pauses at a LangGraph interrupt. Its SQLite checkpoint survives a refresh or server restart. **Reject patch** records the decision. **Approve and open pull request** queues a background job for the repository configured in `targets.toml`. For a personal demo, set that entry to your own fork and sign in with `gh auth login` using an account with push access.

The publisher checks the GitHub base against the source fingerprint, applies the reviewed diff to a temporary clone, and opens a pull request with both checkout observations and the suite comparison. It uses a branch derived from the investigation and diff hash so retries can recover an existing branch or PR. A failed publication displays the error and a retry button. Merging is a separate GitHub action.

Approval also applies the exact verified changes as unstaged edits in the configured local checkout. The worker checks each affected file against the tested before and after hashes and preserves unrelated edits. A conflict appears in the report and can be retried from Streamlit. Previously approved reports offer **Apply approved fix locally**. The running demo continues serving its existing image. New investigations read the deployed application source even after the checkout has been fixed.

The JSON report exposes the local outcome in `result.local_patch` alongside the review decision and independent PR publication status. Run the API on loopback for this single-user demo.
