# .github/workflows/monthly_intel.yml
#
# Runs the market intelligence script on the FIRST MONDAY of each month.
#
# GitHub Actions cron treats day-of-month and day-of-week as OR, not AND,
# so "0 13 1-7 * 1" would fire on days 1-7 AND every Monday. The reliable
# pattern is: trigger every Monday, then gate the job so it only proceeds
# when the date is within the first 7 days of the month.

name: Monthly Market Intelligence

on:
  schedule:
    # Every Monday. Replace 13:00 UTC with the same UTC time your weekly
    # job currently uses. (Cron in GitHub Actions is always UTC — e.g.
    # 6:00 AM Pacific = 13:00 UTC during PDT, 14:00 UTC during PST.)
    - cron: "0 13 * * 1"
  workflow_dispatch: {}   # allows manual runs for testing (bypasses the gate)

permissions:
  contents: write

jobs:
  monthly-report:
    runs-on: ubuntu-latest
    steps:
      - name: Check if this is the first Monday of the month
        id: gate
        run: |
          DAY=$((10#$(date -u +%d)))
          if [ "${{ github.event_name }}" = "workflow_dispatch" ] || [ "$DAY" -le 7 ]; then
            echo "run=true" >> "$GITHUB_OUTPUT"
            echo "First Monday (day $DAY) or manual run — proceeding."
          else
            echo "run=false" >> "$GITHUB_OUTPUT"
            echo "Day $DAY of the month — not the first Monday. Skipping."
          fi

      - name: Checkout
        if: steps.gate.outputs.run == 'true'
        uses: actions/checkout@v4

      - name: Set up Python
        if: steps.gate.outputs.run == 'true'
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        if: steps.gate.outputs.run == 'true'
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 lxml trafilatura youtube-transcript-api

      - name: Run monthly intelligence script
        if: steps.gate.outputs.run == 'true'
        run: python monthly_intel.py

      - name: Commit report
        if: steps.gate.outputs.run == 'true'
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add latest_news.txt
          if git diff --cached --quiet; then
            echo "No changes to commit."
          else
            git commit -m "Monthly intelligence report: $(date -u +%Y-%m-%d)"
            git push
          fi
