# AUTOJOURNAL builder a1

Request: "on auto days, don't ask just record a Y".

- 14:34 MT: started from origin/trunk d81a58d (already past the 14:20 target).
- Auto-day rule: `approval_mode` is "auto" when post-close runs AND no proposal created that ET day was decided
  by a person (`decided_via` telegram or web). A day with no proposals in auto mode is an auto day.
- On an auto day `_auto_journal` (jobs/postclose.py) updates the day's journal row for the live run only where
  `rules_followed IS NULL` to (true, answered_via "auto"): an answer already given is kept, a re-run changes
  nothing. The summary then shows "Rules followed: Yes (auto mode)" (or the kept answer) with no question and
  no buttons, and no journal nonce is issued. Any failure: `postclose.auto_journal_failed` warning, the summary
  asks as before.
- No migration: `journal.answered_via` is a plain String(20) with no CHECK. Web Journal/Reports show
  "Yes (auto mode)". Metrics count the row as an answered, followed day (unchanged code).
- 14:37 MT: targeted tests pass (tests/jobs/test_postclose.py, tests/notify/test_messages.py: 79 passed).
- 14:43 MT: gate 1 failed on the two contract tests that list DailySummaryView's fields; added
  `journal_answer` (defaulted, last) to both.
- 14:48 MT: gate 2 exit 0 (4585 passed, 35 skipped, 6 xfailed; web 730 passed).
