"""SAR Orchestration Layer for LLaMAR — multi-agent Search & Rescue task coordination.

Components:
- SARBarrier: synchronous action collector wrapping LLaMAR SAREnv (barrier.py)
- SARCoordinator: wraps a2a CoordinatorServer with SAR tools (coordinator.py)
- SARWorker: wraps a2a Worker server with SAR action tools (worker.py)
- experiment: end-to-end experiment runner (experiment.py)
- logger: experiment logging utilities (logger.py)
"""
