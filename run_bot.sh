#!/bin/bash
# Swing Trader — native launcher for launchctl
# Activates the venv and runs main.py

cd "/Users/nithun/Documents/Swing trader" || exit 1
source venv/bin/activate
exec python main.py
