# Maintaining BOSS Job Copilot

This repository contains a reusable Skill at `skills/boss-job-copilot/`.

- Keep public instructions and templates generic. Never commit resumes, personal career facts, job databases, browser profiles, credentials, runtime output, or private test logs.
- The Python scripts handle collection, browser actions and storage. The Agent performs semantic job review; do not replace it with title-keyword filters or scoring scripts.
- Preserve chat-first profile/strategy confirmation, first-batch calibration and explicit message authorization. Existing contact records prevent duplicate greetings; uncertain sends are verified without resending.
- For a full job search, prepare/reuse the dedicated BOSS browser and check login early while continuing background analysis. Login or setup delays must not block intake. After strategy approval, check the page once and collect if ready without requiring a separate login acknowledgement. Analysis-only and browser-only requests retain their narrower scope.
- Review progress is stored in SQLite. Use the built-in jobs review view: ten jobs for initial calibration, twenty per subsequent group by default. These are adjustable query sizes, not enforced limits or task claims. Do not add a workflow scheduler or automatically invalidate historical reviews.
- Keep role boundaries and professional preferences in each user's private search plan. Public review guidance must remain profession-neutral; preserve work location and other hard-condition evidence in query output and reports.
- Normal upgrades preserve user data. Never inspect or reset another workspace without a user request. Browser integration tests must use fixtures, not real recruiters.
- Browser open/close/restart only manages the dedicated browser. Never reset or rewrite job, review or contact records on closure. Preserve actual send attempts for read-only verification. Blank pages are not login failures; explicit home navigation and bounded restart reuse the existing profile.
- Full chat pages may omit URL identity parameters. Bind the visible company, recruiter, full job title and selected contact to the authorized contact attempt; recheck before each write. Do not mistake an identity timeout for a hung browser or retry uncertain sends. Restart defaults to BOSS home; close gracefully before forcing only verified owned processes, and verify exit before reopening.
- New public Skill files must be deliberately added to the allowlist in `scripts/package_skill.py`. Inspect file contents as well as filenames before publishing.

From the repository root:

```bash
python -m unittest discover -s skills/boss-job-copilot/scripts/tests -v
python skills/boss-job-copilot/scripts/package_skill.py --out dist/boss-job-copilot.zip
```

Set `BOSS_BROWSER_FIXTURE=1` to include local Chrome fixture tests. Update README usage when public behavior changes.
