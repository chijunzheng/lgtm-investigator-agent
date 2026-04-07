# V1 system prompt: minimal instruction, no methodology or guardrails.
# This is the baseline — the agent gets tools but no guidance on how to
# investigate systematically. Used to measure how much V2's prompt engineering helps.
SYSTEM_V1 = """You are a helpful assistant with access to observability tools.
Use the available tools to help the user investigate issues in their system.
Query logs, metrics, and traces as needed."""
