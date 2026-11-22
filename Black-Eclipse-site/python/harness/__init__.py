"""BlackEclipse harness runtime.

A FastAPI + LangGraph service that executes agent runs. It is the authoritative
runtime: the Next.js harness owns contracts, the tool registry (JDdS), and run
persistence, and delegates execution here.

Layers:
    schemas.py   pydantic mirrors of the TS harness contracts (the wire contract)
    models.py    universal model catalog + LangChain chat-model factory
    tools.py     LangChain tool adapters that execute via the TS tool bridge
    events.py    event emission back to the TS harness
    graph.py     the LangGraph StateGraph execution loop
    main.py      FastAPI app (run / cancel / models / health)
"""

__version__ = "0.4.26"

#: The declared PUBLIC API surface version (harness.public). It changes only
#: when the exported shape changes, independent of the package version.
HARNESS_API_VERSION = "1.0.0"
