"""The ONLY package in Penny allowed to talk to OpenAI.

Everything else — routers, the extraction runner, the report generator — goes through
`app.llm.gateway`. Keeping the SDK behind one door is what makes retries, spend accounting
and the `llm_runs` audit trail unbypassable rather than conventional.

No database, no HTTP routes, no message text in logs. See gateway.py's docstring.
"""
