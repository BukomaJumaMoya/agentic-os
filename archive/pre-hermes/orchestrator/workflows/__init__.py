"""Workflow implementations.

Importing this package registers every workflow. orchestrator.workflow imports
it lazily so that workflow -> workflows -> flagship -> workflow is not a cycle.
"""

from orchestrator.workflows import proposal  # noqa: F401
