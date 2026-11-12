# 24 — Prompt Library for Claude Code

These are copy-paste prompts. Each step prompt in the module files is self-contained. Wrap each step prompt in the templates below.

## 1. Session start prompt (start of every session)

```
You are building the Office Task System. Before writing any code:
1. Read CLAUDE.md fully.
2. Read docs/plan/00_README.md and docs/plan/01_ARCHITECTURE_CONTRACT.md.
3. Read docs/decisions.md and the last 20 lines of docs/CHANGELOG.md.
4. Run `git log --oneline -15` and `make test`. Report the current state: last completed step and test status.
Then wait for the step instruction. Do not start coding until you receive a step.
Rules for this session:
- Implement exactly one step. Do not touch other modules except where the step says so.
- Follow the specification literally: enum values, column names, thresholds, schedules.
- If the spec is ambiguous or conflicts with existing code, stop and ask with a concrete proposal. Do not guess.
- Never weaken a test to make it pass. Never mark tests skip/xfail without my approval.
```

## 2. Step execution template

```
Step: <e.g. M07.2>
Spec file: docs/plan/<module file>.md

<paste the step prompt from the module file here>

Process:
1. Restate the acceptance criteria and the failure tests you will write (IDs).
2. List the files you will create or modify.
3. Write the tests first (failure tests included), run them, and show that they fail.
4. Implement until all tests pass.
5. Run: make lint && make typecheck && make test. All must be green.
6. Add one line to docs/CHANGELOG.md: "<step id>: <summary>".
7. If you deviated from the spec in any way, add an entry to docs/decisions.md (date, step, deviation, reason).
8. Output a summary:
   - files changed;
   - tests added (by ID);
   - query counts or memory figures measured where the step requires them;
   - manual verification items for a person (for example "run on a real Fire TV").
```

## 3. Independent review prompt (new session after each step)

```
You are reviewing step <ID> as an independent senior engineer. You did not write this code.
Read CLAUDE.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/<module file>.md, then run `git diff <previous commit>..HEAD`.
Check, and report findings with file:line:
1. Every acceptance criterion and failure ID of the step has a test that would fail if the behaviour broke.
2. Contract violations:
   - client-supplied timestamps used as truth;
   - writes outside services.py;
   - missing transaction.atomic;
   - missing audit/event in the same transaction;
   - cascade deletes;
   - unbounded querysets;
   - whole-file reads into memory;
   - Graph calls outside ots.storage;
   - external asset URLs;
   - secrets in code;
   - permission checks missing or not server-side;
   - missing Idempotency-Key handling.
3. Error handling: every external call has a timeout and a classified failure path.
4. N+1 queries on list views.
5. Anything that would break the memory budget on a 4 GB server.
Classify each finding as BLOCKER, MAJOR or MINOR. Do not fix anything. Output the list only.
```

## 4. Fix-loop prompt

```
Here are the review findings for step <ID>:
<paste findings>
Fix every BLOCKER and MAJOR. For each fix, add or adjust a test that would have caught it. Leave MINOR items unless trivial. Re-run make lint && make typecheck && make test. Summarise each fix against its finding number.
```

## 5. Phase-gate prompt (end of each phase)

```
Phase <P#> gate check. Read docs/plan/00_README.md (gate row for <P#>) and the done-gate sections of the module files in this phase.
1. Run the full suite, plus tests/failure for this phase's IDs, plus the integration tests listed for this phase in docs/plan/20_INTEGRATION_PLAN.md.
2. Produce docs/qualification/<P#>-gate.md with:
   - a table of every gate item: PASS / FAIL / NEEDS-MANUAL, with evidence (test name or command output);
   - a list of the manual checks a person must perform on the real server, TV or PCs.
Do not write new features.
```

## 6. Debugging prompt (production incident)

```
Incident: <describe symptom, time, alert key>.
Read docs/plan/22_RUNBOOKS.md and docs/plan/21_FAILURE_TEST_CATALOGUE.md.
1. Identify the matching failure ID and runbook.
2. List the read-only diagnostic commands to run (docker logs with time filter, specific SQL against jobs/storage_operations/backup_runs/alerts, the health endpoint). Do not run anything that writes.
3. After I paste the outputs, propose the fix. If code is wrong, write a failing test that reproduces the incident first, then the fix.
```

## 7. Schema change prompt

```
I need a schema change: <describe>.
1. Check it against docs/plan/01_ARCHITECTURE_CONTRACT.md section 6. If it changes the contract, draft the decisions.md entry and wait for my approval before coding.
2. Write a forward-only migration. If it drops or alters a column, confirm deploy.sh takes a pre-migration dump.
3. Update the contract file, the module file and any affected failure tests in the same commit.
```

## 8. Rules of thumb when driving Sonnet

- Give one step per message. Long multi-step prompts produce shallow work.
- Always paste the exact step prompt. Do not paraphrase the spec.
- Run the review prompt in a **fresh session**, so the code is not graded by the session that wrote it.
- If a step fails twice, split it: ask for the tests only first, then the implementation.
- Keep `docs/decisions.md` current. Future sessions read it to avoid relitigating decisions.
