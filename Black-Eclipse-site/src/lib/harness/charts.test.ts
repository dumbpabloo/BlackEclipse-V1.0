import { describe, it, expect, vi, beforeEach } from "vitest";

// The chart engine's browser client.
//
// The contract it keeps: a chart is DATA going in, a COMPUTED figure coming
// out, and a failure is an error the UI can show — never a silent empty
// chart.

import { ChartRenderError, chartImageSrc, isRetryableChartError, renderChart } from "@/lib/harness/charts";

const okFigure = {
  kind: "line",
  label: "computed",
  isEvidence: false,
  artifact: "chart-line-abc.png",
  sha256: "abc",
  bytes: 1234,
  width: 960,
  height: 440,
  theme: "eclipse-dark",
  title: "Equity curve",
  series: [{ name: "NAV", points: 2 }],
  summary: "line chart of NAV",
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        status: "OK",
        figure: okFigure,
        artifact: { id: "art_1", name: "chart-line-abc.png", bytes: 1234, sha256: "abc" },
      }),
    })),
  );
});

describe("renderChart", () => {
  it("posts the spec and returns the computed figure", async () => {
    const rendered = await renderChart({
      kind: "line",
      categories: ["Jan", "Feb"],
      series: [{ name: "NAV", data: [100, 104] }],
    });
    expect(rendered.figure.label).toBe("computed");
    expect(rendered.figure.isEvidence).toBe(false);
    expect(rendered.artifact.id).toBe("art_1");
  });

  it("raises the runtime's refusal message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 400,
        json: async () => ({
          detail: { code: "CHART_SPEC_INVALID", message: "a pie chart needs one label per value" },
        }),
      })),
    );
    await expect(renderChart({ kind: "pie", labels: ["a"], values: [1, 2] })).rejects.toThrow(
      /one label per value/,
    );
  });

  it("fails loudly when the engine returns no figure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ status: "OK" }) })),
    );
    await expect(renderChart({ kind: "line" })).rejects.toThrow(/no figure/i);
  });

  it("keeps the drawn picture when the artifact could not be stored", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          status: "OK",
          figure: okFigure,
          artifact: { id: null, name: "chart-line-abc.png", bytes: 1234, sha256: "abc" },
          image: { mimeType: "image/png", data: "aGVsbG8=" },
        }),
      })),
    );
    const rendered = await renderChart({ kind: "line", series: [{ name: "s", data: [1, 2] }] });
    expect(rendered.artifact.id).toBeNull();
    expect(chartImageSrc(rendered)).toBe("data:image/png;base64,aGVsbG8=");
  });

  it("prefers the durable artifact URL once the picture is stored", async () => {
    const rendered = await renderChart({ kind: "line", series: [{ name: "s", data: [1, 2] }] });
    expect(chartImageSrc(rendered)).toBe("/api/artifacts/art_1/download");
  });

  it("returns no source only when no picture exists at all", async () => {
    expect(
      chartImageSrc({
        figure: okFigure,
        artifact: { id: null, name: "x.png", bytes: 0, sha256: "", downloadUrl: null },
      } as Parameters<typeof chartImageSrc>[0]),
    ).toBeNull();
  });
});

describe("retryable chart failures", () => {
  // A cold-starting runtime is NOT a broken chart. The surface retries these
  // instead of showing an error, so the user is never told to install a runtime
  // that is already installed and booting.
  it("treats a starting/unavailable runtime as retryable", () => {
    expect(isRetryableChartError(new ChartRenderError("booting", "RUNTIME_STARTING"))).toBe(true);
    expect(isRetryableChartError(new ChartRenderError("down", "RUNTIME_UNAVAILABLE"))).toBe(true);
  });

  it("does not retry a spec or drawing failure", () => {
    expect(isRetryableChartError(new ChartRenderError("bad spec", "CHART_SPEC_INVALID"))).toBe(false);
    expect(isRetryableChartError(new ChartRenderError("no plotly", "CHART_RENDER_UNAVAILABLE"))).toBe(false);
    expect(isRetryableChartError(new Error("plain"))).toBe(false);
    expect(isRetryableChartError(undefined)).toBe(false);
  });

  it("classifies a dropped connection during a restart as retryable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
    const err = await renderChart({ kind: "line", series: [{ name: "s", data: [1] }] }).catch((e) => e);
    expect(err).toBeInstanceOf(ChartRenderError);
    expect((err as ChartRenderError).code).toBe("RUNTIME_UNAVAILABLE");
    expect(isRetryableChartError(err)).toBe(true);
  });

  it("classifies a codeless 5xx as retryable infrastructure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) })));
    const err = await renderChart({ kind: "line", series: [{ name: "s", data: [1] }] }).catch((e) => e);
    expect((err as ChartRenderError).code).toBe("RUNTIME_UNAVAILABLE");
    expect(isRetryableChartError(err)).toBe(true);
  });
});
