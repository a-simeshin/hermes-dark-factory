#!/bin/bash
# Cron wrapper for pr-risk-labels.
#
# Why it exists: the scheduler runs the no_agent script of a job WITHOUT
# arguments (argv is just [bash|python, script]); the job's prompt field never
# reaches the script. pr-risk.py without arguments prints its docstring and
# exits with code 2, so the risk:* labels would never be set. The --label-sync
# mode is therefore fixed here, not in the prompt field.
exec python3 "$(cd "$(dirname "$0")" && pwd)/pr-risk.py" --label-sync
