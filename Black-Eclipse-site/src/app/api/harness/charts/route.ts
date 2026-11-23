import { NextResponse } from "next/server";
import {
  HARNESS_PY_URL,
  ensurePythonRuntime,
  probeChartEngine,
} from "@/lib/harness/python-runtime";
import { PYTHON_RUNTIME_DEPS } from "@/lib/harness/runtime-config";
import { runtimeCallbackToken } from "@/lib/harness/runtime-auth";
import { findPython } from "@/lib/scraper/fetchers/python";

export const dynamic = "force-dynamic";

// POST /api/harness/charts — draw a chart with the runtime's chart engine.
//
// The body is a chart SPEC: data only (a kind, some series, a title). The
// runtime validates it, draws it with matplotlib and records the PNG in the
// Artifact Center; this route returns the figure metadata and the artifact id,
// and the browser fetches the picture back through /api/artifacts.
//
// The route is the front door's validation boundary: it forwards the spec
// unchanged (the runtime owns the spec contract) and turns a runtime refusal
// into an honest error the UI can show. It never draws anything itself.
//
// THREE different unavailabilities, three different messages:
//   * the runtime is installed and still booting (or restarting after a code
//     change) — retryable; the UI waits it out, never a repair instruction;
//   * the runtime cannot boot at all (no interpreter / broken stack) — the
//     operator must repair the runtime;
//   * the runtime is healthy but the drawing library is missing — the runtime
//     is installed, `matplotlib`/`plotly` are not. Saying "install the runtime"
//     here sends the operator to fix something that already works.
export async function POST(request: Request) {
  let spec: unknown;
  try {
    spec = await request.json();
  } catch {
    return NextResponse.json(
      { status: "INVALID", error: "the chart request was not readable" },
      { status: 400 },
    );
  }
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) {
    return NextResponse.json(
      { status: "INVALID", error: "a chart request must be a chart spec" },
      { status: 400 },
    );
  }

  try {
    const ready = await ensurePythonRuntime();
    if (!ready) {
      // The runtime did not answer within ONE page request's short budget —
      // usually because it is still importing after a launch or a restart.
      // That is a "wait a moment", NOT a broken install: telling the user to
      // run `npm run setup:python` for a runtime that is booting is the exact
      // misleading error they should never see. Only a genuinely missing
      // interpreter is a repair instruction.
      const installed = Boolean(await findPython(PYTHON_RUNTIME_DEPS));
      if (!installed) {
        return NextResponse.json(
          {
            status: "UNAVAILABLE",
            error:
              "the Python runtime is not installed, so the chart engine cannot start. Run `npm run setup:python`, then restart Eclipse.",
            detail: {
              code: "RUNTIME_UNAVAILABLE",
              message:
                "the Python runtime is not installed, so the chart engine cannot start. Run `npm run setup:python`.",
            },
          },
          { status: 503 },
        );
      }
      return NextResponse.json(
        {
          status: "STARTING",
          error: "Eclipse is starting its chart engine — the chart appears as soon as it is ready.",
          detail: {
            code: "RUNTIME_STARTING",
            message:
              "Eclipse is starting its Python runtime. A first launch can take a minute; the chart appears automatically.",
          },
        },
        { status: 503, headers: { "Retry-After": "2" } },
      );
    }

    // The runtime is up. It still cannot draw without its plotting libraries,
    // and that is a DIFFERENT failure with a different repair.
    const engine = await probeChartEngine();
    if (!engine.ready) {
      const missing = engine.missing.length > 0 ? engine.missing.join(", ") : "matplotlib/plotly";
      const python = engine.python ?? "the selected Python interpreter";
      return NextResponse.json(
        {
          status: "UNAVAILABLE",
          error: `the chart engine cannot draw — ${python} is missing ${missing}. Run \`npm run setup:python\`.`,
          detail: {
            code: "CHART_RENDER_UNAVAILABLE",
            message: `the Python runtime at ${python} is missing ${missing}, so a chart cannot be drawn. Run \`npm run setup:python\`.`,
            missing: engine.missing,
            python: engine.python,
          },
        },
        { status: 503 },
      );
    }

    const url = new URL(`${HARNESS_PY_URL}/charts/render`);
    const operatorToken = runtimeCallbackToken();
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(operatorToken ? { "X-Harness-Operator": operatorToken } : {}),
      },
      body: JSON.stringify(spec),
      cache: "no-store",
    });
    const payload = (await res.json().catch(() => ({}))) as {
      status?: string;
      figure?: unknown;
      artifact?: unknown;
      detail?: { code?: string; message?: string } | string;
    };
    if (!res.ok) {
      const detail = payload.detail;
      const message =
        typeof detail === "object" && detail && "message" in detail
          ? String(detail.message)
          : typeof detail === "string"
            ? detail
            : `the chart engine returned ${res.status}`;
      return NextResponse.json(
        { status: "UNAVAILABLE", error: message, detail },
        { status: res.status },
      );
    }
    return NextResponse.json(payload);
  } catch (err) {
    console.error("[api/harness/charts] POST error:", err);
    return NextResponse.json(
      {
        status: "UNAVAILABLE",
        error: "the chart engine is unavailable — the runtime did not answer.",
        detail: {
          code: "RUNTIME_UNAVAILABLE",
          message: "the chart engine is unavailable — the runtime did not answer.",
        },
      },
      { status: 503 },
    );
  }
}
