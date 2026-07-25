import { describe, expect, it } from "vitest";
import { decodeJwt, identityFromToken, isExpired, roleFromClaims } from "./token";

/** Build an unsigned JWT with the given payload (signature is irrelevant here). */
function makeToken(payload: Record<string, unknown>): string {
  const b64 = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

describe("decodeJwt", () => {
  it("decodes a well-formed payload", () => {
    const token = makeToken({ sub: "alice", roles: ["operator"] });
    expect(decodeJwt(token)).toMatchObject({ sub: "alice", roles: ["operator"] });
  });

  it("returns null for a malformed token", () => {
    expect(decodeJwt("not-a-jwt")).toBeNull();
    expect(decodeJwt("only.two")).toBeNull();
  });
});

describe("roleFromClaims", () => {
  it("maps a roles array", () => {
    expect(roleFromClaims({ roles: ["admin"] })).toBe("admin");
    expect(roleFromClaims({ roles: ["operator"] })).toBe("operator");
  });

  it("maps a space/comma-separated string", () => {
    expect(roleFromClaims({ roles: "operator,foo" })).toBe("operator");
    expect(roleFromClaims({ roles: "orrery-admin something" })).toBe("admin");
  });

  it("defaults to viewer when the claim is missing or unknown", () => {
    expect(roleFromClaims({})).toBe("viewer");
    expect(roleFromClaims({ roles: "nonsense" })).toBe("viewer");
  });

  it("prefers admin over operator when both are present", () => {
    expect(roleFromClaims({ roles: ["operator", "admin"] })).toBe("admin");
  });
});

describe("identityFromToken", () => {
  it("derives subject, role, and expiry", () => {
    const token = makeToken({ sub: "bob", roles: "admin", exp: 1_700_000_000 });
    expect(identityFromToken(token)).toEqual({
      subject: "bob",
      role: "admin",
      expiresAt: 1_700_000_000_000,
    });
  });

  it("returns null for undecodable tokens", () => {
    expect(identityFromToken("garbage")).toBeNull();
  });
});

describe("isExpired", () => {
  it("respects exp", () => {
    const id = { subject: "x", role: "viewer" as const, expiresAt: 1000 };
    expect(isExpired(id, 999)).toBe(false);
    expect(isExpired(id, 1000)).toBe(true);
  });

  it("treats a null expiry as never-expiring", () => {
    const id = { subject: "x", role: "viewer" as const, expiresAt: null };
    expect(isExpired(id, Number.MAX_SAFE_INTEGER)).toBe(false);
  });
});

describe("roleFromClaims with a dotted claim path", () => {
  // Keycloak nests realm roles under realm_access.roles. Before dotted paths
  // the badge silently read undefined and rendered every SSO user as "viewer",
  // disagreeing with the role the server actually enforced.
  it("follows a nested path", () => {
    const claims = { realm_access: { roles: ["admin"] } };
    expect(roleFromClaims(claims, "realm_access.roles")).toBe("admin");
  });

  it("resolves operator from a nested path", () => {
    const claims = { resource_access: { console: { roles: ["operator"] } } };
    expect(roleFromClaims(claims, "resource_access.console.roles")).toBe("operator");
  });

  it("falls back to viewer when the path does not resolve", () => {
    expect(roleFromClaims({ realm_access: {} }, "realm_access.roles")).toBe("viewer");
    expect(roleFromClaims({}, "a.b.c")).toBe("viewer");
  });

  it("does not treat a non-object mid-path as traversable", () => {
    expect(roleFromClaims({ realm_access: "admin" }, "realm_access.roles")).toBe("viewer");
  });

  it("still reads a flat claim by default", () => {
    expect(roleFromClaims({ roles: ["admin"] })).toBe("admin");
  });
});
