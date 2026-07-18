from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.security.guardrails import require_confirmation

from .tools import (
    docker_compose_status,
    get_container_logs,
    get_container_stats,
    inspect_container,
    list_containers,
    list_images,
    remove_image,
    restart_container,
    start_container,
    stop_container,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="docker_agent",
    description=(
        "Specialist for Docker container and image operations. Use this agent for "
        "anything related to containers and images: listing, inspecting, logs, stats, "
        "lifecycle management (start/stop/restart), and compose status."
    ),
    instruction=(
        "You are a Docker operations specialist (SRE). You inspect containers, read "
        "logs, check resource usage, manage container lifecycle, list images, and "
        "report Docker Compose service status.\n\n"
        "## Diagnosis\n"
        "For a named container, go straight to inspect_container / get_container_logs / "
        "get_container_stats. For an open-ended question, list_containers first, then "
        "drill into the suspects. A restarting container: check its exit code "
        "(inspect_container) and the last log lines before claiming a cause. Report "
        "container names, states, and exit codes exactly as returned.\n\n"
        "## Lifecycle operations (stop, start, restart, remove_image)\n"
        "State the exact target (container/image name) before acting, and after the "
        "action verify with list_containers or inspect_container that the container "
        "reached the intended state — report the observed state.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}"
    ),
    tools=[
        list_containers,
        inspect_container,
        get_container_logs,
        get_container_stats,
        docker_compose_status,
        stop_container,
        start_container,
        restart_container,
        list_images,
        remove_image,
    ],
    # Human-in-the-loop gate for @confirm/@destructive tools (stop/restart/
    # remove). Every specialist with guarded tools must wire this — the
    # GuardrailsPlugin enforces RBAC only, not confirmation.
    before_tool_callback=require_confirmation(),
)
