#!/usr/bin/env python3
"""
FlowGate CI Agent
-----------------
Runs inside GitHub Actions on every PR event. Fetches the PR diff via the
GitHub REST API (never checks out or executes the PR's own code), sends it
to Claude with the FlowGate master prompt, then posts the result back to
the PR as a comment + label, and fails the job (blocking merge if this check
is required) when the category is RED.

Required environment variables (set by the workflow, not by hand):
  ANTHROPIC_API_KEY  - Claude API key (from repo secret)
  GITHUB_TOKEN       - provided automatically by GitHub Actions
  REPO               - "owner/repo"
  PR_NUMBER          - pull request number
  PR_TITLE           - PR title
  PR_BODY            - PR description
"""

import os
import sys
import json

import requests
import anthropic

MODEL = "claude-sonnet-4-6"
MAX_DIFF_LINES = 500

MASTER_PROMPT = """You are an AI-Native Delivery Gatekeeper (Agentic TPM). Your goal is to analyze raw git diff files from Pull Requests and execute an automated Semantic Triage.

Analyze the provided Git Diff and PR Description according to these strict rules:

1. CLASSIFICATION BUCKETS:
   - GREEN (Fast-Track): Minimal risk. UI/CSS changes, docs, logging, or isolated functions.
   - YELLOW (Contextual Review): Medium risk. Modifies APIs, cross-team dependencies, or shared components.
   - RED (Guardrail Blocker): High risk. Database schema shifts, security/auth paths, encryption, or complex refactors.

2. INPUT CONSTRAINTS & EDGE CASES:
   - Line Cap Constraint: If the git diff exceeds 500 lines of code, automatically mark it as RED with the reason: "PR exceeds automated scan threshold of 500 lines."
   - Missing Description: If required checklist items (e.g., feature flag, rollback plan) cannot be verified in the diff or description, do not assign GREEN. Escalate to YELLOW.
   - Empty diffs or pure file deletions should be flagged based on path sensitivity (e.g., deleting a config/migration file is RED, deleting a doc/temp file is GREEN).

3. CHECKLIST VERIFICATION:
   - Scan PR Description and Code Diff for:
     a) Feature Flag / Toggle presence.
     b) Backward compatibility (no breaking contract changes).
     c) Rollback plan or explicit instructions.

4. OUTPUT FORMAT:
   Return STRICTLY a single, valid JSON object with NO conversational text, preambles, or markdown commentary outside the JSON block. Do not include any text, markdown wrap like ```json, or explanations before or after the JSON.

JSON Schema:
{
  "category": "GREEN" | "YELLOW" | "RED",
  "risk_score": 1-10,
  "reasoning": "1-2 concise, engineering-focused sentences explaining the decision",
  "impacted_areas": ["Database", "UI", "API", "Security", "Logic", "Docs"],
  "checklist_status": {
    "feature_flag_detected": boolean,
    "backward_compatible": boolean,
    "rollback_plan_present": boolean
  },
  "recommended_action": "Clear recommendation for the engineering lead / pipeline",
  "target_reviewers": ["List of suggested domain owners or 'None' for Green"]
}
"""

LABEL_COLORS = {
    "GREEN": ("triage:green", "0E8A16"),
    "YELLOW": ("triage:yellow", "FBCA04"),
    "RED": ("triage:red", "D93F0B"),
}


def env(name: str) -> str:
    val = os.environ.get(name)
    if val is None:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_pr_diff(repo: str, pr_number: str, token: str) -> str:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = github_headers(token)
    headers["Accept"] = "application/vnd.github.v3.diff"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def count_diff_lines(diff_text: str) -> int:
    n = 0
    for line in diff_text.splitlines():
        if (line.startswith("+") or line.startswith("-")) and not (
            line.startswith("+++") or line.startswith("---")
        ):
            n += 1
    return n


def run_claude_triage(title: str, description: str, diff: str) -> dict:
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    user_message = f"PR Title: {title}\nPR Description: {description}\n\nGit Diff:\n{diff}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=MASTER_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def post_comment(repo: str, pr_number: str, token: str, result: dict):
    emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[result["category"]]
    reviewers = ", ".join(result.get("target_reviewers", [])) or "None"
    body = f"""## {emoji} FlowGate Triage: {result['category']}

**Risk score:** {result['risk_score']}/10

**Reasoning:** {result['reasoning']}

**Impacted areas:** {', '.join(result.get('impacted_areas', []))}

**Checklist:**
- Feature flag detected: {result['checklist_status']['feature_flag_detected']}
- Backward compatible: {result['checklist_status']['backward_compatible']}
- Rollback plan present: {result['checklist_status']['rollback_plan_present']}

**Recommended action:** {result['recommended_action']}

**Suggested reviewers:** {reviewers}

---
*Automated triage by FlowGate. This does not replace human review for YELLOW/RED PRs.*
"""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    resp = requests.post(url, headers=github_headers(token), json={"body": body}, timeout=30)
    resp.raise_for_status()


def ensure_label_exists(repo: str, token: str, name: str, color: str):
    url = f"https://api.github.com/repos/{repo}/labels"
    resp = requests.post(
        url, headers=github_headers(token),
        json={"name": name, "color": color, "description": "FlowGate automated PR triage"},
        timeout=30,
    )
    # 422 means it already exists - that's fine.
    if resp.status_code not in (201, 422):
        resp.raise_for_status()


def apply_label(repo: str, pr_number: str, token: str, category: str):
    label_name, color = LABEL_COLORS[category]
    ensure_label_exists(repo, token, label_name, color)
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/labels"
    resp = requests.post(url, headers=github_headers(token), json={"labels": [label_name]}, timeout=30)
    resp.raise_for_status()


def main():
    repo = env("REPO")
    pr_number = env("PR_NUMBER")
    token = env("GITHUB_TOKEN")
    title = os.environ.get("PR_TITLE", "")
    description = os.environ.get("PR_BODY", "") or "(no description provided)"

    diff = fetch_pr_diff(repo, pr_number, token)
    line_count = count_diff_lines(diff)

    result = run_claude_triage(title, description, diff)

    # Deterministic enforcement of the line-cap rule, independent of the model:
    if line_count > MAX_DIFF_LINES and result.get("category") != "RED":
        result["category"] = "RED"
        result["reasoning"] = "PR exceeds automated scan threshold of 500 lines."
        result["risk_score"] = max(result.get("risk_score", 5), 6)

    print(json.dumps(result, indent=2))

    post_comment(repo, pr_number, token, result)
    apply_label(repo, pr_number, token, result["category"])

    if result["category"] == "RED":
        print("::error::FlowGate blocked this PR (RED). See PR comment for details.")
        sys.exit(1)  # fails the check; add as a required status check to hard-block merge


if __name__ == "__main__":
    main()