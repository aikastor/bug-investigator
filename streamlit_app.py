"""Local operator UI for the Bug Investigator API."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import streamlit as st

API = os.environ.get("INVESTIGATOR_API", "http://127.0.0.1:8174").rstrip("/")
st.set_page_config(page_title="Bug Investigator", layout="centered")


def api(path, *, method="GET"):
    request = Request(API + path, method=method, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        st.error(f"Investigator returned HTTP {exc.code}. " + exc.read(600).decode(errors="replace"))
    except (URLError, TimeoutError, ValueError) as exc:
        st.error(f"Could not read the investigator response. {exc}")
    st.stop()


st.title("Bug Investigator")
jobs = api("/investigations")
if not jobs:
    st.info("Waiting for the first Docker incident.")
    st.button("Refresh investigations")
    st.stop()

by_id = {item["incident_id"]: item for item in jobs}
requested = st.query_params.get("investigation")
ids = list(by_id)
key = st.selectbox("Investigation", ids, index=ids.index(requested) if requested in by_id else 0,
                   format_func=lambda i: f"{by_id[i].get('message') or by_id[i]['status']} · {i[:8]}")
path = "/investigations/" + quote(key, safe="")


@st.fragment(run_every=4)
def report():
    data = api(path)
    result = data.get("result") or {}
    trace = data.get("trace") or {}
    if trace.get("url"):
        st.link_button("Open LangSmith trace", trace["url"])
    elif trace.get("status") == "unavailable":
        st.caption(f"LangSmith export unavailable · execution {trace['execution_id']}")
    usage = data.get("usage")
    if usage:
        counts = [f"{label} {usage[field] if usage[field] is not None else 'unknown'}"
                  for label, field in [("Input", "input_tokens"), ("Output", "output_tokens"), ("Total", "total_tokens")]]
        st.caption("Tokens · " + " · ".join(counts) + f" · {usage['responses']} model responses")
    if not result:
        st.subheader(data["status"].capitalize())
        calls = data.get("tool_calls", [])
        if calls:
            st.write("Latest tool", calls[-1]["tool"])
        if data.get("error"):
            st.error(data["error"])
        return
    review = result.get("review") or {}
    verification = result.get("verification") or {}
    st.subheader("Patch review" if verification else "Investigation result")
    st.caption(" · ".join(filter(None, [verification.get("outcome"), review.get("status")])))
    st.write(result["summary"].split("\n")[0])
    attempts = verification.get("attempts", [])
    if attempts:
        attempt = attempts[-1]
        left, right = st.columns(2)
        for column, label, run in [(left, "Baseline", result.get("baseline", {})),
                                   (right, "Candidate", attempt.get("candidate", {}))]:
            observed = run.get("observation", {})
            with column:
                st.markdown(f"**{label}**")
                st.write(" · ".join(str(value) for value in [observed.get("actual"),
                         f"HTTP {observed['http_status']}" if "http_status" in observed else None,
                         run.get("outcome")] if value is not None))
        st.code(attempt["diff"], language="diff")
        suite = attempt.get("suite", {}).get("tests", {})
        if suite:
            before = verification["baseline_suite"]["tests"]
            st.table([{"Test": name, "Baseline": before.get(name, {}).get("outcome", "unknown"),
                       "Candidate": test["outcome"]} for name, test in suite.items()])
            with st.expander("Booking observations"):
                for observation in attempt["suite"].get("observations", []):
                    st.write(observation.get("inputs", observation.get("scenario", "")),
                             observation.get("actual", ""), observation.get("feedback", ""))
        if attempt.get("detail"):
            st.code(attempt["detail"], language=None)
    publication = data.get("publication") or {}
    if review.get("status") == "pending":
        repo = data.get("github_target")
        st.write("Approval applies the verified diff to the target’s local source checkout.")
        if repo:
            st.write(f"Approval opens a pull request in **{repo['repo']}** against `{repo['base']}`.")
        approve, reject = st.columns(2)
        choice = None
        if approve.button("Approve and open pull request" if repo else "Approve patch",
                          key=f"approve-{key}", type="primary"):
            choice = "approved"
        if reject.button("Reject patch", key=f"reject-{key}"):
            choice = "rejected"
        if choice:
            api(f"{path}/review/{review['token']}/{choice}", method="POST")
            st.rerun()
    elif review.get("decision"):
        st.write(f"Review {review['decision']} · {review['decided_at']}")
    if review.get("decision") == "approved":
        local = result.get("local_patch") or {}
        if local.get("status") == "applied":
            st.write("Applied locally · " + ", ".join(local["files"]))
        elif local.get("status") == "queued":
            st.info("Local change queued")
        else:
            if local.get("error"):
                st.error(local["error"])
            if st.button("Apply approved fix locally", key=f"local-{key}"):
                api(f"{path}/review/{review['token']}/approved", method="POST")
                st.rerun()
    if publication.get("url"):
        st.link_button("Open GitHub pull request", publication["url"])
    elif publication.get("status") == "failed":
        st.error(publication["error"])
        if st.button("Retry publication", key=f"retry-{key}"):
            api(f"{path}/publication/{review['token']}/retry", method="POST")
            st.rerun()
    elif publication:
        st.info("Pull request " + publication["status"])
    st.link_button("Full investigation report", API + path)


report()
