#!/bin/sh
# Full census on macOS / Linux, resumable.
#
#   ./run-full.sh            first run, or continue an interrupted one
#   ./run-full.sh -j 4       any extra flag is passed to `netcensus census`
#
# Running it again after a crash, reboot or Ctrl-C RESUMES: every test already
# decided is in data/census.jsonl and is skipped.  Progress is printed and
# appended to data/census.log (watch it with: tail -f data/census.log).
set -e
cd "$(dirname "$0")"

# The LNA harness environment, when present (Mac mini: venv, WPT_ROOT, pinned
# browsers, ulimit).  Without it, WPT_ROOT / WPT_CHROME must already be set.
[ -f ../env.sh ] && . ../env.sh
[ -z "$WPT_CHROME" ] && [ -n "$LNA_CHROME" ] && export WPT_CHROME="$LNA_CHROME"
export PYTHONIOENCODING=utf-8

JOBS=8                                  # 10-core / 16 GB Mac mini
RESUME=""
[ -s data/census.jsonl ] && RESUME="--resume"
mkdir -p data

echo "netcensus full run  $(date '+%F %T')  WPT_ROOT=$WPT_ROOT  ${RESUME:-fresh}" \
    | tee -a data/census.log
python -m netcensus census -j "$JOBS" --out data/census.json $RESUME "$@" 2>&1 \
    | tee -a data/census.log
