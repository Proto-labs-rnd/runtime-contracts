#!/bin/bash
# session-guard.sh — Context window monitor
# Usage: bash session-guard.sh [input_tokens] [max_tokens]
# Exit codes: 0=OK, 1=WARNING, 2=DELEGATE

INPUT=${1:-0}
MAX=${2:-128000}

if [ "$MAX" -eq 0 ]; then
  echo "ERROR: max_tokens cannot be 0"
  exit 3
fi

PCT=$(echo "scale=1; $INPUT * 100 / $MAX" | bc)
PCT_INT=$(echo "$PCT" | cut -d. -f1)

if [ "$PCT_INT" -ge 80 ]; then
  echo "DELEGATE [${PCT}%] - déléguer à un subagent maintenant"
  exit 2
elif [ "$PCT_INT" -ge 70 ]; then
  echo "WARNING [${PCT}%] - contexte élevé"
  exit 1
else
  echo "OK [${PCT}%]"
  exit 0
fi
