"""Model-agnostic LLM access via OpenRouter.

Nothing in the codebase is hardcoded to a single vendor. Models are selected by
config string (e.g. 'anthropic/claude-sonnet-4.6', 'openai/gpt-4o') and can be
swapped without code changes. Zero-Data-Retention routing is enforced by default.
"""
