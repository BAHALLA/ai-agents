import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { MeResponse, SelfTestResponse } from "../api/types";

export interface SystemController {
  /** The server's own view of the caller — null until it loads. */
  me: MeResponse | null;
  selfTest: SelfTestResponse | null;
  isChecking: boolean;
  error: string | null;
  runSelfTest: () => Promise<void>;
}

/**
 * Deployment posture and first-run diagnostics (AEP-019 Milestone 3).
 *
 * `/me` is fetched once on mount: it carries the role the *server* resolved and
 * the autonomy level actually in force, which is what decides whether a
 * mutating tool will run. The console also decodes the JWT locally for the
 * badge, but that is the browser's reading of a token it cannot verify — this
 * is the authoritative answer, and the two are shown together so a mismatch is
 * visible rather than mysterious.
 *
 * The self-test is on demand, never automatic: it reaches out to real
 * infrastructure and does a one-token model round-trip, so it runs when an
 * operator asks for it.
 */
export function useSystem(token: string | null): SystemController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [me, setMe] = useState<MeResponse | null>(null);
  const [selfTest, setSelfTest] = useState<SelfTestResponse | null>(null);
  const [isChecking, setIsChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  useEffect(() => {
    let cancelled = false;
    // Advisory data for a badge: an older server without /me simply leaves the
    // autonomy row absent rather than breaking the console.
    client
      .me()
      .then((res) => {
        if (!cancelled) setMe(res);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [client, token]);

  const runSelfTest = useCallback(async () => {
    if (isChecking) return;
    setIsChecking(true);
    setError(null);
    try {
      setSelfTest(await client.selfTest());
    } catch (err) {
      const rateLimited = err instanceof ApiError && err.isRateLimited;
      setError(
        rateLimited
          ? "Checks are rate limited — wait a moment before running them again."
          : err instanceof ApiError
            ? err.message
            : "Could not run the environment check.",
      );
    } finally {
      setIsChecking(false);
    }
  }, [client, isChecking]);

  return { me, selfTest, isChecking, error, runSelfTest };
}
