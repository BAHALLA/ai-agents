from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env
from orrery_core.security.guardrails import require_confirmation

from .strimzi import (
    approve_kafka_rebalance,
    describe_strimzi_cluster,
    get_kafka_connect_status,
    get_kafka_rebalance_status,
    get_mirrormaker2_status,
    list_kafka_connectors,
    list_kafka_users,
    list_strimzi_clusters,
    list_strimzi_topics,
    restart_kafka_connector,
)
from .tools import (
    create_kafka_topic,
    delete_kafka_topic,
    describe_consumer_groups,
    get_consumer_lag,
    get_kafka_cluster_health,
    get_topic_metadata,
    list_consumer_groups,
    list_kafka_topics,
    update_kafka_partitions,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="kafka_health_agent",
    description=(
        "Agent to monitor and report on the health of a Kafka cluster. "
        "Strimzi-aware: understands Kafka, KafkaTopic, KafkaUser, KafkaConnect, "
        "KafkaConnector, KafkaMirrorMaker2, and KafkaRebalance CRs."
    ),
    instruction=(
        "You are a Kafka operations specialist (SRE). You check cluster health, manage "
        "topics (list, create, delete, metadata, partition scaling), and inspect consumer "
        "groups and lag via the Kafka protocol; Strimzi-aware tools cover "
        "operator-managed resources on the Kubernetes control plane.\n\n"
        "## Tool routing\n"
        "- **Kafka-protocol tools** (get_kafka_cluster_health, list_kafka_topics, "
        "get_topic_metadata, list/describe_consumer_groups, get_consumer_lag) — the "
        "runtime view: what brokers actually report right now. Default to these.\n"
        "- **Strimzi tools** (list/describe_strimzi_clusters, list_strimzi_topics, "
        "list_kafka_users, list_kafka_connectors, get_kafka_connect_status, "
        "get_mirrormaker2_status, get_kafka_rebalance_status) — the declarative view: "
        "CR status from the operator, not the broker. Use for connectors, rebalances, "
        "MM2, users, and 'what does the operator think' questions.\n"
        "- When the two views disagree (e.g. a topic in the CR but not on the broker), "
        "report both readings and name the discrepancy — that gap usually IS the incident.\n\n"
        "## Diagnosing lag\n"
        "Lag is a trend, not a snapshot: report the lag value AND whether the group has "
        "active members (describe_consumer_groups). Lag with no members is an outage, "
        "not slowness.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}"
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        create_kafka_topic,
        delete_kafka_topic,
        update_kafka_partitions,
        get_topic_metadata,
        list_consumer_groups,
        describe_consumer_groups,
        get_consumer_lag,
        list_strimzi_clusters,
        describe_strimzi_cluster,
        list_strimzi_topics,
        list_kafka_users,
        get_kafka_rebalance_status,
        approve_kafka_rebalance,
        get_kafka_connect_status,
        list_kafka_connectors,
        restart_kafka_connector,
        get_mirrormaker2_status,
    ],
    before_tool_callback=require_confirmation(),
)
