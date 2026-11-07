"""Hermes Stage 1 entry point for a verified MCP-capable handler module.

This script intentionally makes no assumptions about Hermes command names or
background-execution APIs. Set AGENTHUB_HANDLER to a deployment-owned Python
function which invokes the locally installed Hermes interface.
"""

try:
    from .poll_client import main
except ImportError:  # Direct execution from the scripts directory.
    from poll_client import main

if __name__ == "__main__":
    main()
