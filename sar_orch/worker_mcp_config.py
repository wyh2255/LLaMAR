"""Generate per-worker MCP config pointing to the Map Agent endpoint."""

import json
from pathlib import Path


def write_worker_mcp_config(log_dir: str, agent_name: str, coordinator_http_url: str) -> Path:
    """Write mcp.json for a worker agent.

    Args:
        log_dir: Experiment log directory (e.g. logs/20260719_143022)
        agent_name: Worker agent name (e.g. Alice, Bob)
        coordinator_http_url: Coordinator HTTP base URL (e.g. http://localhost:8080)

    Returns:
        Path to the written mcp.json file
    """
    config = {
        "mcpServers": {
            "map_agent": {
                "url": f"{coordinator_http_url}/mcp/map",
                "type": "streamable_http",
            }
        }
    }
    path = Path(log_dir) / f"mcp_{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    return path
