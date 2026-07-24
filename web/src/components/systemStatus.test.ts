import { describe, expect, it } from "vitest";
import type { ActivityEntry } from "../api/types";
import { specialistStatuses } from "./systemStatus";

function call(agent: string, status: string, tool = "check"): ActivityEntry {
  return {
    operation: tool,
    details: `[${agent}] ns=default → ${status}`,
    timestamp: "2026-07-25T10:00:00Z",
  };
}

describe("specialistStatuses", () => {
  it("rolls tool calls up into one row per system", () => {
    const rows = specialistStatuses([
      call("kafka_health_agent", "success"),
      call("kafka_health_agent", "success"),
      call("k8s_health_agent", "success"),
    ]);
    expect(rows.map((r) => [r.label, r.calls, r.state])).toEqual([
      ["Kafka", 2, "ok"],
      ["Kubernetes", 1, "ok"],
    ]);
  });

  it("treats the triage workflow's checker nodes as the same system", () => {
    const rows = specialistStatuses([
      call("kafka_health_agent", "success"),
      call("kafka_health_checker", "success"),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].calls).toBe(2);
  });

  it("marks a system failed only when every call failed", () => {
    const partial = specialistStatuses([
      call("docker_agent", "success"),
      call("docker_agent", "error"),
    ]);
    expect(partial[0].state).toBe("partial");

    const failed = specialistStatuses([call("docker_agent", "error")]);
    expect(failed[0].state).toBe("failed");
  });

  it("counts a gate denial as a failure", () => {
    const rows = specialistStatuses([call("k8s_health_agent", "access_denied")]);
    expect(rows[0].state).toBe("failed");
  });

  it("omits systems that were never called", () => {
    // "We didn't ask" must not render as "healthy" — that is the one reading
    // an operator must never take away from a triage sweep.
    const rows = specialistStatuses([call("kafka_health_agent", "success")]);
    expect(rows.map((r) => r.key)).toEqual(["kafka"]);
  });

  it("keeps a stable order across refreshes", () => {
    const entries = [call("docker_agent", "success"), call("kafka_health_agent", "success")];
    expect(specialistStatuses(entries).map((r) => r.key)).toEqual(["kafka", "docker"]);
    expect(specialistStatuses([...entries].reverse()).map((r) => r.key)).toEqual([
      "kafka",
      "docker",
    ]);
  });

  it("ignores the coordinator and unknown agents", () => {
    const rows = specialistStatuses([
      call("orrery_chat_agent", "success"),
      call("ops_journal_agent", "success"),
    ]);
    expect(rows).toEqual([]);
  });

  it("survives a details string that doesn't match the expected shape", () => {
    const rows = specialistStatuses([
      { operation: "x", details: "no agent, no arrow", timestamp: "bad" },
    ]);
    expect(rows).toEqual([]);
  });
});
