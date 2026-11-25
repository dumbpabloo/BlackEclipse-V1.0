import { describe, it, expect, vi, beforeEach } from "vitest";

const ensurePythonRuntime = vi.fn();
const guardMutation = vi.fn();

vi.mock("@/lib/harness/python-runtime", () => ({
  HARNESS_PY_URL: "http://127.0.0.1:8765",
  ensurePythonRuntime: (...a: unknown[]) => ensurePythonRuntime(...a),
}));
vi.mock("@/lib/harness/runtime-auth", () => ({
  runtimeCallbackToken: () => "test-token",
}));
vi.mock("@/lib/api-guard", () => ({
  guardMutation: (...a: unknown[]) => guardMutation(...a),
}));

import { GET, POST, DELETE } from "@/app/api/artifacts/[[...path]]/route";

function ctx(path: string[]) {
  return { params: Promise.resolve({ path }) };
}

function req(method: string, origin = "http://localhost:3000") {
  return new Request("http://localhost:3000/api/artifacts", {
    method,
    headers: { "content-type": "application/json", ...(origin ? { origin } : {}) },
    body: method === "GET" || method === "DELETE" ? undefined : JSON.stringify({ name: "x" }),
  });
}

describe("/api/artifacts proxy", () => {
  beforeEach(() => {
    ensurePythonRuntime.mockReset();
    guardMutation.mockReset();
    vi.unstubAllGlobals();
  });

  it("refuses a cross-origin mutation before reaching the runtime", async () => {
    guardMutation.mockReturnValue({ status: 403, body: { error: "cross-origin" } });
    const res = await POST(req("POST", "http://evil.example"), ctx([]));
    expect(res.status).toBe(403);
    expect(ensurePythonRuntime).not.toHaveBeenCalled();
  });

  it("returns 503 honestly when the runtime is unavailable", async () => {
    guardMutation.mockReturnValue(null);
    ensurePythonRuntime.mockResolvedValue(false);
    const res = await GET(req("GET"), ctx([]));
    expect(res.status).toBe(503);
  });

  it("forwards a GET to the runtime and passes JSON back", async () => {
    ensurePythonRuntime.mockResolvedValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ success: true, total: 3 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const res = await GET(req("GET"), ctx(["stats"]));
    expect(res.status).toBe(200);
    expect((await res.json()).total).toBe(3);
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain("/artifacts/stats");
  });

  it("streams non-JSON download bytes through", async () => {
    ensurePythonRuntime.mockResolvedValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: { "content-type": "image/png", "content-disposition": 'attachment; filename="a.png"' },
        }),
      ),
    );
    const res = await GET(req("GET"), ctx(["a1", "download"]));
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/png");
  });

  it("forwards Range/If-Range and passes 206 + seek headers back", async () => {
    ensurePythonRuntime.mockResolvedValue(true);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Uint8Array([4, 5, 6, 7]), {
        status: 206,
        headers: {
          "content-type": "video/mp4",
          "accept-ranges": "bytes",
          "content-range": "bytes 4-7/16",
          "content-length": "4",
          etag: '"abc"',
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const rangeRequest = new Request("http://localhost:3000/api/artifacts/a1/download?inline=1", {
      method: "GET",
      headers: { range: "bytes=4-7", "if-range": '"abc"' },
    });
    const res = await GET(rangeRequest, ctx(["a1", "download"]));

    expect(res.status).toBe(206);
    expect(res.headers.get("content-range")).toBe("bytes 4-7/16");
    expect(res.headers.get("accept-ranges")).toBe("bytes");
    expect(res.headers.get("content-length")).toBe("4");
    expect(res.headers.get("vary")).toBe("Range");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Range).toBe("bytes=4-7");
    expect(headers["If-Range"]).toBe('"abc"');
  });

  it("retries a dropped media GET once instead of ending playback", async () => {
    ensurePythonRuntime.mockResolvedValue(true);
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("socket closed"))
      .mockResolvedValueOnce(
        new Response(new Uint8Array([1]), {
          status: 206,
          headers: { "content-type": "video/mp4", "content-range": "bytes 0-0/1" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const res = await GET(req("GET"), ctx(["a1", "download"]));
    expect(res.status).toBe(206);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("guards DELETE as a mutation", async () => {
    guardMutation.mockReturnValue(null);
    ensurePythonRuntime.mockResolvedValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ success: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    await DELETE(req("DELETE"), ctx(["a1"]));
    expect(guardMutation).toHaveBeenCalled();
  });
});
