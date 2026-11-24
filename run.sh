#!/bin/sh
# Entry point for cron: run the watcher with this folder's virtualenv.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python scraper.py "$@"
