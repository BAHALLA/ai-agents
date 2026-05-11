from orrery_assistant.agent import root_agent
from orrery_core import SecureMemoryService, default_plugins
from orrery_core.runner import create_context_cache_config
from orrery_core.server import ServerConfig, create_app

config = ServerConfig.from_env()
api = create_app(
    root_agent=root_agent,
    app_name="orrery",
    plugins=default_plugins(enable_auth=config.auth_enabled, enable_memory=True),
    config=config,
    memory_service=SecureMemoryService(),
    context_cache_config=create_context_cache_config(),
)
