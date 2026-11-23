import { describe, it, expect, vi, beforeEach } from "vitest";

// Front-door contract for POST /api/harness/charts — the route that draws a
// chart with the runtime's chart engine.
//
// The route's job is narrow: forward the chart SPEC unchanged, turn a runtime
// refusal into an honest error the UI can show, and never pretend a chart
// exists when the engine could not draw one.

const runtimeFetch = vi.fn(async (_url: string, _init?: RequestInit): Promise<unknown> => ({
  ok: true,
  status: 200,
  json: async () => ({
    status: "OK",
    figure: {
      kind: "line",
      label: "computed",
      isEvidence: false,
      artifact: "chart-line-abc.png",
      sha256: "abc",
      bytes: 1234,
      width: 960,
      height: 440,
      theme: "eclipse-dark",
      title: "T",
      series: [{ name: "s", points: 2 }],
      summary: "line chart of s",
    },
    artifact: {
      id: "art_1",
      name: "chart-line-abc.png",
      bytes: 1234,
      sha256: "abc",
      downloadUrl: "/artifacts/art_1/download",
    },
  }),
} as unknown as Response));

vi.mock("@/lib/harness/python-runtime", () => ({
  HARNESS_PY_URL: "http://127.0.0.1:8765",
  ensurePythonRuntime: vi.fn(async () => true),
  probeChartEngine: vi.fn(async () => ({ ready: true, python: "/v/python", missing: [] })),
}));

vi.mock("@/lib/harness/runtime-auth", () => ({
  runtimeCallbackToken: () => "token",
}));

vi.mock("@/lib/scraper/fetchers/python", () => ({
  findPython: vi.fn(async () => null),
  findProjectRoot: () => process.cwd(),
}));

import { POST } from "@/app/api/harness/charts/route";
import { ensurePythonRuntime, probeChartEngine } from "@/lib/harness/python-runtime";
import { findPython } from "@/lib/scraper/fetchers/python";

function req(body: unknown): Request {
  return new Request("http://localhost:3000/api/harness/charts", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  runtimeFetch.mockClear();
  vi.stubGlobal("fetch", runtimeFetch);
  vi.mocked(ensurePythonRuntime).mockResolvedValue(true);
  vi.mocked(probeChartEngine).mockResolvedValue({ ready: true, python: "/v/python", missing: [] });
  vi.mocked(findPython).mockResolvedValue(null);
  runtimeFetch.mockImplementation(async (_url: string, _init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () => ({
      status: "OK",
      figure: {
        kind: "line",
        label: "computed",
        isEvidence: false,
        artifact: "chart-line-abc.png",
        sha256: "abc",
        bytes: 1234,
        width: 960,
        height: 440,
        theme: "eclipse-dark",
        title: "T",
        series: [{ name: "s", points: 2 }],
        summary: "line chart of s",
      },
      artifact: {
        id: "art_1", name: "chart-line-abc.png", bytes: 1234, sha256: "abc",
        downloadUrl: "/artifacts/art_1/download",
      },
    }),
  } as unknown as Response));
});

describe("POST /api/harness/charts", () => {
  it("forwards the spec to the runtime and returns the figure", async () => {
    const res = await POST(req({ kind: "line", series: [{ name: "s", data: [1, 2] }] }));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.figure.label).toBe("computed");
    expect(body.figure.isEvidence).toBe(false);
    expect(runtimeFetch).toHaveBeenCalledTimes(1);
  });

  it("rejects a body that is not a chart spec", async () => {
    const res = await POST(req([1, 2, 3]));
    expect(res.status).toBe(400);
    expect(runtimeFetch).not.toHaveBeenCalled();
  });

  it("rejects an unreadable body", async () => {
    const res = await POST(req("{not json"));
    expect(res.status).toBe(400);
    expect(runtimeFetch).not.toHaveBeenCalled();
  });

  it("passes the runtime's refusal through as an honest error", async () => {
    runtimeFetch.mockImplementation(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ detail: { code: "CHART_SPEC_INVALID", message: "a pie chart needs one label per value" } }),
    } as unknown as Response));
    const res = await POST(req({ kind: "pie", labels: ["a"], values: [1, 2] }));
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("one label per value");
  });

  it("reports an unavailable engine as 503, not as an empty chart", async () => {
    runtimeFetch.mockImplementation(async () => {
      throw new Error("runtime down");
    });
    const res = await POST(req({ kind: "line", series: [] }));
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.status).toBe("UNAVAILABLE");
  });

  it("names the repair command when the runtime is not installed", async () => {
    vi.mocked(ensurePythonRuntime).mockResolvedValueOnce(false);
    vi.mocked(findPython).mockResolvedValueOnce(null);
    const res = await POST(req({ kind: "line", series: [{ name: "s", data: [1] }] }));
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.error).toContain("setup:python");
    expect(body.detail.code).toBe("RUNTIME_UNAVAILABLE");
    expect(runtimeFetch).not.toHaveBeenCalled();
  });

  // The runtime is INSTALLED and booting. A page request has a short budget, so
  // it answers "starting" — a retryable state the chart surface waits out. It
  // must never tell the user to install something that already exists.
  it("answers a booting runtime as retryable, never as a missing install", async () => {
    vi.mocked(ensurePythonRuntime).mockResolvedValueOnce(false);
    vi.mocked(findPython).mockResolvedValueOnce({ cmd: "python" });
    const res = await POST(req({ kind: "line", series: [{ name: "s", data: [1] }] }));
    expect(res.status).toBe(503);
    expect(res.headers.get("Retry-After")).toBe("2");
    const body = await res.json();
    expect(body.status).toBe("STARTING");
    expect(body.detail.code).toBe("RUNTIME_STARTING");
    expect(body.error).not.toContain("setup:python");
    expect(runtimeFetch).not.toHaveBeenCalled();
  });

  // The bug this route used to hide: the runtime is healthy, so the generic
  // "install the Python runtime" message sent the operator to fix something
  // that was already installed. A missing drawing library is a DIFFERENT,
  // named failure that points at the actual interpreter and modules.
  it("names the missing drawing libraries when the runtime cannot draw", async () => {
    vi.mocked(probeChartEngine).mockResolvedValueOnce({
      ready: false,
      python: "C:/vendor/python/python.exe",
      missing: ["matplotlib", "plotly"],
    });
    const res = await POST(req({ kind: "line", series: [{ name: "s", data: [1] }] }));
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.status).toBe("UNAVAILABLE");
    expect(body.detail.code).toBe("CHART_RENDER_UNAVAILABLE");
    expect(body.detail.missing).toEqual(["matplotlib", "plotly"]);
    expect(body.error).toContain("C:/vendor/python/python.exe");
    expect(body.error).toContain("matplotlib");
    expect(body.error).toContain("setup:python");
    expect(runtimeFetch).not.toHaveBeenCalled();
  });
});
