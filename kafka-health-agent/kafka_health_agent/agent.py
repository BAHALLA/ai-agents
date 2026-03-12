import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from dotenv import load_dotenv
from google.adk.agents import Agent
from confluent_kafka.admin import AdminClient, NewTopic, ConfigResource
from confluent_kafka import KafkaException

# Load environment variables from .env file in the same directory as this script
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

MODEL_NAME = os.getenv("GEMINI_MODEL_VERSION", "gemini-2.0-flash")
BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")


def _get_admin_client() -> AdminClient:
    """Returns an AdminClient instance."""
    return AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})


def get_kafka_cluster_health() -> Dict[str, Any]:
    """Checks the health of the Kafka cluster.

    Returns:
        A dictionary with the health status and cluster information.
    """
    admin = _get_admin_client()
    try:
        metadata = admin.list_topics(timeout=10)
        brokers = metadata.brokers
        num_brokers = len(brokers)

        health_status = "healthy" if num_brokers > 0 else "unhealthy"

        return {
            "status": "success",
            "health": health_status,
            "brokers_online": num_brokers,
            "brokers": [
                {"id": b.id, "host": b.host, "port": b.port} for b in brokers.values()
            ],
            "message": f"Cluster is {health_status} with {num_brokers} brokers online.",
        }
    except KafkaException as e:
        return {"status": "error", "message": f"Failed to connect to Kafka: {str(e)}"}


def list_kafka_topics() -> Dict[str, Any]:
    """Lists all available topics in the Kafka cluster.

    Returns:
        A dictionary with the list of topics or an error message.
    """
    admin = _get_admin_client()
    try:
        metadata = admin.list_topics(timeout=10)
        topics = list(metadata.topics.keys())
        return {"status": "success", "topics": topics, "count": len(topics)}
    except KafkaException as e:
        return {"status": "error", "message": f"Failed to list topics: {str(e)}"}


def create_kafka_topic(
    topic_name: str, num_partitions: int = 1, replication_factor: int = 1
) -> Dict[str, Any]:
    """Creates a new Kafka topic.

    Args:
        topic_name: Name of the topic to create.
        num_partitions: Number of partitions for the topic.
        replication_factor: Replication factor for the topic.

    Returns:
        A dictionary with the operation result.
    """
    admin = _get_admin_client()
    new_topic = NewTopic(
        topic_name, num_partitions=num_partitions, replication_factor=replication_factor
    )

    try:
        futures = admin.create_topics([new_topic])
        # Wait for the operation to complete
        for topic, future in futures.items():
            try:
                future.result()
                return {
                    "status": "success",
                    "message": f"Topic '{topic_name}' created successfully.",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to create topic '{topic_name}': {str(e)}",
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Unexpected error while creating topic: {str(e)}",
        }


def delete_kafka_topic(topic_name: str) -> Dict[str, Any]:
    """Deletes an existing Kafka topic.

    Args:
        topic_name: Name of the topic to delete.

    Returns:
        A dictionary with the operation result.
    """
    admin = _get_admin_client()
    try:
        futures = admin.delete_topics([topic_name])
        for topic, future in futures.items():
            try:
                future.result()
                return {
                    "status": "success",
                    "message": f"Topic '{topic_name}' deleted successfully.",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to delete topic '{topic_name}': {str(e)}",
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Unexpected error while deleting topic: {str(e)}",
        }


def get_topic_metadata(topic_name: str) -> Dict[str, Any]:
    """Gets detailed metadata for a specific topic.

    Args:
        topic_name: Name of the topic.

    Returns:
        A dictionary with detailed topic metadata.
    """
    admin = _get_admin_client()
    try:
        metadata = admin.list_topics(topic=topic_name, timeout=10)
        if topic_name not in metadata.topics:
            return {"status": "error", "message": f"Topic '{topic_name}' not found."}

        topic_data = metadata.topics[topic_name]
        partitions = []
        for p_id, p_info in topic_data.partitions.items():
            partitions.append(
                {
                    "id": p_id,
                    "leader": p_info.leader,
                    "replicas": p_info.replicas,
                    "isrs": p_info.isrs,
                }
            )

        return {
            "status": "success",
            "topic": topic_name,
            "partitions": partitions,
            "num_partitions": len(partitions),
        }
    except KafkaException as e:
        return {
            "status": "error",
            "message": f"Failed to get metadata for topic '{topic_name}': {str(e)}",
        }


root_agent = Agent(
    name="kafka_health_agent",
    model=MODEL_NAME,
    description=("Agent to monitor and report on the health of a Kafka cluster."),
    instruction=(
        "You are a specialized agent for Kafka monitoring. You can check cluster health, "
        "list topics, and help troubleshoot Kafka-related issues using the provided tools."
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        create_kafka_topic,
        delete_kafka_topic,
        get_topic_metadata,
    ],
)
