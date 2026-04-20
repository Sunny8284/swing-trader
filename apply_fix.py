"""
One-time fix for main.py — run this once in VS Code terminal:
    python apply_fix.py
"""
import re

with open("main.py", "r") as f:
    content = f.read()

# Fix 1: Remove required=True from subparsers
content = content.replace(
    'sub = parser.add_subparsers(dest="command", required=True)',
    'sub = parser.add_subparsers(dest="command")'
)

# Fix 2: Add default "run" if no command given (after parse_args)
if 'if not args.command:' not in content:
    content = content.replace(
        '    args = parser.parse_args()\n\n    if args.command == "run":',
        '    args = parser.parse_args()\n\n    if not args.command:\n        args.command = "run"\n\n    if args.command == "run":'
    )

with open("main.py", "w") as f:
    f.write(content)

print("✅ main.py fixed — run 'python main.py' to test.")
