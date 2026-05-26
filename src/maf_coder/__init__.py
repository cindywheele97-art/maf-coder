"""MAF-Coder — Multi-Agent Framework for Coder.

Production-grade Rust coding agent team. See agent_team_soul.md for the
organizational constitution and MAF-Coder_v2_Build_Plan.md for phased delivery.

Phase A scope (this commit):
- Pydantic schema layer (artifacts + messages)
- ModelRouter (LiteLLM-backed, soul.md §4 droid-whispering)

Subsequent phases activate Orchestrator, Workers, Validators, sandbox, etc.
"""

__version__ = "0.1.0"
