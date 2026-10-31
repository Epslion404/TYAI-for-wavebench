"""Offline collection tests: subprocess fixtures never invoke GitHub or hardware."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


# Load directly, so the hyphenated tool directory needs no package/install step.
MODULE_PATH = Path(__file__).resolve().parents[1] / "github_source.py"
SPEC = importlib.util.spec_from_file_location("tyai_test_github_source", MODULE_PATH)
source = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source
SPEC.loader.exec_module(source)
GitHubSource = source.GitHubSource
GitHubSourceError = source.GitHubSourceError
diff_snapshots = source.diff_snapshots
REPO = source.REPOSITORY
API = f"https://api.github.com/repos/{REPO}"
WEB = f"https://github.com/{REPO}"


def issue(number=1, **changes):
    item = {"number": number, "title": "中文需求", "body": "正文", "state": "closed",
            "state_reason": "completed", "labels": [{"name": "prio:P1"}],
            "user": {"login": "Epslion404"}, "updated_at": "2026-10-01T00:00:00Z",
            "html_url": f"{WEB}/issues/{number}", "comments": 0}
    item.update(changes)
    return item


def pull(number=2, **changes):
    item = issue(number, html_url=f"{WEB}/pull/{number}", state="open")
    item.update({"head": {"sha": "a" * 40, "ref": "feature"},
                 "base": {"ref": "master"}, "draft": False, "merged_at": None})
    item.update(changes)
    return item


def comment(comment_id=10, number=1, **changes):
    item = {"id": comment_id, "body": "human discussion", "user": {"login": "Epslion404"},
            "updated_at": "2026-10-01T00:00:00Z", "html_url": f"{WEB}/issues/{number}#x",
            "issue_url": f"{API}/issues/{number}"}
    item.update(changes)
    return item


def fixtures():
    return {
        "issues?state=all&per_page=100": [issue(), issue(2, pull_request={})],
        "pulls?state=all&per_page=100": [pull()],
        "issues/comments?per_page=100": [comment(), comment(11, 2)],
        "pulls/comments?per_page=100": [
            comment(12, 2, pull_request_url=f"{API}/pulls/2", commit_id="a" * 40)],
        "pulls/2/reviews?per_page=100": [
            comment(13, 2, state="APPROVED", submitted_at="2026-10-01T00:00:00Z",
                    commit_id="a" * 40)],
        "git/ref/heads/master": {"object": {"sha": "b" * 40}},
        "git/ref/heads/competition": {"object": {"sha": "c" * 40}},
    }


class FakeRun:
    def __init__(self, data=None, responses=None):
        self.data = fixtures() if data is None else data
        self.responses = responses or {}
        self.calls = []

    def __call__(self, command, **kwargs):
        endpoint = command[-1].removeprefix(f"repos/{REPO}/")
        self.calls.append((endpoint, command, kwargs))
        assert command[:2] == ["gh", "api"]
        assert command[command.index("--method") + 1] == "GET"
        assert command[command.index("--hostname") + 1] == "github.com"
        assert kwargs["timeout"] == 30
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["text"] and kwargs["capture_output"]
        assert kwargs["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
        assert ("--paginate" in command) == (not endpoint.startswith("git/ref/"))
        queued = self.responses.get(endpoint)
        if queued:
            result = queued.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return subprocess.CompletedProcess(command, 0,
                                           json.dumps(self.data[endpoint], ensure_ascii=False), "")


def collect(data):
    return GitHubSource(run=FakeRun(data)).snapshot()


def keys(events):
    return {event["key"] for event in events}


def test_complete_snapshot_and_all_review_strategy():
    data = fixtures()
    data["pulls?state=all&per_page=100"].append(
        pull(3, state="closed", updated_at="2020-01-01T00:00:00Z"))
    data["pulls/3/reviews?per_page=100"] = [comment(14, 3, state="DISMISSED")]
    run = FakeRun(data)
    snapshot = GitHubSource(run=run).snapshot()
    entries = snapshot["entries"]
    assert snapshot["repository"] == REPO
    assert "issue:1" in entries and "issue:2" not in entries
    assert {"pr:2", "pr:3", "review:14", "branch:master", "branch:competition"} <= entries.keys()
    assert entries["issue:1"]["title"] == "中文需求"
    assert entries["issue:1"]["priority"] == "P1"
    assert entries["issue-comment:11"]["number"] == 2
    assert entries["review-comment:12"]["number"] == 2
    assert entries["review:13"]["sha"] == "a" * 40
    assert all(len(entry["fingerprint"]) == 64 for entry in entries.values())
    json.dumps(snapshot, ensure_ascii=False)
    assert not any(endpoint.startswith("issues/1") for endpoint, _, _ in run.calls)


def test_consecutive_page_arrays_are_concatenated():
    pages = " \n" + json.dumps([issue()], ensure_ascii=False) + "\n\t" + json.dumps(
        [issue(4)], ensure_ascii=False) + "\n[]\n"
    run = FakeRun(responses={"issues?state=all&per_page=100": [
        subprocess.CompletedProcess([], 0, pages, "")]})
    entries = GitHubSource(run=run).snapshot()["entries"]
    assert {"issue:1", "issue:4"} <= entries.keys()


@pytest.mark.parametrize("failure", [
    subprocess.CompletedProcess([], 1, "private token stdout", "secret credential stderr"),
    subprocess.TimeoutExpired("private token", 30),
    OSError("private credential"),
    subprocess.CompletedProcess([], 0, "[]\n[truncated", ""),
    subprocess.CompletedProcess([], 0, '{"unexpected": "object"}', ""),
    subprocess.CompletedProcess([], 0, "", ""),
])
def test_failure_retries_and_never_returns_partial_or_credentials(failure):
    endpoint = "git/ref/heads/competition" if isinstance(failure, OSError) else (
        "issues/comments?per_page=100")
    run = FakeRun(responses={endpoint: [failure, failure, failure]})
    with pytest.raises(GitHubSourceError) as caught:
        GitHubSource(run=run).snapshot()
    assert sum(call[0] == endpoint for call in run.calls) == 3
    assert "credential" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert "token" not in str(caught.value)


def test_transient_failure_retries_to_complete_snapshot():
    run = FakeRun(responses={"issues?state=all&per_page=100": [
        subprocess.TimeoutExpired("gh", 30),
        subprocess.CompletedProcess([], 1, "", "transient")]})
    assert "branch:competition" in GitHubSource(run=run).snapshot()["entries"]
    assert len([call for call in run.calls if call[0].startswith("issues?state")]) == 3


def test_issue_updated_time_counts_and_label_order_do_not_loop():
    data = fixtures()
    original = data["issues?state=all&per_page=100"][0]
    original["labels"].append({"name": "kind:task"})
    before = collect(data)
    original.update(updated_at="2099-01-01T00:00:00Z", comments=99,
                    reactions={"total_count": 200})
    original["labels"].reverse()
    assert diff_snapshots(before, collect(data)) == []


@pytest.mark.parametrize("change", [
    {"body": "new requirement"}, {"state": "open"},
    {"state_reason": "not_planned"}, {"labels": [{"name": "prio:P0"}]},
    {"title": "new title"},
])
def test_issue_meaningful_edits(change):
    data = fixtures()
    before = collect(data)
    data["issues?state=all&per_page=100"][0].update(change)
    assert keys(diff_snapshots(before, collect(data))) == {"issue:1"}


@pytest.mark.parametrize("endpoint,key", [
    ("issues/comments?per_page=100", "issue-comment:10"),
    ("pulls/comments?per_page=100", "review-comment:12"),
    ("pulls/2/reviews?per_page=100", "review:13"),
])
@pytest.mark.parametrize("change", [{"body": "edited discussion"},
                                   {"updated_at": "2026-10-02T00:00:00Z"}])
def test_comment_body_and_edit_time_changes(endpoint, key, change):
    data = fixtures()
    before = collect(data)
    data[endpoint][0].update(change)
    assert keys(diff_snapshots(before, collect(data))) == {key}


def test_review_dismissal_changes_event():
    data = fixtures()
    before = collect(data)
    data["pulls/2/reviews?per_page=100"][0]["state"] = "DISMISSED"
    assert keys(diff_snapshots(before, collect(data))) == {"review:13"}


def test_pr_owner_multiple_human_commits_always_emit():
    data = fixtures()
    before = collect(data)
    for sha in ("d" * 40, "e" * 40):
        data["pulls?state=all&per_page=100"][0]["head"]["sha"] = sha
        after = collect(data)
        events = diff_snapshots(before, after)
        assert keys(events) == {"pr:2"}
        assert events[0]["actor"] == "Epslion404"
        assert events[0]["sha"] == sha
        before = after


@pytest.mark.parametrize("endpoint,key", [
    ("issues/comments?per_page=100", "issue-comment:10"),
    ("pulls/comments?per_page=100", "review-comment:12"),
    ("pulls/2/reviews?per_page=100", "review:13"),
])
def test_only_full_body_marker_suppresses_self_comments(endpoint, key):
    data = fixtures()
    before = collect(data)
    # Marker beyond summary length must still work for our publishing identity.
    data[endpoint][0]["body"] = "x" * 500 + "\n<!-- tyai-auto-review: run-1 -->"
    data[endpoint][0]["user"] = {"login": "Epslion404"}
    after = collect(data)
    assert after["entries"][key]["self_review"]
    assert "tyai-auto-review" not in after["entries"][key]["summary"]
    assert diff_snapshots(before, after) == []
    # Removing the marker permits a later human edit to generate an event.
    data[endpoint][0]["body"] = "human response"
    assert keys(diff_snapshots(after, collect(data))) == {key}


def test_new_marked_comments_ignored_but_owner_branch_commit_retained():
    data = fixtures()
    before = collect(data)
    data["issues/comments?per_page=100"].append(
        comment(100, body="<!-- tyai-auto-review: 1 --> own result"))
    data["issues/comments?per_page=100"].append(comment(101, body="owner discussion"))
    data["git/ref/heads/master"]["object"]["sha"] = "f" * 40
    events = diff_snapshots(before, collect(data))
    assert keys(events) == {"branch:master", "issue-comment:101"}
    branch = next(event for event in events if event["kind"] == "branch")
    assert branch["old_sha"] == "b" * 40 and branch["sha"] == "f" * 40


def test_other_actor_cannot_disable_review_with_marker():
    data = fixtures()
    before = collect(data)
    data["issues/comments?per_page=100"][0].update(
        body="<!-- tyai-auto-review: copied -->", user={"login": "contributor"})
    after = collect(data)
    assert not after["entries"]["issue-comment:10"]["self_review"]
    assert keys(diff_snapshots(before, after)) == {"issue-comment:10"}


def test_own_new_pr_suppressed_but_followup_human_commit_detected():
    data = fixtures()
    before = collect(data)
    own = pull(50, body="<!-- tyai-auto-review: own-fix -->")
    data["pulls?state=all&per_page=100"].append(own)
    data["pulls/50/reviews?per_page=100"] = []
    after = collect(data)
    assert diff_snapshots(before, after) == []
    own["head"]["sha"] = "e" * 40
    assert keys(diff_snapshots(after, collect(data))) == {"pr:50"}


def test_deletions_ignored_and_no_mutation():
    before = collect(fixtures())
    after = copy.deepcopy(before)
    del after["entries"]["issue:1"]
    assert diff_snapshots(before, after) == []
    assert "issue:1" in before["entries"]
    assert diff_snapshots(before, before) == []


def test_empty_explicit_baseline_emits_additions_and_branch_old_sha_none():
    after = collect(fixtures())
    events = diff_snapshots({"repository": REPO, "entries": {}}, after)
    assert keys(events) == after["entries"].keys()
    assert all(event["old_sha"] is None for event in events if event["kind"] == "branch")


def test_repository_restriction():
    with pytest.raises(ValueError, match="Only"):
        GitHubSource(repo="Scaxlibur/WaveBench", run=FakeRun())
    after = collect(fixtures())
    with pytest.raises(ValueError, match="Snapshots"):
        diff_snapshots({"repository": "foreign/repo", "entries": {}}, after)


def test_foreign_comment_parent_fails_whole_snapshot():
    data = fixtures()
    data["issues/comments?per_page=100"][0]["issue_url"] = (
        "https://api.github.com/repos/foreign/repo/issues/1")
    with pytest.raises(GitHubSourceError, match="foreign"):
        collect(data)
