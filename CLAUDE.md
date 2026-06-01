# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Project:** Device Management — a Python 3.11 / FastAPI backend for plugin management
(centralized config, plugin catalog, progressive deployment, telemetry, secure relay).
Security-sensitive domain: OIDC/Keycloak auth, relay secrets, signed artifacts.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Git & PR workflow

**Never push directly to `main`. All changes land through a pull request.**

- Don't commit to `main` locally and don't `git push` to `main`. If you're on `main`, create a branch first.
- Branch naming follows the convention CI already keys on: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`
  (also `docs/<slug>` for documentation-only work).
- Open the PR against `main` in the `IA-Generative` GitHub org (use `gh pr create`).
- Push **only when the user explicitly asks**, and confirm with the user at least once before doing comitting or pushing.

### PR title

Use the Conventional Commits format (see below) — e.g. `fix(security): publish OIDC discovery cache under lock`.

### PR description

Straight to the point. No filler, no AI throat-clearing. Cover, briefly:
- **What** changed and **why** (the problem it solves).
- **How** to verify it (tests added, commands to run, or manual steps).
- Any breaking changes, migrations (`alembic`), or new env vars.

A few tight bullets beats a wall of prose. If the change is trivial, one line is fine.

## Conventional Commits

Commit subjects (and PR titles) follow `type(scope): subject`:

```
fix(security): publish OIDC discovery cache under lock
feat(catalog): expose plugin id in public /catalog/api/plugins
docs(deploy): consolidate scaleway + dgx runbook
chore(secrets): normalize k8s secret handling
```

- **type:** `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`, `ci`.
- **scope:** the area touched. Scopes already in use in this repo include
  `security`, `catalog`, `k8s`, `db`, `deploy`, `secrets`, `updates`, `ops`.
- **subject:** imperative mood, lower-case, no trailing period.

## Security guardrails

This is a security-sensitive service. Be conservative around:

- **Auth / OIDC / Keycloak** (`app/middleware`, auth services), **relay secrets**, and signed artifacts —
  changes here need tests and a clear rationale.
- **`bandit` and `semgrep` must stay clean.** Don't blanket-suppress findings; if a `# nosec`
  is genuinely warranted, justify it inline. Lint config lives in `pyproject.toml`.
- **No secrets in committed code or k8s manifests.** Secrets belong in per-environment overlays
  / `env-secrets.yaml`, never in the committed base. Don't print or log secret values.
- Validate and sanitize all external input (config templates, uploads, relay payloads).

## Comments

Only comment things that have business logic complexity.
Comments are an apology, not a requirement. Good code mostly documents itself.

Bad:

```python
def hash_it(data):
    # The hash
    hash_value = 0
    # Length of string
    length = len(data)
    # Loop through every character in data
    for i in range(length):
        # Get character code
        char = ord(data[i])
        # Make the hash
        hash_value = (hash_value << 5) - hash_value + char
        # Convert to 32-bit integer
        hash_value &= 0xFFFFFFFF
    return hash_value
```

Good:

```python
def hash_it(data):
    hash_value = 0
    for char in data:
        hash_value = (hash_value << 5) - hash_value + ord(char)
        # Convert to 32-bit integer
        hash_value &= 0xFFFFFFFF
    return hash_value
```

Don't leave commented out code in your codebase. Version control exists for a reason. Leave old code in your history.

Bad:

```python
do_stuff()
# do_other_stuff()
# do_some_more_stuff()
```

Good:

```python
do_stuff()
```

Don't have journal comments. Remember, use version control! Use `git log` to get history.

Bad:

```python
# 2016-12-20: Removed monads, didn't understand them (RM)
# 2016-10-01: Improved using special monads (JP)
# 2016-02-03: Removed type-checking (LI)
def combine(a, b):
    return a + b
```

Good:

```python
def combine(a, b):
    return a + b
```

Avoid positional markers. They usually just add noise. Let the functions and variable names along with proper indentation and formatting give the visual structure to your code.

Bad:

```python
##############################
# Scope model instantiation
##############################
model = {"menu": "foo", "nav": "bar"}

##############################
# Action setup
##############################
def actions():
    ...
```

Good:

```python
model = {"menu": "foo", "nav": "bar"}

def actions():
    ...
```

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
</content>
</invoke>
