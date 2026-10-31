"""Read-only, complete GitHub snapshots for the authorized TYAI repository.

Issues, pulls and both comment types use repository-wide paginated endpoints.
GitHub has no repository-wide REST endpoint for PR reviews, so reviews are read
for *every* PR, including old/closed PRs. This costs one paginated call per PR,
but avoids a recency cutoff silently missing edited or dismissed old reviews.
The runner owns baseline persistence; diff_snapshots never invents a baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit

from process_options import background_process_options


REPOSITORY = "Epslion404/TYAI-for-wavebench"
SELF_MARKER = "<!-- tyai-auto-review:"
BRANCHES = ("master", "competition")


class GitHubSourceError(RuntimeError):
    """Collection failed; no partial snapshot is suitable for persistence."""


def _fingerprint(content: dict[str, Any]) -> str:
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _labels(item: dict[str, Any]) -> list[str]:
    return sorted({label if isinstance(label, str) else label["name"]
                   for label in item.get("labels", [])})


def _priority(labels: list[str]) -> str | None:
    priorities = sorted(label[5:] for label in labels if label.startswith("prio:"))
    return priorities[0] if priorities else None


def _actor(item: dict[str, Any]) -> str | None:
    return (item.get("user") or {}).get("login")


def _summary(body: str | None) -> str:
    return " ".join((body or "").split())[:400]


def _number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubSourceError("GitHub returned an invalid object number/id")
    return value


def _parent_number(url: str, resource: str) -> int:
    """Only extract parent numbers from URLs belonging to the allowed repo."""
    parsed = urlsplit(url)
    prefix = f"/repos/{REPOSITORY}/{resource}/"
    if (parsed.scheme != "https" or parsed.netloc != "api.github.com"
            or not parsed.path.startswith(prefix) or parsed.query or parsed.fragment):
        raise GitHubSourceError("GitHub returned a foreign comment parent URL")
    suffix = parsed.path[len(prefix):]
    if not suffix.isascii() or not suffix.isdecimal() or int(suffix) <= 0:
        raise GitHubSourceError("GitHub returned an invalid comment parent URL")
    return int(suffix)


def _decode_stream(text: str, arrays: bool) -> Any:
    """gh --paginate emits consecutive JSON arrays, not one JSON document."""
    decoder = json.JSONDecoder()
    offset = 0
    pages = []
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        page, offset = decoder.raw_decode(text, offset)
        if arrays and (not isinstance(page, list)
                       or any(not isinstance(item, dict) for item in page)):
            raise ValueError("Expected JSON arrays of objects")
        pages.append(page)
    if not pages:
        raise ValueError("Empty GitHub response")
    if arrays:
        return [item for page in pages for item in page]
    if len(pages) != 1 or not isinstance(pages[0], dict):
        raise ValueError("Expected one JSON object")
    return pages[0]


class GitHubSource:
    def __init__(
        self,
        repo: str = REPOSITORY,
        proxy: str = "http://127.0.0.1:7897",
        gh: str = "gh",
        run: Callable[..., Any] = subprocess.run,
        self_actor: str = "Epslion404",
    ) -> None:
        if repo != REPOSITORY:
            raise ValueError(f"Only {REPOSITORY} is authorized")
        self.repo = repo
        self.proxy = proxy
        self.gh = gh
        self.run = run
        self.self_actor = self_actor

    def _get(self, endpoint: str, *, arrays: bool = True) -> Any:
        command = [self.gh, "api", "--hostname", "github.com", "--method", "GET",
                   "-H", "Accept: application/vnd.github+json",
                   "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if arrays:
            command.append("--paginate")
        command.append(f"repos/{self.repo}/{endpoint}")
        env = os.environ.copy()
        if self.proxy:
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[name] = self.proxy
        # Three bounded immediate retries, with no sleeps or credential-bearing
        # stderr/stdout exposed in errors or persisted snapshots.
        for _ in range(3):
            try:
                result = self.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="strict", timeout=30,
                                  check=False, env=env, **background_process_options())
                if result.returncode == 0:
                    return _decode_stream(result.stdout, arrays)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        raise GitHubSourceError(f"GitHub read failed after 3 attempts: {endpoint}") from None

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe entries only when every required read has succeeded."""
        try:
            return self._snapshot()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GitHubSourceError("GitHub returned an invalid snapshot object") from None

    def _snapshot(self) -> dict[str, Any]:
        entries: dict[str, dict[str, Any]] = {}
        parents: dict[int, dict[str, Any]] = {}
        issues = self._get("issues?state=all&per_page=100")
        pulls = self._get("pulls?state=all&per_page=100")
        for kind, objects in (("issue", issues), ("pr", pulls)):
            for item in objects:
                if kind == "issue" and "pull_request" in item:
                    continue
                number = _number(item["number"])
                labels = _labels(item)
                content = {
                    "title": item["title"], "body": item.get("body"),
                    "state": item["state"], "state_reason": item.get("state_reason"),
                    "labels": labels, "assignees": sorted(
                        actor["login"] for actor in item.get("assignees", [])),
                    "milestone": (item.get("milestone") or {}).get("number"),
                }
                entry = {
                    "url": item["html_url"], "kind": kind, "number": number,
                    "title": item["title"], "actor": _actor(item),
                    "updated_at": item.get("updated_at"), "priority": _priority(labels),
                    "summary": _summary(item.get("body")),
                    "self_review": (SELF_MARKER in (item.get("body") or "")
                                    and _actor(item) == self.self_actor),
                }
                if kind == "pr":
                    head, base = item["head"], item["base"]
                    content.update({"head_sha": head["sha"], "head_ref": head["ref"],
                                    "base_ref": base["ref"], "draft": item.get("draft", False),
                                    "merged_at": item.get("merged_at"),
                                    "merge_commit_sha": item.get("merge_commit_sha")})
                    entry.update({"sha": head["sha"], "branch": base["ref"]})
                # updated_at and volatile counts are intentionally excluded.
                # actor is the author, not evidence of who last updated an issue/PR.
                entry["fingerprint"] = _fingerprint(content)
                entries[f"{kind}:{number}"] = entry
                parents[number] = entry

        for kind, endpoint, parent_field, resource in (
            ("issue-comment", "issues/comments?per_page=100", "issue_url", "issues"),
            ("review-comment", "pulls/comments?per_page=100", "pull_request_url", "pulls"),
        ):
            for item in self._get(endpoint):
                number = _parent_number(item[parent_field], resource)
                self._add_comment(entries, parents, kind, number, item)

        for pull in pulls:
            number = _number(pull["number"])
            for review in self._get(f"pulls/{number}/reviews?per_page=100"):
                self._add_comment(entries, parents, "review", number, review)

        for branch in BRANCHES:
            ref = self._get(f"git/ref/heads/{branch}", arrays=False)
            sha = ref["object"]["sha"]
            if not isinstance(sha, str) or not sha:
                raise GitHubSourceError("GitHub returned an invalid branch SHA")
            entries[f"branch:{branch}"] = {
                "fingerprint": _fingerprint({"sha": sha}),
                "url": f"https://github.com/{self.repo}/tree/{branch}",
                "kind": "branch", "number": None, "title": branch, "actor": None,
                "updated_at": None, "sha": sha, "branch": branch,
                "priority": None, "summary": f"{branch} at {sha}",
            }
        return {"repository": self.repo, "entries": entries}

    def _add_comment(
        self, entries: dict[str, Any], parents: dict[int, Any], kind: str,
        number: int, item: dict[str, Any],
    ) -> None:
        parent = parents.get(number, {})
        body = item.get("body") or ""
        content = {"body": body, "updated_at": item.get("updated_at"),
                   "state": item.get("state"), "commit_id": item.get("commit_id"),
                   "path": item.get("path"), "line": item.get("line"),
                   "side": item.get("side"), "in_reply_to_id": item.get("in_reply_to_id"),
                   "submitted_at": item.get("submitted_at")}
        entry = {
            "fingerprint": _fingerprint(content), "url": item["html_url"], "kind": kind,
            "number": number, "title": parent.get("title", f"#{number}"),
            "actor": _actor(item),
            "updated_at": item.get("updated_at") or item.get("submitted_at"),
            "priority": parent.get("priority"), "summary": _summary(body),
            # Preserve all objects in snapshots. Detect the marker in the full
            # body, even if it falls beyond the bounded summary.
            "self_review": SELF_MARKER in body and _actor(item) == self.self_actor,
        }
        if item.get("commit_id"):
            entry["sha"] = item["commit_id"]
        if parent.get("branch"):
            entry["branch"] = parent["branch"]
        entries[f"{kind}:{_number(item['id'])}"] = entry


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Return added/changed entries; callers decide when to establish a baseline.

    Deleted entries are ignored. Author/owner identity never suppresses changes.
    Only bodies with the auto-review marker suppress comment/review events.
    Branch updates include old_sha for the runner's subsequent compare request.
    """
    for snapshot in (before, after):
        if snapshot.get("repository") != REPOSITORY:
            raise ValueError(f"Snapshots must belong to {REPOSITORY}")
    previous = before["entries"]
    events = []
    for key, entry in sorted(after["entries"].items()):
        old = previous.get(key)
        if old is not None and old["fingerprint"] == entry["fingerprint"]:
            continue
        if entry["kind"] in ("issue-comment", "review-comment", "review"):
            if entry.get("self_review"):
                continue
        # Our newly-created follow-up issue/PR already has a reviewed receipt.
        # Subsequent edits, including human commits to our PR, still emit events.
        if old is None and entry["kind"] in ("issue", "pr") and entry.get("self_review"):
            continue
        event = {**entry, "key": key}
        if entry["kind"] == "branch":
            event["old_sha"] = old.get("sha") if old else None
        events.append(event)
    return events
