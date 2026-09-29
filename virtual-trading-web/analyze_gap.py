"""Compatibility entrypoint for the corrected offline execution study.
The former broad-label/median-ratio results are historical and not valid attribution.
Live buying logic is unchanged. See research_execution.py and AUDIT_REPAIR.md.
"""
from research_execution import main

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
