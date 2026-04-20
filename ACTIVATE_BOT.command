#!/bin/bash
# Double-click this file to activate the Swing Trader daily scheduler.
# It will run every weekday at 9:30am automatically after this.

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nithun.swingtrader.plist 2>/dev/null || \
launchctl load ~/Library/LaunchAgents/com.nithun.swingtrader.plist 2>/dev/null

echo ""
echo "✅ Swing Trader scheduler activated!"
echo "   Runs every weekday at 9:30am"
echo "   Logs: ~/Documents/Swing trader/logs/bot.log"
echo ""
echo "Press any key to close..."
read -n 1
