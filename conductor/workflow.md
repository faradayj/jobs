# Workflow Configuration

## Empirical Solo Debugging Protocol (Scratch Scripts)
Before creating specs, writing implementation plans, or modifying any core application scripts (`src/app_*.py`), the agent MUST follow this empirical debugging protocol:

1. **Isolated Scratch Reproduction**:
   - Create single-purpose, non-modifying scratch scripts in `<appDataDir>/scratch/` to navigate to and interact with target ATS pages live.
   - Do NOT run production batch CLI scripts to diagnose unknown errors.

2. **Live DOM Probing**:
   - Execute live Playwright JS evaluations to inspect exact HTML elements, attributes (`data-automation-id`, `role`, `aria-*`), and parent container hierarchies.
   - Determine whether elements are standard HTML inputs, custom React widgets, or combobox listboxes empirically.

3. **Empirical Screenshot & Error Capture**:
   - Capture full-page screenshots (`artifacts/stuck_*.png`) and extract un-truncated error text from validation banners when a step fails.
   - Base all diagnostic hypotheses strictly on concrete, visual screenshot evidence and raw DOM logs.

4. **Isolated Micro-Patch Verification**:
   - Test potential fixes (e.g. event dispatches like `blur`, `change`, `Tab` keypresses) inside scratch scripts first.
   - Propose code changes to main application files ONLY after empirical verification proves the fix works cleanly.

---

## Development Protocol
1. **Spec-Driven Development**: Every track must have a clear specification and implementation plan.
2. **Empirical Verification**: Code edits must be tested and verified against actual target pages or test suites.
3. **Commit Hygiene**: Commit work atomically with conventional commit messages (`feat:`, `fix:`, `chore:`).
4. **Safety Safeguard**: Never auto-submit applications; always pause at the Review page.
